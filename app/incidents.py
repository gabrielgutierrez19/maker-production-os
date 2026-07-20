import json
import os
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI
from sqlalchemy import func, select

from .database import SessionLocal
from .models import Order

INCIDENT_PROMPT = """You are the on-call SRE for a small production pipeline.
Alert payload: {alert}
Evidence: {evidence}
Return JSON only with keys "briefing" and "spoken_headline".
"briefing" must contain exactly 3 sentences: what is happening, the most likely root cause with evidence, and one concrete recommended action.
"spoken_headline" must be one short line. Use plain language for a non-technical business owner. No hedging."""


@dataclass
class Incident:
    id: str
    received_at: str
    alert_title: str
    alert_status: str
    briefing: str
    spoken_headline: str
    evidence: dict[str, Any]


incidents: deque[Incident] = deque(maxlen=20)


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
        oldest = session.scalar(select(func.min(Order.created_at)).where(Order.status != "shipped"))
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
    waiting = sum(value for stage, value in evidence["orders_by_status"].items() if stage != "shipped")
    order_word = "order" if waiting == 1 else "orders"
    normalized_title = title.lower()
    if status in {"ok", "recovered", "resolved"}:
        briefing = (
            f"{title} has recovered and Shopfloor is reporting normal service. "
            f"The recovery is supported by the current queue of {waiting} open {order_word}. "
            "No immediate action is required, but review the production queue before closing the incident."
        )
    elif any(word in normalized_title for word in ("oldest", "backlog", "waiting")) and waiting == 0:
        briefing = (
            f"{title} is active, but the current production queue has no open orders. "
            "The most likely cause is a delayed metric recovery because the live backlog age is zero hours. "
            "Refresh Datadog once, and contact the technical owner only if the alert remains active."
        )
    elif any(word in normalized_title for word in ("worker", "heartbeat", "photo checker", "qc")):
        briefing = (
            f"{title} is {status}, so automatic photo checking may have stopped. "
            f"The local evidence includes {len(evidence['recent_logs'])} recent app events and {waiting} open {order_word}. "
            "Restart the Shopfloor app, then confirm that Photo checker running returns to green."
        )
    elif any(word in normalized_title for word in ("error", "server", "request")):
        briefing = (
            f"{title} is {status}, so customers may be seeing failures in Shopfloor. "
            f"The local evidence includes {len(evidence['recent_logs'])} recent app events while {waiting} {order_word} remain open. "
            "Stop customer testing, open the latest APM error, and contact the technical owner."
        )
    else:
        briefing = (
            f"{title} is {status}, and {waiting} {order_word} currently need attention. "
            f"The most likely cause is a stalled production stage because the oldest open order has waited {oldest} hours. "
            "Open the production queue, unblock the oldest order first, and contact the technical owner if the alert remains active."
        )
    return {"briefing": briefing, "spoken_headline": f"Shopfloor needs attention: {title}."}


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
    incidents.appendleft(incident)
    return incident


def latest_incident() -> dict[str, Any] | None:
    return asdict(incidents[0]) if incidents else None
