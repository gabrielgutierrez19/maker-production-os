import base64
import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import random
import secrets
from io import BytesIO
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select

from .database import SessionLocal, init_db
from .incidents import create_incident, latest_incident
from .models import Order, Photo, ReuploadToken, StageEvent
from .observability import count, gauge, histogram, log_event

load_dotenv()

app = FastAPI(title="Shopfloor")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

STAGES = ["received", "qc", "on_hold_photo", "ready_to_print", "printed", "pressed", "shipped"]
NEXT_STAGE = {
    "received": "qc",
    "qc": "ready_to_print",
    "on_hold_photo": "qc",
    "ready_to_print": "printed",
    "printed": "pressed",
    "pressed": "shipped",
}
MANUAL_STAGES = {"ready_to_print", "printed", "pressed"}
SAMPLE_PHOTOS = ["good.svg", "blurry.svg", "low-res.svg", "face-near-edge.svg"]
NAMES = [("Sofía Martín", "sofia@example.com"), ("Lucas Pérez", "lucas@example.com"), ("Elena Ruiz", "elena@example.com"), ("Mateo García", "mateo@example.com")]
PACKAGES = ["9 imanes personalizados", "12 imanes personalizados", "24 imanes personalizados"]
QC_PROMPT = """You are the print-quality inspector for a shop that heat-presses customer photos onto 50mm magnets. Judge this photo for printability at that size: sharpness (especially faces), effective resolution for a 50x50mm print, exposure, and crop risk. Return JSON only: {\"verdict\": \"pass\"|\"fail\", \"reasons\": [...], \"customer_message\": \"...\"}. customer_message is a warm, plain-Spanish, one-sentence explanation with a concrete suggestion."""
QC_NOT_CONFIGURED_MESSAGE = "El QC automático no está configurado. Añade la clave de OpenAI y pulsa «Reintentar QC»."
QC_LIMIT_MESSAGE = "Se alcanzó el límite de revisiones automáticas. Amplía el límite o revisa la foto manualmente."
QC_ERROR_MESSAGE = "La revisión automática falló. Pulsa «Reintentar QC» para intentarlo de nuevo."
UPLOAD_DIR = Path("app/static/uploads")
ALLOWED_IMAGE_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
MAX_IMAGE_PIXELS = 25_000_000
worker_task: asyncio.Task | None = None


@app.on_event("startup")
async def startup() -> None:
    init_db()
    global worker_task
    worker_task = asyncio.create_task(queue_worker())


@app.on_event("shutdown")
async def shutdown() -> None:
    if worker_task:
        worker_task.cancel()


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def hmac_is_valid(body: bytes, signature: str | None) -> bool:
    if os.getenv("SIM_MODE", "false").lower() == "true":
        return True
    secret = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")
    if not secret or not signature:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


def datadog_webhook_is_valid(secret: str | None) -> bool:
    if os.getenv("SIM_MODE", "false").lower() == "true":
        return True
    expected = os.getenv("DATADOG_WEBHOOK_SECRET", "")
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
    log_event("stage_transition", order_id=order.id, from_status=previous, to_status=destination)


def sample_qc(file_path: str) -> dict:
    if "blurry" in file_path:
        return {"verdict": "fail", "reasons": ["La foto está borrosa, especialmente en la cara."], "customer_message": "La foto se ve borrosa; ¿podrías subir otra más nítida y tomada con buena luz?"}
    if "low-res" in file_path:
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

    if file_path.startswith("/static/"):
        local_file = Path("app") / file_path.lstrip("/")
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
                calls_used = session.scalar(select(func.count()).select_from(Photo).where(~Photo.file_path.startswith("/static/sample_photos/"), Photo.qc_status.in_(("pass", "fail"))))
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
                log_event("qc_error", order_id=order.id, error_type=type(exc).__name__)
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
        oldest = session.scalar(select(func.min(Order.created_at)).where(Order.status != "shipped"))
        status_counts = dict(session.execute(select(Order.status, func.count()).group_by(Order.status)).all())
    gauge("maker.backlog.oldest_order_age_hours", 0 if not oldest else (now() - oldest).total_seconds() / 3600)
    for stage in STAGES:
        gauge("maker.orders.by_status", status_counts.get(stage, 0), tags=[f"status:{stage}"])
    gauge("maker.qc.worker.heartbeat", 1)


async def queue_worker() -> None:
    while True:
        await asyncio.to_thread(process_queue)
        await asyncio.sleep(2)


def photo_urls(payload: dict) -> list[str]:
    urls: list[str] = []
    for item in payload.get("line_items", []):
        for prop in item.get("properties") or []:
            name, value = str(prop.get("name", "")).lower(), str(prop.get("value", ""))
            if value.startswith(("http://", "https://", "/static/")) and any(word in name for word in ("photo", "image", "upload")):
                urls.append(value)
    return urls or ["/static/sample_photos/good.svg"]


def ingest_order(payload: dict, source: str) -> Order:
    shopify_id = str(payload.get("id")) if payload.get("id") is not None else None
    with SessionLocal() as session:
        if shopify_id and session.scalar(select(Order).where(Order.shopify_order_id == shopify_id)):
            return session.scalar(select(Order).where(Order.shopify_order_id == shopify_id))
        customer = payload.get("customer") or {}
        name = " ".join(filter(None, [customer.get("first_name"), customer.get("last_name")])).strip() or payload.get("name", "Cliente")
        order = Order(source=source, shopify_order_id=shopify_id, customer_name=name, email=payload.get("email") or customer.get("email"), package=(payload.get("line_items") or [{"title": "Pedido personalizado"}])[0].get("title", "Pedido personalizado"), status="received", created_at=now(), sla_due_at=now() + timedelta(hours=48))
        session.add(order)
        session.flush()
        session.add(StageEvent(order_id=order.id, from_status=None, to_status="received", at=now()))
        for path in photo_urls(payload):
            session.add(Photo(order_id=order.id, file_path=path, qc_status="pending", qc_reasons=None, customer_message=None, replaced_by=None))
        session.commit()
        session.refresh(order)
        count("maker.orders.created", tags=[f"source:{source}"])
        log_event("order_created", order_id=order.id, source=source)
        return order


def fake_shopify_payload(number: int) -> dict:
    name, email = random.choice(NAMES)
    first, last = name.split(" ", 1)
    photo = random.choice(SAMPLE_PHOTOS)
    return {"id": 900000 + number, "name": f"#{9000 + number}", "email": email, "created_at": datetime.now(UTC).isoformat(), "customer": {"first_name": first, "last_name": last, "email": email}, "line_items": [{"title": random.choice(PACKAGES), "quantity": 1, "properties": [{"name": "Customer photo upload", "value": f"/static/sample_photos/{photo}"}]}]}


def board_context(request: Request) -> dict:
    with SessionLocal() as session:
        orders = session.scalars(select(Order).order_by(Order.created_at.desc())).all()
        photos = session.scalars(select(Photo).where(Photo.replaced_by.is_(None))).all()
        tokens = session.scalars(select(ReuploadToken).where(ReuploadToken.used_at.is_(None), ReuploadToken.expires_at > now())).all()
    photo_by_order = {photo.order_id: photo for photo in photos}
    token_by_photo = {token.photo_id: token for token in tokens}
    columns = [{"status": stage, "orders": [order for order in orders if order.status == stage]} for stage in STAGES]
    return {
        "request": request,
        "columns": columns,
        "photos": photo_by_order,
        "tokens": token_by_photo,
        "next_stage": NEXT_STAGE,
        "manual_stages": MANUAL_STAGES,
        "incident": latest_incident(),
    }


@app.post("/webhooks/shopify/orders")
async def shopify_order(request: Request):
    body = await request.body()
    if not hmac_is_valid(body, request.headers.get("X-Shopify-Hmac-Sha256")):
        raise HTTPException(status_code=401, detail="Invalid Shopify HMAC")
    order = ingest_order(await request.json(), "shopify")
    return {"id": order.id, "status": order.status}


@app.post("/simulate/orders")
def simulate_orders(n: int = Query(1, ge=1, le=100)):
    orders = [ingest_order(fake_shopify_payload(random.randint(1, 999999)), "sim") for _ in range(n)]
    return {"created": len(orders), "order_ids": [order.id for order in orders]}


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


@app.get("/health")
def health():
    return {"status": "ok", "worker_running": worker_task is not None and not worker_task.done()}


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
    destination = UPLOAD_DIR / f"{secrets.token_hex(12)}{suffix}"
    with SessionLocal() as session:
        upload_token = session.get(ReuploadToken, token)
        if not upload_token or upload_token.used_at or upload_token.expires_at < now():
            return reupload_response(request, token, 404, "Este enlace ya no es válido. Pide a la tienda un nuevo enlace para subir tu foto.")
        old_photo = session.get(Photo, upload_token.photo_id)
        order = session.get(Order, upload_token.order_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        new_photo = Photo(order_id=order.id, file_path=f"/static/uploads/{destination.name}", qc_status="pending", qc_reasons=None, customer_message=None, replaced_by=None)
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
