import asyncio
import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import httpx
import pytest

import app.main as main
from app.database import SessionLocal
from app.main import app, ingest_order, process_queue
from app.models import Order, Photo, ReminderEvent, ReuploadToken, StageEvent
from app.operations import business_seconds_between

VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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


def test_shopify_created_at_drives_the_first_response_target(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    created_at = main.now() - timedelta(minutes=3)
    payload = order_payload(124, "/static/uploads/customer-upload.png")
    payload["created_at"] = created_at.replace(tzinfo=UTC).isoformat()

    order = ingest_order(payload, "shopify")
    process_queue()

    with SessionLocal() as session:
        stored = session.get(Order, order.id)
        summary = main.operations_context(session, main.now())
        state = summary["states"][order.id]
        assert stored.created_at == created_at
        assert stored.status == "qc"
        assert state["state"] == "overdue"
        assert state["target_seconds"] == 120


def test_failed_qc_issues_a_token_and_a_replacement_releases_the_order(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)
    order = ingest_order(order_payload(456, "/static/sample_photos/blurry.svg"), "sim")
    process_queue()

    with SessionLocal() as session:
        held = session.get(Order, order.id)
        failed_photo = session.query(Photo).filter_by(order_id=order.id, replaced_by=None).one()
        token = session.query(ReuploadToken).filter_by(photo_id=failed_photo.id, used_at=None).one()
        assert held.status == "on_hold_photo"
        assert failed_photo.qc_status == "fail"

    uploaded = request("POST", f"/reupload/{token.token}", files={"file": ("replacement.png", VALID_PNG, "image/png")})
    assert uploaded.status_code == 200
    assert "Foto recibida" in uploaded.text
    assert "/dashboard" not in uploaded.text
    monkeypatch.setattr(main, "qc_result", lambda _: {"verdict": "pass", "reasons": [], "customer_message": ""})
    process_queue()

    with SessionLocal() as session:
        released = session.get(Order, order.id)
        active_photo = session.query(Photo).filter_by(order_id=order.id, replaced_by=None).one()
        used_token = session.get(ReuploadToken, token.token)
        assert released.status == "ready_to_print"
        assert active_photo.qc_status == "pass"
        assert used_token.used_at is not None


def test_reupload_rejects_invalid_token_before_writing_a_file(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)

    uploaded = request("POST", "/reupload/not-a-real-token", files={"file": ("replacement.png", VALID_PNG, "image/png")})

    assert uploaded.status_code == 404
    assert "Este enlace ya no es válido" in uploaded.text
    assert not list(tmp_path.iterdir())


def test_reupload_rejects_non_image_content(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)
    order = ingest_order(order_payload(457, "/static/sample_photos/blurry.svg"), "sim")
    process_queue()
    with SessionLocal() as session:
        token = session.query(ReuploadToken).filter_by(order_id=order.id, used_at=None).one()

    uploaded = request("POST", f"/reupload/{token.token}", files={"file": ("fake.png", b"not-an-image", "image/png")})

    assert uploaded.status_code == 415
    assert "no parece una foto válida" in uploaded.text
    assert not list(tmp_path.iterdir())
    with SessionLocal() as session:
        assert session.get(ReuploadToken, token.token).used_at is None


def test_reupload_rejects_oversized_content(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "8")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)
    order = ingest_order(order_payload(458, "/static/sample_photos/blurry.svg"), "sim")
    process_queue()
    with SessionLocal() as session:
        token = session.query(ReuploadToken).filter_by(order_id=order.id, used_at=None).one()

    uploaded = request("POST", f"/reupload/{token.token}", files={"file": ("large.png", VALID_PNG, "image/png")})

    assert uploaded.status_code == 413
    assert "supera el límite de 10 MB" in uploaded.text
    assert not list(tmp_path.iterdir())


def test_reupload_without_a_file_shows_a_friendly_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path)
    order = ingest_order(order_payload(459, "/static/sample_photos/blurry.svg"), "sim")
    process_queue()
    with SessionLocal() as session:
        token = session.query(ReuploadToken).filter_by(order_id=order.id, used_at=None).one()

    uploaded = request("POST", f"/reupload/{token.token}")

    assert uploaded.status_code == 400
    assert "Selecciona una foto" in uploaded.text
    with SessionLocal() as session:
        assert session.get(ReuploadToken, token.token).used_at is None


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
        assert photo.customer_message == main.QC_NOT_CONFIGURED_MESSAGE


def test_real_qc_cap_stops_an_unknown_upload_before_the_api_call(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("MAX_REAL_QC_CALLS", "0")
    monkeypatch.setattr(main, "qc_result", lambda _: (_ for _ in ()).throw(AssertionError("API must not be called after the cap")))
    order = ingest_order(order_payload(987, "/static/uploads/customer-upload.png"), "sim")
    process_queue()

    with SessionLocal() as session:
        awaiting_qc = session.get(Order, order.id)
        photo = session.query(Photo).filter_by(order_id=order.id, replaced_by=None).one()
        assert awaiting_qc.status == "qc"
        assert photo.customer_message == main.QC_LIMIT_MESSAGE


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


def test_business_metrics_include_status_funnel_and_qc_denominator(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    counts = []
    gauges = []
    monkeypatch.setattr(main, "count", lambda metric, value=1, tags=None: counts.append((metric, value, tags)))
    monkeypatch.setattr(main, "gauge", lambda metric, value, tags=None: gauges.append((metric, value, tags)))
    ingest_order(order_payload(852, "/static/sample_photos/good.svg"), "sim")

    process_queue()

    assert ("maker.qc.inspected", 1, None) in counts
    funnel = {
        tags[0].split(":", 1)[1]: value
        for metric, value, tags in gauges
        if metric == "maker.orders.by_status"
    }
    assert funnel == {
        "received": 0,
        "qc": 0,
        "on_hold_photo": 0,
        "ready_to_print": 1,
        "printed": 0,
        "pressed": 0,
        "shipped": 0,
        "delivered": 0,
    }


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


def test_datadog_webhook_creates_an_owner_briefing_in_sim_mode(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    ingest_order(order_payload(901, "/static/sample_photos/good.svg"), "sim")

    response = request(
        "POST",
        "/webhooks/datadog",
        json={"title": "Oldest order alert", "alert_status": "Alert"},
    )

    assert response.status_code == 200
    result = response.json()
    assert result["view_url"] == "/incidents/latest"
    assert "Oldest order alert" in result["briefing"]
    assert len(result["briefing"].removesuffix(".").split(". ")) == 3

    page = request("GET", "/incidents/latest")
    dashboard = request("GET", "/dashboard")
    assert page.status_code == 200
    assert "What is happening and what to do" in page.text
    assert result["spoken_headline"] in page.text
    assert "Review incident" in dashboard.text


def test_datadog_webhook_rejects_non_object_json(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")

    response = request("POST", "/webhooks/datadog", json=["not", "an", "object"])

    assert response.status_code == 400


def test_datadog_webhook_requires_its_secret_outside_sim_mode(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "false")
    monkeypatch.setenv("DATADOG_WEBHOOK_SECRET", "datadog-test-secret")
    payload = {"title": "Worker stopped", "alert_status": "Alert"}

    rejected = request("POST", "/webhooks/datadog", json=payload)
    accepted = request(
        "POST",
        "/webhooks/datadog",
        json=payload,
        headers={"X-Shopfloor-Webhook-Secret": "datadog-test-secret"},
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200


def test_configured_datadog_secret_is_enforced_in_sim_mode(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setenv("DATADOG_WEBHOOK_SECRET", "configured-secret")

    rejected = request(
        "POST",
        "/webhooks/datadog",
        json={"title": "Worker stopped", "alert_status": "Alert"},
    )
    accepted = request(
        "POST",
        "/webhooks/datadog",
        json={"title": "Worker stopped", "alert_status": "Alert"},
        headers={"X-Shopfloor-Webhook-Secret": "configured-secret"},
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200


def test_oldest_order_alert_with_an_empty_queue_recommends_metric_refresh(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")

    response = request(
        "POST",
        "/webhooks/datadog",
        json={"title": "Oldest order alert", "alert_status": "Alert"},
    )

    briefing = response.json()["briefing"]
    assert "no open orders" in briefing
    assert "delayed metric recovery" in briefing
    assert "Refresh Datadog" in briefing


def test_chaos_controls_are_blocked_outside_sim_mode(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "false")

    response = request("POST", "/chaos/surge")

    assert response.status_code == 403


def test_chaos_controls_can_be_disabled_for_a_public_sim_demo(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setenv("ENABLE_CHAOS_CONTROLS", "false")

    response = request("POST", "/chaos/poison")
    dashboard = request("GET", "/dashboard")

    assert response.status_code == 403
    assert "Demo controls" not in dashboard.text


def test_surge_creates_40_event_orders(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")

    response = request("POST", "/chaos/surge")

    assert response.status_code == 200
    assert response.json()["created"] == 40
    with SessionLocal() as session:
        assert session.query(Order).filter_by(source="event").count() == 40


def test_slow_mode_is_visible_and_resettable(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setenv("CHAOS_SLOW_SECONDS", "0")
    monkeypatch.setenv("CHAOS_SLOW_DURATION_SECONDS", "60")

    slowed = request("POST", "/chaos/slow")
    health = request("GET", "/health")
    dashboard = request("GET", "/dashboard")
    reset = request("POST", "/chaos/reset")

    assert slowed.json()["duration_seconds"] == 60
    assert health.json()["chaos"]["slow"] is True
    assert "Test active" in dashboard.text
    assert reset.json()["status"] == "cleared"
    assert request("GET", "/health").json()["chaos"]["slow"] is False


def test_poison_stops_the_worker_on_its_next_cycle(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    armed = request("POST", "/chaos/poison")

    async def run_poisoned_worker():
        with pytest.raises(RuntimeError, match="Chaos poison upload"):
            await main.queue_worker()

    assert armed.json()["status"] == "armed"
    asyncio.run(run_poisoned_worker())
    assert main.chaos_poison_next is False


def test_public_root_redirects_to_the_dashboard():
    response = request("GET", "/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"


def test_public_simulation_has_a_total_order_cap(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setenv("MAX_SIM_ORDERS_TOTAL", "1")
    ingest_order(order_payload(1001, "/static/sample_photos/good.svg"), "sim")

    response = request("POST", "/simulate/orders?n=1")

    assert response.status_code == 409
    assert response.json()["detail"] == "Demo order limit reached (1)"


def test_business_clock_counts_only_weekdays_between_nine_and_six():
    friday_at_five = datetime(2026, 7, 17, 15, 0, tzinfo=UTC).replace(tzinfo=None)
    monday_at_ten = datetime(2026, 7, 20, 8, 0, tzinfo=UTC).replace(tzinfo=None)

    assert business_seconds_between(friday_at_five, monday_at_ten) == 2 * 60 * 60


def test_photo_reminders_are_sent_once_per_24_hours_and_stop_after_three(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    order = ingest_order(order_payload(1101, "/static/sample_photos/blurry.svg"), "sim")
    process_queue()

    with SessionLocal() as session:
        held_at = session.query(StageEvent).filter_by(order_id=order.id, to_status="on_hold_photo").one()
        held_at.at = main.now() - timedelta(hours=25)
        session.commit()

    process_queue()
    process_queue()
    with SessionLocal() as session:
        reminders = session.query(ReminderEvent).filter_by(order_id=order.id).all()
        assert [item.reminder_number for item in reminders] == [1]

    for expected in (2, 3):
        with SessionLocal() as session:
            latest = session.query(ReminderEvent).filter_by(order_id=order.id).order_by(ReminderEvent.sent_at.desc()).first()
            latest.sent_at = main.now() - timedelta(hours=25)
            session.commit()
        process_queue()
        with SessionLocal() as session:
            assert session.query(ReminderEvent).filter_by(order_id=order.id).count() == expected

    with SessionLocal() as session:
        latest = session.query(ReminderEvent).filter_by(order_id=order.id).order_by(ReminderEvent.sent_at.desc()).first()
        latest.sent_at = main.now() - timedelta(hours=25)
        session.commit()
    process_queue()
    with SessionLocal() as session:
        assert session.query(ReminderEvent).filter_by(order_id=order.id).count() == 3

    detail = request("GET", f"/orders/{order.id}")
    assert "Personal follow-up needed" in detail.text
    assert "3 of 3" in detail.text


def test_order_detail_is_linked_from_the_queue_and_uses_plain_quality_language(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    order = ingest_order(order_payload(1102, "/static/sample_photos/good.svg"), "sim")
    process_queue()

    dashboard = request("GET", "/dashboard")
    detail = request("GET", f"/orders/{order.id}")

    assert f'/orders/{order.id}' in dashboard.text
    assert "Quality check" in detail.text
    assert "Production timeline" in detail.text


def test_shipped_order_can_be_marked_delivered(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    order = ingest_order(order_payload(1103, "/static/sample_photos/good.svg"), "sim")
    process_queue()
    for _ in range(3):
        request("POST", f"/orders/{order.id}/advance")

    with SessionLocal() as session:
        assert session.get(Order, order.id).status == "shipped"

    delivered = request("POST", f"/orders/{order.id}/advance")
    assert delivered.status_code == 303
    with SessionLocal() as session:
        updated = session.get(Order, order.id)
        event = session.query(StageEvent).filter_by(order_id=order.id, to_status="delivered").one()
        assert updated.status == "delivered"
        assert event.from_status == "shipped"


def test_demo_history_populates_seven_day_production_and_delivery_metrics(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    main.seed_demo_history(16)

    dashboard = request("GET", "/dashboard")

    assert "Median production cycle" in dashboard.text
    assert "Average shipped-to-delivered time" in dashboard.text
    assert "Not available" not in dashboard.text
    with SessionLocal() as session:
        assert session.query(Order).filter_by(status="delivered").count() == 16


def test_hosted_snapshot_uses_aggregate_metrics_and_order_logs_for_deep_links(monkeypatch):
    monkeypatch.setenv("SIM_MODE", "true")
    monkeypatch.setenv("APP_BASE_URL", "https://shopfloor.example")
    metric_batches = []
    log_batches = []
    monkeypatch.setattr(main, "publish_metrics", lambda points: metric_batches.append(points))
    monkeypatch.setattr(main, "publish_order_logs", lambda items: log_batches.append(items))
    main.last_http_publish_at = None
    order = ingest_order(order_payload(1104, "/static/sample_photos/blurry.svg"), "sim")
    process_queue()
    with SessionLocal() as session:
        event = session.query(StageEvent).filter_by(order_id=order.id, to_status="on_hold_photo").one()
        event.at = main.now() - timedelta(hours=25)
        session.commit()
        summary = main.operations_context(session, main.now())

    main.last_http_publish_at = None
    main.publish_hosted_snapshot(summary, main.now())

    assert any(metric == "maker.orders.by_status" for metric, _, _ in metric_batches[-1])
    assert all(not any(tag.startswith("order_id:") for tag in (tags or [])) for _, _, tags in metric_batches[-1])
    assert log_batches[-1][0]["order_id"] == order.id
    assert log_batches[-1][0]["order_url"] == f"https://shopfloor.example/orders/{order.id}"
    assert "customer_name" not in log_batches[-1][0]
