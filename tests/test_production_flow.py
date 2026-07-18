import asyncio
import base64
import hashlib
import hmac

import httpx

import app.main as main
from app.database import SessionLocal
from app.main import app, ingest_order, process_queue
from app.models import Order, Photo, ReuploadToken, StageEvent


def request(method: str, path: str, **kwargs) -> httpx.Response:
    async def send():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def order_payload(order_id: int, image: str) -> dict:
    return {
        "id": order_id,
        "email": "sofia@example.com",
        "customer": {"first_name": "Sofía", "last_name": "Martín"},
        "line_items": [{"title": "9 imanes personalizados", "properties": [{"name": "Customer photo upload", "value": image}]}],
    }


def test_shopify_webhook_requires_a_valid_hmac(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "false")
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", "test-secret")
    body = b'{"id":123,"line_items":[]}'
    signature = base64.b64encode(hmac.new(b"test-secret", body, hashlib.sha256).digest()).decode()

    accepted = request("POST", "/webhooks/shopify/orders", content=body, headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": signature})
    rejected = request("POST", "/webhooks/shopify/orders", content=body, headers={"Content-Type": "application/json", "X-Shopify-Hmac-Sha256": "bad"})

    assert accepted.status_code == 200
    assert rejected.status_code == 401


def test_failed_qc_issues_a_token_and_a_replacement_releases_the_order(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    order = ingest_order(order_payload(456, "/static/sample_photos/blurry.svg"), "sim")
    process_queue()

    with SessionLocal() as session:
        held = session.get(Order, order.id)
        failed_photo = session.query(Photo).filter_by(order_id=order.id, replaced_by=None).one()
        token = session.query(ReuploadToken).filter_by(photo_id=failed_photo.id, used_at=None).one()
        assert held.status == "on_hold_photo"
        assert failed_photo.qc_status == "fail"

    uploaded = request("POST", f"/reupload/{token.token}", files={"file": ("replacement.png", b"image-bytes", "image/png")})
    assert uploaded.status_code == 303
    monkeypatch.setattr(main, "qc_result", lambda _: {"verdict": "pass", "reasons": [], "customer_message": ""})
    process_queue()

    with SessionLocal() as session:
        released = session.get(Order, order.id)
        active_photo = session.query(Photo).filter_by(order_id=order.id, replaced_by=None).one()
        used_token = session.get(ReuploadToken, token.token)
        assert released.status == "ready_to_print"
        assert active_photo.qc_status == "pass"
        assert used_token.used_at is not None


def test_unknown_upload_stays_pending_without_an_openai_key(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    order = ingest_order(order_payload(654, "/static/uploads/customer-upload.png"), "sim")
    process_queue()

    with SessionLocal() as session:
        awaiting_qc = session.get(Order, order.id)
        photo = session.query(Photo).filter_by(order_id=order.id, replaced_by=None).one()
        assert awaiting_qc.status == "qc"
        assert photo.qc_status == "pending"
        assert photo.customer_message == "Pendiente de revisión visual automática."


def test_real_qc_cap_stops_an_unknown_upload_before_the_api_call(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MAX_REAL_QC_CALLS", "0")
    monkeypatch.setattr(main, "qc_result", lambda _: (_ for _ in ()).throw(AssertionError("API must not be called after the cap")))
    order = ingest_order(order_payload(987, "/static/uploads/customer-upload.png"), "sim")
    process_queue()

    with SessionLocal() as session:
        awaiting_qc = session.get(Order, order.id)
        assert awaiting_qc.status == "qc"


def test_qc_error_pauses_automatic_retries_until_manually_released(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MAX_REAL_QC_CALLS", "20")
    attempts = 0

    def failing_qc(_):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("temporary provider failure")

    monkeypatch.setattr(main, "qc_result", failing_qc)
    order = ingest_order(order_payload(741, "/static/uploads/customer-upload.png"), "sim")

    process_queue()
    process_queue()

    with SessionLocal() as session:
        paused = session.get(Order, order.id)
        photo = session.query(Photo).filter_by(order_id=order.id, replaced_by=None).one()
        assert paused.status == "qc"
        assert photo.qc_status == "pending"
        assert photo.customer_message == main.QC_ERROR_MESSAGE
    assert attempts == 1

    retried = request("POST", f"/orders/{order.id}/retry-qc")
    assert retried.status_code == 303
    process_queue()
    assert attempts == 2


def test_manual_stage_advance_records_an_event(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    order = ingest_order(order_payload(789, "/static/sample_photos/good.svg"), "sim")
    process_queue()

    advanced = request("POST", f"/orders/{order.id}/advance")
    assert advanced.status_code == 303

    with SessionLocal() as session:
        updated = session.get(Order, order.id)
        events = session.query(StageEvent).filter_by(order_id=order.id).order_by(StageEvent.id).all()
        assert updated.status == "printed"
        assert (events[-1].from_status, events[-1].to_status) == ("ready_to_print", "printed")
