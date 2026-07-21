import base64
import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import random
import secrets
from contextlib import asynccontextmanager, suppress
from io import BytesIO
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select

from .database import SessionLocal, init_db
from .incidents import create_incident, latest_incident
from .models import Order, Photo, ReminderEvent, ReuploadToken, StageEvent
from .observability import count, gauge, histogram, log_event, publish_metrics, publish_order_logs
from .operations import (
    MAX_PHOTO_REMINDERS,
    STAGE_LABELS,
    current_stage_started,
    elapsed_seconds,
    format_duration,
    format_timestamp,
    operations_context,
    order_stage_state,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    migrate_sample_photo_paths()
    seed_count = int(os.getenv("SEED_DEMO_ORDERS", "0"))
    if seed_count:
        with SessionLocal() as session:
            order_count = session.scalar(select(func.count()).select_from(Order))
        if order_count == 0:
            for index in range(seed_count):
                ingest_order(fake_shopify_payload(index + 1), "sim")
    history_count = int(os.getenv("SEED_DEMO_HISTORY", "0"))
    if history_count and sim_mode():
        seed_demo_history(history_count)
    global worker_task
    worker_task = asyncio.create_task(queue_worker())
    try:
        yield
    finally:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task


app = FastAPI(title="Shopfloor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "app/static/uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory="app/templates")

STAGES = ["received", "qc", "on_hold_photo", "ready_to_print", "printed", "pressed", "shipped", "delivered"]
BOARD_STAGES = [stage for stage in STAGES if stage != "delivered"]
NEXT_STAGE = {
    "received": "qc",
    "qc": "ready_to_print",
    "on_hold_photo": "qc",
    "ready_to_print": "printed",
    "printed": "pressed",
    "pressed": "shipped",
    "shipped": "delivered",
}
MANUAL_STAGES = {"ready_to_print", "printed", "pressed", "shipped"}
SAMPLE_PHOTO_GROUPS = {
    "good": [
        "good.jpg",
        "good-beach-family.jpg",
        "good-newborn-family.jpg",
        "good-hiking-friends.jpg",
    ],
    "blurry": [
        "blurry.jpg",
        "blurry-wedding-dance.jpg",
        "blurry-dog-park.jpg",
        "blurry-football.jpg",
        "blurry-bicycles.jpg",
    ],
    "low-detail": [
        "low-detail.jpg",
        "low-detail-concert.jpg",
        "low-detail-restaurant.jpg",
        "low-detail-family.jpg",
    ],
    "face-near-edge": [
        "face-near-edge.jpg",
        "face-near-edge-graduation.jpg",
        "face-near-edge-family.jpg",
    ],
}
SAMPLE_PHOTOS = [
    "good.jpg",
    "blurry.jpg",
    "low-detail.jpg",
    "face-near-edge.jpg",
    "good-beach-family.jpg",
    "blurry-wedding-dance.jpg",
    "low-detail-concert.jpg",
    "face-near-edge-graduation.jpg",
    "good-newborn-family.jpg",
    "blurry-dog-park.jpg",
    "low-detail-restaurant.jpg",
    "face-near-edge-family.jpg",
    "good-hiking-friends.jpg",
    "blurry-football.jpg",
    "low-detail-family.jpg",
    "blurry-bicycles.jpg",
]
SAFE_SAMPLE_PATHS = {f"/static/sample_photos/{name}" for name in SAMPLE_PHOTOS}
LEGACY_SAMPLE_PATHS = {
    "/static/sample_photos/good.svg": "/static/sample_photos/good.jpg",
    "/static/sample_photos/blurry.svg": "/static/sample_photos/blurry.jpg",
    "/static/sample_photos/low-res.svg": "/static/sample_photos/low-detail.jpg",
    "/static/sample_photos/face-near-edge.svg": "/static/sample_photos/face-near-edge.jpg",
}
NAMES = [("Sofía Martín", "sofia@example.com"), ("Lucas Pérez", "lucas@example.com"), ("Elena Ruiz", "elena@example.com"), ("Mateo García", "mateo@example.com")]
PACKAGES = ["9 imanes personalizados", "12 imanes personalizados", "24 imanes personalizados"]
QC_PROMPT = """You are the print-quality inspector for a shop that heat-presses customer photos onto 50mm magnets. Judge this photo for printability at that size: sharpness (especially faces), effective resolution for a 50x50mm print, exposure, and crop risk. Return JSON only: {\"verdict\": \"pass\"|\"fail\", \"reasons\": [...], \"customer_message\": \"...\"}. customer_message is a warm, plain-Spanish, one-sentence explanation with a concrete suggestion."""
QC_NOT_CONFIGURED_MESSAGE = "El control de calidad automático no está configurado. Añade la clave de OpenAI y pulsa «Reintentar control de calidad»."
QC_LIMIT_MESSAGE = "Se alcanzó el límite de revisiones automáticas. Amplía el límite o revisa la foto manualmente."
QC_ERROR_MESSAGE = "La revisión automática falló. Pulsa «Reintentar control de calidad» para intentarlo de nuevo."
QC_TEMPORARILY_UNAVAILABLE_MESSAGE = "No podemos revisar la foto ahora mismo. Tu enlace sigue activo; inténtalo de nuevo en unos minutos o contacta con la tienda."
ALLOWED_IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
MAX_IMAGE_PIXELS = 25_000_000
worker_task: asyncio.Task | None = None
chaos_poison_next = False
chaos_slow_until: datetime | None = None
last_http_publish_at: datetime | None = None


def sim_mode() -> bool:
    return os.getenv("SIM_MODE", "false").lower() == "true"


def chaos_controls_enabled() -> bool:
    return sim_mode() and os.getenv("ENABLE_CHAOS_CONTROLS", "true").lower() == "true"


def slow_mode_active() -> bool:
    return chaos_slow_until is not None and chaos_slow_until > now()


@app.middleware("http")
async def apply_chaos_latency(request: Request, call_next):
    if slow_mode_active() and not request.url.path.startswith(("/chaos/", "/health")):
        await asyncio.sleep(float(os.getenv("CHAOS_SLOW_SECONDS", "2.5")))
    return await call_next(request)


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def migrate_sample_photo_paths() -> None:
    with SessionLocal() as session:
        changed = 0
        for old_path, new_path in LEGACY_SAMPLE_PATHS.items():
            changed += session.query(Photo).filter(Photo.file_path == old_path).update(
                {Photo.file_path: new_path}, synchronize_session=False
            )
        category_indexes = {category: 0 for category in SAMPLE_PHOTO_GROUPS}
        active_photos = session.scalars(
            select(Photo)
            .join(Order, Order.id == Photo.order_id)
            .where(
                Order.status != "delivered",
                Photo.replaced_by.is_(None),
                Photo.file_path.startswith("/static/sample_photos/"),
            )
            .order_by(Photo.id)
        ).all()
        for photo in active_photos:
            category = sample_photo_category(photo.file_path)
            choices = SAMPLE_PHOTO_GROUPS[category]
            replacement = f"/static/sample_photos/{choices[category_indexes[category] % len(choices)]}"
            category_indexes[category] += 1
            if photo.file_path != replacement:
                photo.file_path = replacement
                changed += 1
        if changed:
            session.commit()
            log_event("sample_photos_migrated", photo_count=changed)


def sample_photo_category(file_path: str) -> str:
    if "blurry" in file_path:
        return "blurry"
    if "low-res" in file_path or "low-detail" in file_path:
        return "low-detail"
    if "face-near-edge" in file_path:
        return "face-near-edge"
    return "good"


def payload_created_at(payload: dict) -> datetime:
    value = payload.get("created_at")
    if not value:
        return now()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return now()
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def hmac_is_valid(body: bytes, signature: str | None) -> bool:
    secret = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
    if not secret or not signature:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


def datadog_webhook_is_valid(secret: str | None) -> bool:
    expected = os.getenv("DATADOG_WEBHOOK_SECRET", "")
    if sim_mode() and not expected:
        return True
    return bool(expected and secret and hmac.compare_digest(expected, secret))


def transition(session, order: Order, destination: str) -> None:
    if order.status == destination:
        return
    previous = order.status
    started_at = session.scalar(select(StageEvent.at).where(StageEvent.order_id == order.id, StageEvent.to_status == previous).order_by(StageEvent.at.desc()))
    order.status = destination
    at = now()
    session.add(StageEvent(order_id=order.id, from_status=previous, to_status=destination, at=at))
    if started_at:
        histogram("maker.stage.cycle_seconds", (at - started_at).total_seconds(), tags=[f"stage:{previous}"])
    log_event(
        "stage_transition",
        order_id=order.id,
        from_status=previous,
        to_status=destination,
        order_url=f"{os.getenv('APP_BASE_URL', '').rstrip('/')}/orders/{order.id}" if os.getenv("APP_BASE_URL") else f"/orders/{order.id}",
    )


def sample_qc(file_path: str) -> dict:
    if "blurry" in file_path:
        return {"verdict": "fail", "reasons": ["La foto está borrosa, especialmente en la cara."], "customer_message": "La foto se ve borrosa; ¿podrías subir otra más nítida y tomada con buena luz?"}
    if "low-res" in file_path or "low-detail" in file_path:
        return {"verdict": "fail", "reasons": ["La resolución es demasiado baja para un imán de 50 mm."], "customer_message": "La foto tiene poca resolución; ¿puedes subir el archivo original o una imagen más grande?"}
    if "face-near-edge" in file_path:
        return {"verdict": "fail", "reasons": ["El recorte cuadrado podría cortar la cara."], "customer_message": "La cara queda muy cerca del borde; ¿puedes subir una foto con un poco más de espacio alrededor?"}
    return {"verdict": "pass", "reasons": [], "customer_message": ""}


class QCUnavailable(Exception):
    pass


def qc_result(file_path: str) -> dict:
    if file_path.startswith("/static/sample_photos/") and os.getenv("SIM_MODE", "false").lower() == "true":
        return sample_qc(file_path)
    if not os.getenv("OPENAI_API_KEY"):
        raise QCUnavailable("OpenAI API key is not configured")
    from openai import OpenAI

    local_file = None
    if file_path.startswith("/static/"):
        local_file = Path("app") / file_path.lstrip("/")
    elif file_path.startswith("/uploads/"):
        local_file = UPLOAD_DIR / Path(file_path).name
    if local_file is not None:
        mime = mimetypes.guess_type(local_file.name)[0] or "application/octet-stream"
        image_url = f"data:{mime};base64,{base64.b64encode(local_file.read_bytes()).decode()}"
    else:
        image_url = file_path
    response = OpenAI().responses.create(
        model="gpt-5.6-terra",
        input=[{"role": "user", "content": [{"type": "input_text", "text": QC_PROMPT}, {"type": "input_image", "image_url": image_url}]}],
    )
    content = response.output_text.strip().removeprefix("```json").removesuffix("```").strip()
    result = json.loads(content)
    if result.get("verdict") not in {"pass", "fail"}:
        raise ValueError("Vision QC returned an invalid verdict")
    return result


def inspect_order(order_id: int) -> None:
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        if not order or order.status != "qc":
            return
        inspected_count = 0
        rejected_count = 0
        photos = session.scalars(select(Photo).where(Photo.order_id == order.id, Photo.replaced_by.is_(None))).all()
        for photo in photos:
            if photo.qc_status != "pending":
                continue
            if photo.customer_message:
                continue
            if not photo.file_path.startswith("/static/sample_photos/"):
                calls_used = real_qc_calls_used(session)
                if calls_used >= int(os.getenv("MAX_REAL_QC_CALLS", "20")):
                    photo.customer_message = QC_LIMIT_MESSAGE
                    log_event("qc_quota_reached", order_id=order.id, limit=int(os.getenv("MAX_REAL_QC_CALLS", "20")))
                    session.commit()
                    return
            try:
                result = qc_result(photo.file_path)
            except QCUnavailable:
                photo.customer_message = QC_NOT_CONFIGURED_MESSAGE
                log_event("qc_unavailable", order_id=order.id)
                session.commit()
                return
            except Exception as exc:
                photo.customer_message = QC_ERROR_MESSAGE
                log_event(
                    "qc_error",
                    order_id=order.id,
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
                session.commit()
                return
            photo.qc_status = result["verdict"]
            photo.qc_reasons = result.get("reasons", [])
            photo.customer_message = result.get("customer_message", "")
            inspected_count += 1
            if photo.qc_status == "fail":
                rejected_count += 1
                log_event("qc_rejected", order_id=order.id, photo_id=photo.id, reasons=photo.qc_reasons)
        if any(photo.qc_status == "pending" for photo in photos):
            return
        if any(photo.qc_status == "fail" for photo in photos):
            transition(session, order, "on_hold_photo")
            for photo in photos:
                if photo.qc_status == "fail":
                    session.add(ReuploadToken(token=secrets.token_urlsafe(24), order_id=order.id, photo_id=photo.id, expires_at=now() + timedelta(hours=72), used_at=None))
        else:
            transition(session, order, "ready_to_print")
        session.commit()
        if inspected_count:
            count("maker.qc.inspected", value=inspected_count)
        if rejected_count:
            count("maker.qc.rejected", value=rejected_count)


def send_due_photo_reminders(session, at: datetime) -> int:
    sent_count = 0
    orders = session.scalars(select(Order).where(Order.status == "on_hold_photo")).all()
    for order in orders:
        events = session.scalars(
            select(StageEvent).where(StageEvent.order_id == order.id).order_by(StageEvent.at)
        ).all()
        reminders = session.scalars(
            select(ReminderEvent).where(ReminderEvent.order_id == order.id).order_by(ReminderEvent.sent_at)
        ).all()
        if len(reminders) >= MAX_PHOTO_REMINDERS:
            continue
        stage_started = current_stage_started(order, events)
        reference = reminders[-1].sent_at if reminders else stage_started
        if elapsed_seconds(reference, at) < 24 * 60 * 60:
            continue
        reminder_number = len(reminders) + 1
        session.add(
            ReminderEvent(
                order_id=order.id,
                reminder_number=reminder_number,
                sent_at=at,
                channel="demo_email",
            )
        )
        log_event(
            "customer_photo_reminder_sent",
            order_id=order.id,
            reminder_number=reminder_number,
            order_url=f"{os.getenv('APP_BASE_URL', '').rstrip('/')}/orders/{order.id}" if os.getenv("APP_BASE_URL") else f"/orders/{order.id}",
        )
        sent_count += 1
    return sent_count


def emit_operational_metrics(summary: dict) -> None:
    for row in summary["stage_rows"]:
        tags = [f"stage:{row['status']}"]
        gauge("maker.orders.by_status", row["count"], tags=tags)
        gauge("maker.orders.overdue", row["overdue_count"], tags=tags)
        oldest_state = row.get("oldest_state")
        gauge(
            "maker.stage.oldest_age_seconds",
            oldest_state["elapsed_seconds"] if oldest_state else 0,
            tags=tags,
        )
    performance = summary["performance"]
    for key, metric in (
        ("production_median", "maker.production.cycle.median_seconds"),
        ("production_p90", "maker.production.cycle.p90_seconds"),
        ("fulfillment_average", "maker.fulfillment.average_seconds"),
        ("customer_wait_average", "maker.customer_wait.average_seconds"),
        ("delivery_average", "maker.delivery.average_seconds"),
    ):
        if performance[key] is not None:
            gauge(metric, performance[key])
    gauge("maker.orders.shipped_7d", performance["shipped_count"])
    gauge("maker.orders.delivered_7d", performance["delivered_count"])
    gauge("maker.orders.attention", len(summary["attention_orders"]))


def publish_hosted_snapshot(summary: dict, at: datetime) -> None:
    global last_http_publish_at
    interval = int(os.getenv("DD_HTTP_INTERVAL_SECONDS", "60"))
    if last_http_publish_at and elapsed_seconds(last_http_publish_at, at) < interval:
        return
    last_http_publish_at = at
    points: list[tuple[str, float, list[str] | None]] = []
    for row in summary["stage_rows"]:
        tags = [f"stage:{row['status']}"]
        points.extend([
            ("maker.orders.by_status", row["count"], tags),
            ("maker.orders.overdue", row["overdue_count"], tags),
            (
                "maker.stage.oldest_age_seconds",
                row["oldest_state"]["elapsed_seconds"] if row["oldest_state"] else 0,
                tags,
            ),
        ])
    performance = summary["performance"]
    for key, metric in (
        ("production_median", "maker.production.cycle.median_seconds"),
        ("production_p90", "maker.production.cycle.p90_seconds"),
        ("fulfillment_average", "maker.fulfillment.average_seconds"),
        ("customer_wait_average", "maker.customer_wait.average_seconds"),
        ("delivery_average", "maker.delivery.average_seconds"),
    ):
        if performance[key] is not None:
            points.append((metric, performance[key], None))
    points.extend([
        ("maker.orders.shipped_7d", performance["shipped_count"], None),
        ("maker.orders.delivered_7d", performance["delivered_count"], None),
        ("maker.orders.attention", len(summary["attention_orders"]), None),
        ("maker.qc.worker.heartbeat", 1, None),
    ])
    publish_metrics(points)

    base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    logs = []
    for order in summary["attention_orders"]:
        state = summary["states"][order.id]
        logs.append({
            "message": f"Order {order.id} needs attention in {STAGE_LABELS[order.status]}",
            "order_id": order.id,
            "order_url": f"{base_url}/orders/{order.id}",
            "stage": order.status,
            "stage_label": STAGE_LABELS[order.status],
            "stage_age_seconds": state["elapsed_seconds"],
            "sla_target_seconds": state.get("target_seconds"),
            "reminder_count": state.get("reminder_count", 0),
            "last_reminder_at": state.get("last_reminder_at").isoformat() if state.get("last_reminder_at") else None,
            "needs_attention": True,
        })
    publish_order_logs(logs)


def process_queue() -> None:
    with SessionLocal() as session:
        received = session.scalars(select(Order).where(Order.status == "received")).all()
        for order in received:
            transition(session, order, "qc")
        session.commit()
        order_ids = session.scalars(select(Order.id).where(Order.status == "qc")).all()
    for order_id in order_ids:
        inspect_order(order_id)
    with SessionLocal() as session:
        at = now()
        reminders_sent = send_due_photo_reminders(session, at)
        session.commit()
        summary = operations_context(session, at)
        oldest = session.scalar(
            select(func.min(Order.created_at)).where(~Order.status.in_(("shipped", "delivered")))
        )
    gauge("maker.backlog.oldest_order_age_hours", 0 if not oldest else (now() - oldest).total_seconds() / 3600)
    emit_operational_metrics(summary)
    publish_hosted_snapshot(summary, at)
    if reminders_sent:
        count("maker.customer.reminders_sent", reminders_sent)
    gauge("maker.qc.worker.heartbeat", 1)


async def queue_worker() -> None:
    global chaos_poison_next
    while True:
        if chaos_poison_next:
            chaos_poison_next = False
            log_event("chaos_worker_crash", mode="poison")
            raise RuntimeError("Chaos poison upload stopped the QC worker")
        await asyncio.to_thread(process_queue)
        await asyncio.sleep(2)


def real_qc_calls_used(session) -> int:
    return session.scalar(
        select(func.count()).select_from(Photo).where(
            ~Photo.file_path.startswith("/static/sample_photos/"),
            Photo.qc_status.in_(("pass", "fail")),
        )
    )


def real_qc_is_available(session) -> bool:
    return bool(os.getenv("OPENAI_API_KEY")) and real_qc_calls_used(session) < int(
        os.getenv("MAX_REAL_QC_CALLS", "20")
    )


def photo_urls(payload: dict, *, trusted_source: bool) -> list[str]:
    urls: list[str] = []
    for item in payload.get("line_items", []):
        for prop in item.get("properties") or []:
            name, value = str(prop.get("name", "")).lower(), str(prop.get("value", ""))
            if not any(word in name for word in ("photo", "image", "upload")):
                continue
            if value.startswith("/static/"):
                if not trusted_source and value not in SAFE_SAMPLE_PATHS:
                    raise HTTPException(
                        status_code=400,
                        detail="Unsigned demo webhooks may only use the published sample photos",
                    )
                urls.append(value)
            elif value.startswith(("http://", "https://")):
                if not trusted_source:
                    raise HTTPException(
                        status_code=400,
                        detail="Unsigned demo webhooks may only use local /static/ sample photos",
                    )
                urls.append(value)
    return urls or ["/static/sample_photos/good.jpg"]


def ingest_order(payload: dict, source: str, *, verified_source: bool = False) -> Order:
    shopify_id = str(payload.get("id")) if payload.get("id") is not None else None
    with SessionLocal() as session:
        if shopify_id and session.scalar(select(Order).where(Order.shopify_order_id == shopify_id)):
            return session.scalar(select(Order).where(Order.shopify_order_id == shopify_id))
        if sim_mode():
            order_count = session.scalar(select(func.count()).select_from(Order))
            limit = int(os.getenv("MAX_SIM_ORDERS_TOTAL", "200"))
            if order_count >= limit:
                raise HTTPException(status_code=409, detail=f"Demo order limit reached ({limit})")
        customer = payload.get("customer") or {}
        name = " ".join(filter(None, [customer.get("first_name"), customer.get("last_name")])).strip() or payload.get("name", "Cliente")
        trusted_internal_source = source in {"sim", "event"}
        created_at = payload_created_at(payload) if verified_source or trusted_internal_source else now()
        order = Order(source=source, shopify_order_id=shopify_id, customer_name=name, email=payload.get("email") or customer.get("email"), package=(payload.get("line_items") or [{"title": "Pedido personalizado"}])[0].get("title", "Pedido personalizado"), status="received", created_at=created_at, sla_due_at=created_at + timedelta(hours=48))
        session.add(order)
        session.flush()
        session.add(StageEvent(order_id=order.id, from_status=None, to_status="received", at=now()))
        for path in photo_urls(payload, trusted_source=verified_source or trusted_internal_source):
            session.add(Photo(order_id=order.id, file_path=path, qc_status="pending", qc_reasons=None, customer_message=None, replaced_by=None))
        session.commit()
        session.refresh(order)
        count("maker.orders.created", tags=[f"source:{source}"])
        log_event("order_created", order_id=order.id, source=source)
        return order


def fake_shopify_payload(number: int) -> dict:
    name, email = random.choice(NAMES)
    first, last = name.split(" ", 1)
    photo = SAMPLE_PHOTOS[(number - 1) % len(SAMPLE_PHOTOS)]
    return {"id": 900000 + number, "name": f"#{9000 + number}", "email": email, "created_at": datetime.now(UTC).isoformat(), "customer": {"first_name": first, "last_name": last, "email": email}, "line_items": [{"title": random.choice(PACKAGES), "quantity": 1, "properties": [{"name": "Customer photo upload", "value": f"/static/sample_photos/{photo}"}]}]}


def seed_demo_history(count: int) -> None:
    with SessionLocal() as session:
        if session.scalar(select(func.count()).select_from(StageEvent).where(StageEvent.to_status == "delivered")):
            return
        anchor = now()
        for index in range(count):
            name, email = NAMES[index % len(NAMES)]
            age_days = 4 + (index % 11)
            received_at = anchor - timedelta(days=age_days, hours=index % 5)
            order = Order(
                source="sim",
                shopify_order_id=f"history-{int(anchor.timestamp())}-{index}",
                customer_name=name,
                email=email,
                package=PACKAGES[index % len(PACKAGES)],
                status="delivered",
                created_at=received_at,
                sla_due_at=received_at + timedelta(hours=48),
            )
            session.add(order)
            session.flush()
            session.add(
                Photo(
                    order_id=order.id,
                    file_path="/static/sample_photos/good.jpg",
                    qc_status="pass",
                    qc_reasons=[],
                    customer_message="",
                    replaced_by=None,
                )
            )
            qc_at = received_at + timedelta(seconds=20 + (index % 30))
            events = [
                (None, "received", received_at),
                ("received", "qc", qc_at),
            ]
            cursor = qc_at + timedelta(seconds=35 + (index % 50))
            if index % 4 == 0:
                events.append(("qc", "on_hold_photo", cursor))
                reminder_total = 1 + (index % 3)
                for reminder_number in range(1, reminder_total + 1):
                    reminder_at = cursor + timedelta(hours=24 * reminder_number)
                    session.add(
                        ReminderEvent(
                            order_id=order.id,
                            reminder_number=reminder_number,
                            sent_at=reminder_at,
                            channel="demo_email",
                        )
                    )
                resumed = cursor + timedelta(hours=24 * reminder_total, minutes=25)
                events.extend([
                    ("on_hold_photo", "qc", resumed),
                    ("qc", "ready_to_print", resumed + timedelta(seconds=45)),
                ])
                cursor = resumed + timedelta(seconds=45)
            else:
                events.append(("qc", "ready_to_print", cursor))
            printed_at = cursor + timedelta(hours=2 + (index % 7))
            pressed_at = printed_at + timedelta(hours=1 + (index % 6))
            shipped_at = pressed_at + timedelta(hours=3 + (index % 11))
            delivered_at = shipped_at + timedelta(hours=28 + (index % 5) * 8)
            events.extend([
                ("ready_to_print", "printed", printed_at),
                ("printed", "pressed", pressed_at),
                ("pressed", "shipped", shipped_at),
                ("shipped", "delivered", delivered_at),
            ])
            for from_status, to_status, at in events:
                session.add(
                    StageEvent(
                        order_id=order.id,
                        from_status=from_status,
                        to_status=to_status,
                        at=at,
                    )
                )
        session.commit()
        log_event("demo_history_seeded", completed_orders=count)


def reset_demo_data() -> dict[str, int]:
    if not sim_mode():
        raise HTTPException(status_code=403, detail="Demo reset is only available in simulation mode")
    with SessionLocal() as session:
        removed = session.scalar(select(func.count()).select_from(Order))
        session.query(ReuploadToken).delete()
        session.query(ReminderEvent).delete()
        session.query(Photo).delete()
        session.query(StageEvent).delete()
        session.query(Order).delete()
        session.commit()
    for upload in UPLOAD_DIR.iterdir():
        if upload.is_file():
            upload.unlink()
    seed_count = int(os.getenv("SEED_DEMO_ORDERS", "0"))
    for index in range(seed_count):
        ingest_order(fake_shopify_payload(index + 1), "sim")
    history_count = int(os.getenv("SEED_DEMO_HISTORY", "0"))
    if history_count:
        seed_demo_history(history_count)
    log_event("demo_data_reset", removed_orders=removed, active_seeded=seed_count, history_seeded=history_count)
    return {"removed": removed, "active_seeded": seed_count, "history_seeded": history_count}


def board_context(request: Request) -> dict:
    with SessionLocal() as session:
        summary = operations_context(session, now())
        orders = summary["orders"]
        photos = session.scalars(select(Photo).where(Photo.replaced_by.is_(None))).all()
        tokens = session.scalars(select(ReuploadToken).where(ReuploadToken.used_at.is_(None), ReuploadToken.expires_at > now())).all()
    photo_by_order = {photo.order_id: photo for photo in photos}
    token_by_photo = {token.photo_id: token for token in tokens}
    columns = [
        {
            "status": stage,
            "label": STAGE_LABELS[stage],
            "orders": [order for order in orders if order.status == stage],
        }
        for stage in BOARD_STAGES
    ]
    return {
        "request": request,
        "columns": columns,
        "photos": photo_by_order,
        "tokens": token_by_photo,
        "next_stage": NEXT_STAGE,
        "manual_stages": MANUAL_STAGES,
        "stage_labels": STAGE_LABELS,
        "operations": summary,
        "stage_summary": {row["status"]: row for row in summary["stage_rows"]},
        "format_duration": format_duration,
        "incident": latest_incident(),
        "sim_mode": sim_mode(),
        "chaos_controls_enabled": chaos_controls_enabled(),
        "chaos": {
            "slow": slow_mode_active(),
            "worker_stopped": worker_task is not None and worker_task.done(),
        },
    }


@app.post("/webhooks/shopify/orders")
async def shopify_order(request: Request):
    body = await request.body()
    verified = hmac_is_valid(body, request.headers.get("X-Shopify-Hmac-Sha256"))
    if not verified and not sim_mode():
        raise HTTPException(status_code=401, detail="Invalid Shopify HMAC")
    order = ingest_order(await request.json(), "shopify", verified_source=verified)
    return {"id": order.id, "status": order.status}


@app.post("/simulate/orders")
def simulate_orders(n: int = Query(1, ge=1, le=100)):
    with SessionLocal() as session:
        existing = session.scalar(select(func.count()).select_from(Order))
    limit = int(os.getenv("MAX_SIM_ORDERS_TOTAL", "200"))
    if existing + n > limit:
        raise HTTPException(status_code=409, detail=f"Demo order limit reached ({limit})")
    orders = [ingest_order(fake_shopify_payload(random.randint(1, 999999)), "sim") for _ in range(n)]
    return {"created": len(orders), "order_ids": [order.id for order in orders]}


@app.post("/chaos/{mode}")
async def chaos(mode: str, request: Request):
    if not chaos_controls_enabled():
        raise HTTPException(status_code=403, detail="Chaos controls are not available")

    global chaos_poison_next, chaos_slow_until, worker_task
    result: dict = {"mode": mode}
    if mode == "surge":
        surge_seed = int(now().timestamp() * 1_000_000)
        orders = await asyncio.to_thread(
            lambda: [ingest_order(fake_shopify_payload(surge_seed + index), "event") for index in range(40)]
        )
        result.update({"status": "triggered", "created": len(orders)})
    elif mode == "slow":
        duration = int(os.getenv("CHAOS_SLOW_DURATION_SECONDS", "300"))
        chaos_slow_until = now() + timedelta(seconds=duration)
        result.update({"status": "active", "duration_seconds": duration})
    elif mode == "poison":
        chaos_poison_next = True
        result.update({"status": "armed", "effect": "The photo checker will stop on its next cycle"})
    elif mode == "reset":
        chaos_poison_next = False
        chaos_slow_until = None
        if worker_task is not None and worker_task.done():
            worker_task = asyncio.create_task(queue_worker())
        result.update({"status": "cleared", "worker_running": worker_task is not None and not worker_task.done()})
    else:
        raise HTTPException(status_code=404, detail="Unknown chaos mode")

    log_event("chaos_changed", mode=mode, status=result["status"])
    count("maker.chaos.triggered", tags=[f"mode:{mode}"])
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/dashboard", status_code=303)
    return result


@app.post("/webhooks/datadog")
async def datadog_alert(request: Request):
    if not datadog_webhook_is_valid(request.headers.get("X-Shopfloor-Webhook-Secret")):
        raise HTTPException(status_code=401, detail="Invalid Datadog webhook secret")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Datadog webhook payload must be a JSON object")
    incident = await asyncio.to_thread(create_incident, payload)
    log_event(
        "incident_briefing_created",
        incident_id=incident.id,
        alert_title=incident.alert_title,
        alert_status=incident.alert_status,
    )
    return {
        "incident_id": incident.id,
        "briefing": incident.briefing,
        "spoken_headline": incident.spoken_headline,
        "audio_url": incident.audio_path,
        "view_url": "/incidents/latest",
    }


@app.get("/incidents/latest", response_class=HTMLResponse)
def incident_page(request: Request):
    return templates.TemplateResponse(
        request,
        "incident.html",
        {"request": request, "incident": latest_incident()},
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", board_context(request))


@app.get("/dashboard-link", response_class=HTMLResponse)
def dashboard_link(request: Request):
    """Render the Datadog action link with safe new-tab behavior."""
    return templates.TemplateResponse(request, "dashboard_link.html")


@app.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(order_id: int, request: Request):
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        photos = session.scalars(
            select(Photo).where(Photo.order_id == order.id).order_by(Photo.id.desc())
        ).all()
        events = session.scalars(
            select(StageEvent).where(StageEvent.order_id == order.id).order_by(StageEvent.at)
        ).all()
        reminders = session.scalars(
            select(ReminderEvent).where(ReminderEvent.order_id == order.id).order_by(ReminderEvent.sent_at)
        ).all()
        state = order_stage_state(order, events, reminders, now())
    return templates.TemplateResponse(
        request,
        "order.html",
        {
            "request": request,
            "order": order,
            "photos": photos,
            "events": events,
            "reminders": reminders,
            "state": state,
            "stage_labels": STAGE_LABELS,
            "next_stage": NEXT_STAGE,
            "manual_stages": MANUAL_STAGES,
            "format_duration": format_duration,
            "format_timestamp": format_timestamp,
        },
    )


@app.get("/")
def root():
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/health")
def health():
    with SessionLocal() as session:
        qc_calls_used = real_qc_calls_used(session)
        qc_limit = int(os.getenv("MAX_REAL_QC_CALLS", "20"))
    return {
        "status": "ok",
        "worker_running": worker_task is not None and not worker_task.done(),
        "quality_check": {
            "ready": bool(os.getenv("OPENAI_API_KEY")) and qc_calls_used < qc_limit,
            "configured": bool(os.getenv("OPENAI_API_KEY")),
            "calls_used": qc_calls_used,
            "call_limit": qc_limit,
        },
        "chaos": {
            "poison_armed": chaos_poison_next,
            "slow": slow_mode_active(),
            "slow_until": chaos_slow_until.isoformat() if slow_mode_active() else None,
        },
    }


@app.post("/admin/reset-demo")
def admin_reset_demo(request: Request):
    expected = os.getenv("DEMO_ADMIN_SECRET", "")
    supplied = request.headers.get("X-Shopfloor-Admin-Secret", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Invalid demo admin secret")
    return reset_demo_data()


@app.post("/orders/{order_id}/advance", response_class=HTMLResponse)
def advance_order(order_id: int, request: Request):
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.status not in MANUAL_STAGES:
            raise HTTPException(status_code=409, detail="QC stages advance automatically")
        destination = NEXT_STAGE.get(order.status)
        if destination:
            transition(session, order, destination)
            session.commit()
    context = board_context(request)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "board.html", context)
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/orders/{order_id}/send-reminder")
def send_photo_reminder(order_id: int, request: Request):
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.status != "on_hold_photo":
            raise HTTPException(status_code=409, detail="Reminders only apply to orders awaiting a photo")
        reminder_count = session.scalar(
            select(func.count()).select_from(ReminderEvent).where(ReminderEvent.order_id == order.id)
        )
        if reminder_count >= MAX_PHOTO_REMINDERS:
            raise HTTPException(status_code=409, detail="This order now needs personal follow-up")
        reminder_number = reminder_count + 1
        session.add(
            ReminderEvent(
                order_id=order.id,
                reminder_number=reminder_number,
                sent_at=now(),
                channel="demo_email",
            )
        )
        session.commit()
        count("maker.customer.reminders_sent")
        log_event(
            "customer_photo_reminder_sent",
            order_id=order.id,
            reminder_number=reminder_number,
            order_url=str(request.base_url).rstrip("/") + f"/orders/{order.id}",
        )
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/retry-qc", response_class=HTMLResponse)
def retry_qc(order_id: int, request: Request):
    with SessionLocal() as session:
        order = session.get(Order, order_id)
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.status != "qc":
            raise HTTPException(status_code=409, detail="Only pending QC orders can be retried")
        photos = session.scalars(
            select(Photo).where(
                Photo.order_id == order.id,
                Photo.replaced_by.is_(None),
                Photo.qc_status == "pending",
            )
        ).all()
        for photo in photos:
            photo.customer_message = None
        session.commit()
    context = board_context(request)
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "board.html", context)
    return RedirectResponse("/dashboard", status_code=303)


def valid_token(token_value: str):
    with SessionLocal() as session:
        token = session.get(ReuploadToken, token_value)
        if not token or token.used_at or token.expires_at < now():
            raise HTTPException(status_code=404, detail="This re-upload link is no longer valid")
        photo = session.get(Photo, token.photo_id)
        order = session.get(Order, token.order_id)
        return token, photo, order


def max_upload_bytes() -> int:
    return int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))


def validated_image_suffix(data: bytes) -> str:
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = image.format
            width, height = image.size
            if width * height > MAX_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail="La foto tiene unas dimensiones demasiado grandes. Elige una imagen más pequeña.")
            image.verify()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(status_code=415, detail="El archivo no parece una foto válida. Sube una imagen JPEG, PNG o WebP.")
    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise HTTPException(status_code=415, detail="El formato no es compatible. Sube una imagen JPEG, PNG o WebP.")
    return ALLOWED_IMAGE_FORMATS[image_format]


def reupload_response(request: Request, token: str, status_code: int, error: str):
    try:
        _, photo, order = valid_token(token)
    except HTTPException:
        photo = None
        order = None
    return templates.TemplateResponse(
        request,
        "reupload.html",
        {"request": request, "token": token, "photo": photo, "order": order, "error": error},
        status_code=status_code,
    )


@app.get("/reupload/{token}", response_class=HTMLResponse)
def reupload_page(token: str, request: Request):
    try:
        _, photo, order = valid_token(token)
    except HTTPException:
        return reupload_response(request, token, 404, "Este enlace ya no es válido. Pide a la tienda un nuevo enlace para subir tu foto.")
    return templates.TemplateResponse(request, "reupload.html", {"request": request, "token": token, "photo": photo, "order": order, "error": None})


@app.post("/reupload/{token}")
async def reupload_photo(request: Request, token: str, file: UploadFile | None = File(None)):
    try:
        _, _, customer_order = valid_token(token)
    except HTTPException:
        return reupload_response(request, token, 404, "Este enlace ya no es válido. Pide a la tienda un nuevo enlace para subir tu foto.")
    if not file or not file.filename:
        return reupload_response(request, token, 400, "Selecciona una foto antes de enviarla.")
    data = await file.read(max_upload_bytes() + 1)
    if len(data) > max_upload_bytes():
        return reupload_response(request, token, 413, "La foto supera el límite de 10 MB. Elige una versión más pequeña.")
    try:
        suffix = validated_image_suffix(data)
    except HTTPException as exc:
        return reupload_response(request, token, exc.status_code, str(exc.detail))
    with SessionLocal() as session:
        if not real_qc_is_available(session):
            return reupload_response(request, token, 503, QC_TEMPORARILY_UNAVAILABLE_MESSAGE)
    destination = UPLOAD_DIR / f"{secrets.token_hex(12)}{suffix}"
    with SessionLocal() as session:
        upload_token = session.get(ReuploadToken, token)
        if not upload_token or upload_token.used_at or upload_token.expires_at < now():
            return reupload_response(request, token, 404, "Este enlace ya no es válido. Pide a la tienda un nuevo enlace para subir tu foto.")
        old_photo = session.get(Photo, upload_token.photo_id)
        order = session.get(Order, upload_token.order_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        new_photo = Photo(order_id=order.id, file_path=f"/uploads/{destination.name}", qc_status="pending", qc_reasons=None, customer_message=None, replaced_by=None)
        session.add(new_photo)
        session.flush()
        old_photo.replaced_by = new_photo.id
        upload_token.used_at = now()
        transition(session, order, "qc")
        try:
            session.commit()
        except Exception:
            destination.unlink(missing_ok=True)
            raise
    return templates.TemplateResponse(
        request,
        "reupload_success.html",
        {"request": request, "order": customer_order},
    )
