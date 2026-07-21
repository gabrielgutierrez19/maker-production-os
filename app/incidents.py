import json
import os
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI
from sqlalchemy import func, select

from .database import SessionLocal
from .models import Order
from .observability import log_event

INCIDENT_PROMPT = """You are the on-call SRE for a small production pipeline.
Alert payload: {alert}
Evidence: {evidence}
Return JSON only with keys "briefing" and "spoken_headline".
"briefing" must contain exactly 3 sentences: what is happening, the most likely root cause with evidence, and one concrete recommended action.
"spoken_headline" must be one short line. Sound like a helpful colleague talking out loud to a non-technical business owner, relaxed and plain, no corporate phrasing. No hedging."""


@dataclass
class Incident:
    id: str
    received_at: str
    alert_title: str
    alert_status: str
    briefing: str
    spoken_headline: str
    evidence: dict[str, Any]
    audio_path: str | None = None


incidents: deque[Incident] = deque(maxlen=20)
CLOSED_ORDER_STATUSES = {"shipped", "delivered"}
ELEVENLABS_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


def synthesize_speech(text: str) -> bytes | None:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        return None
    voice_id = os.getenv("ELEVENLABS_VOICE_ID") or ELEVENLABS_DEFAULT_VOICE_ID
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps({"text": text, "model_id": "eleven_multilingual_v2"}).encode(),
        headers={"Content-Type": "application/json", "xi-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log_event("incident_voice_failed", error_type=type(exc).__name__)
        return None


def _recent_logs(limit: int = 20) -> list[dict[str, Any]]:
    path = Path(os.getenv("LOG_PATH", "logs/app.jsonl"))
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def collect_evidence() -> dict[str, Any]:
    with SessionLocal() as session:
        status_counts = dict(session.execute(select(Order.status, func.count()).group_by(Order.status)).all())
        oldest = session.scalar(
            select(func.min(Order.created_at)).where(~Order.status.in_(CLOSED_ORDER_STATUSES))
        )
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "orders_by_status": status_counts,
        "oldest_open_order_hours": 0 if not oldest else round((now - oldest).total_seconds() / 3600, 2),
        "recent_logs": _recent_logs(),
    }


def _alert_value(payload: dict[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def simulated_briefing(payload: dict[str, Any], evidence: dict[str, Any]) -> dict[str, str]:
    title = _alert_value(payload, "title", "event_title", "alert_title", default="Shopfloor alert")
    status = _alert_value(payload, "alert_status", "status", default="alert").lower()
    oldest = evidence["oldest_open_order_hours"]
    waiting = sum(
        value
        for stage, value in evidence["orders_by_status"].items()
        if stage not in CLOSED_ORDER_STATUSES
    )
    order_word = "order" if waiting == 1 else "orders"
    normalized_title = title.lower()
    if status in {"ok", "recovered", "resolved"}:
        briefing = (
            f"Good news, {title} has recovered and Shopfloor looks healthy again. "
            f"The queue backs that up with {waiting} open {order_word} moving normally. "
            "Nothing urgent to do, just glance over the production queue before closing this out."
        )
        headline = f"Good news from Shopfloor: {title} has recovered."
    elif any(word in normalized_title for word in ("oldest", "backlog", "waiting")) and waiting == 0:
        briefing = (
            f"{title} is active, but the production queue actually has no open orders right now. "
            "This looks like a delayed metric recovery, since the live backlog age is zero hours. "
            "Refresh Datadog once, and only reach out to the technical owner if the alert stays active."
        )
        headline = f"Heads up from Shopfloor: {title}."
    elif any(word in normalized_title for word in ("worker", "heartbeat", "photo checker", "qc")):
        briefing = (
            f"{title} is {status}, which usually means the automatic photo checker has stopped. "
            f"Right now there are {len(evidence['recent_logs'])} recent app events on file and {waiting} open {order_word} waiting on it. "
            "Restart the Shopfloor app and check that the photo checker comes back green."
        )
        headline = f"Heads up from Shopfloor: {title}."
    elif any(word in normalized_title for word in ("error", "server", "request")):
        briefing = (
            f"{title} is {status}, so customers might be running into errors in Shopfloor right now. "
            f"There are {len(evidence['recent_logs'])} recent app events on file while {waiting} {order_word} sit open. "
            "Pause customer testing, open the latest error in Datadog, and bring in the technical owner."
        )
        headline = f"Heads up from Shopfloor: {title}."
    else:
        briefing = (
            f"{title} is {status}, and {waiting} {order_word} could use a look. "
            f"The likely culprit is a stalled production stage, since the oldest open order has been waiting {oldest} hours. "
            "Open the production queue, unblock that oldest order first, and call the technical owner if the alert sticks around."
        )
        headline = f"Heads up from Shopfloor: {title}."
    return {"briefing": briefing, "spoken_headline": headline}


def generate_briefing(payload: dict[str, Any], evidence: dict[str, Any]) -> dict[str, str]:
    if os.getenv("SIM_MODE", "false").lower() == "true" or not os.getenv("OPENAI_API_KEY"):
        return simulated_briefing(payload, evidence)
    response = OpenAI().responses.create(
        model="gpt-5.6-terra",
        input=INCIDENT_PROMPT.format(
            alert=json.dumps(payload, ensure_ascii=False),
            evidence=json.dumps(evidence, ensure_ascii=False),
        ),
    )
    content = response.output_text.strip().removeprefix("```json").removesuffix("```").strip()
    result = json.loads(content)
    if not result.get("briefing") or not result.get("spoken_headline"):
        raise ValueError("Incident copilot returned an invalid briefing")
    return result


def create_incident(payload: dict[str, Any]) -> Incident:
    received_at = datetime.now(UTC)
    evidence = collect_evidence()
    result = generate_briefing(payload, evidence)
    incident = Incident(
        id=received_at.strftime("%Y%m%d%H%M%S%f"),
        received_at=received_at.isoformat(),
        alert_title=_alert_value(payload, "title", "event_title", "alert_title", default="Shopfloor alert"),
        alert_status=_alert_value(payload, "alert_status", "status", default="alert"),
        briefing=result["briefing"],
        spoken_headline=result["spoken_headline"],
        evidence=evidence,
    )
    audio = synthesize_speech(f"{incident.spoken_headline} {incident.briefing}")
    if audio:
        audio_dir = Path(os.getenv("UPLOAD_DIR", "app/static/uploads"))
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / f"incident-{incident.id}.mp3").write_bytes(audio)
        incident.audio_path = f"/uploads/incident-{incident.id}.mp3"
    incidents.appendleft(incident)
    return incident


def latest_incident() -> dict[str, Any] | None:
    return asdict(incidents[0]) if incidents else None
