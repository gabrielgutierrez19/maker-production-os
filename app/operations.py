from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from statistics import mean, median
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .models import Order, ReminderEvent, StageEvent


SHOP_TIMEZONE = ZoneInfo("Europe/Madrid")
BUSINESS_OPEN = time(9, 0)
BUSINESS_CLOSE = time(18, 0)
STAGE_LABELS = {
    "received": "Received",
    "qc": "Quality check",
    "on_hold_photo": "On hold photo",
    "ready_to_print": "Ready to print",
    "printed": "Printed",
    "pressed": "Pressed",
    "shipped": "Shipped",
    "delivered": "Delivered",
}
STAGE_TARGETS = {
    "received": (2 * 60, "continuous"),
    "qc": (2 * 60, "first_response"),
    "on_hold_photo": (24 * 60 * 60, "continuous"),
    "ready_to_print": (6 * 60 * 60, "business"),
    "printed": (6 * 60 * 60, "business"),
    "pressed": (12 * 60 * 60, "business"),
    "shipped": (48 * 60 * 60, "continuous"),
}
PRODUCTION_GREEN_SECONDS = 24 * 60 * 60
PRODUCTION_WARNING_SECONDS = 36 * 60 * 60
MAX_PHOTO_REMINDERS = 3


def utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def madrid_aware(value: datetime) -> datetime:
    return utc_naive(value).replace(tzinfo=UTC).astimezone(SHOP_TIMEZONE)


def business_seconds_between(start: datetime, end: datetime) -> float:
    if end <= start:
        return 0.0
    local_start = madrid_aware(start)
    local_end = madrid_aware(end)
    total = 0.0
    cursor: date = local_start.date()
    while cursor <= local_end.date():
        if cursor.weekday() < 5:
            opening = datetime.combine(cursor, BUSINESS_OPEN, SHOP_TIMEZONE)
            closing = datetime.combine(cursor, BUSINESS_CLOSE, SHOP_TIMEZONE)
            interval_start = max(local_start, opening)
            interval_end = min(local_end, closing)
            if interval_end > interval_start:
                total += (interval_end - interval_start).total_seconds()
        cursor += timedelta(days=1)
    return total


def elapsed_seconds(start: datetime, end: datetime) -> float:
    return max(0.0, (utc_naive(end) - utc_naive(start)).total_seconds())


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "Not available"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes / 60
    if hours <= 48:
        return f"{hours:.1f}h".replace(".0h", "h")
    return f"{hours / 24:.1f}d".replace(".0d", "d")


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "Not yet"
    return madrid_aware(value).strftime("%d %b %Y · %H:%M")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def stage_entries(events: list[StageEvent]) -> dict[str, datetime]:
    result: dict[str, datetime] = {}
    for event in sorted(events, key=lambda item: item.at):
        result[event.to_status] = event.at
    return result


def stage_intervals(events: list[StageEvent], end: datetime) -> list[tuple[str, datetime, datetime]]:
    ordered = sorted(events, key=lambda item: item.at)
    intervals: list[tuple[str, datetime, datetime]] = []
    for index, event in enumerate(ordered):
        interval_end = ordered[index + 1].at if index + 1 < len(ordered) else end
        intervals.append((event.to_status, event.at, interval_end))
    return intervals


def current_stage_started(order: Order, events: list[StageEvent]) -> datetime:
    matching = [event.at for event in events if event.to_status == order.status]
    return max(matching) if matching else order.created_at


def reminder_state(
    order: Order,
    stage_started: datetime,
    reminders: list[ReminderEvent],
    at: datetime,
) -> dict:
    sent = sorted(reminders, key=lambda item: item.sent_at)
    last_sent = sent[-1].sent_at if sent else None
    reference = last_sent or stage_started
    seconds = elapsed_seconds(reference, at)
    count = len(sent)
    personal_follow_up = count >= MAX_PHOTO_REMINDERS
    state = "attention" if personal_follow_up or seconds > STAGE_TARGETS["on_hold_photo"][0] else "healthy"
    if personal_follow_up:
        message = "Personal follow-up needed"
    elif last_sent:
        message = f"Reminder {count} sent {format_duration(elapsed_seconds(last_sent, at))} ago"
    else:
        message = f"First reminder due in {format_duration(max(0, STAGE_TARGETS['on_hold_photo'][0] - seconds))}"
    return {
        "state": state,
        "elapsed_seconds": seconds,
        "reminder_count": count,
        "last_reminder_at": last_sent,
        "personal_follow_up": personal_follow_up,
        "message": message,
    }


def order_stage_state(
    order: Order,
    events: list[StageEvent],
    reminders: list[ReminderEvent],
    at: datetime,
) -> dict:
    started = current_stage_started(order, events)
    if order.status == "delivered":
        return {
            "state": "complete",
            "started_at": started,
            "elapsed_seconds": 0,
            "target_seconds": None,
            "target_label": "Complete",
            "message": "Delivered",
        }
    if order.status == "on_hold_photo":
        state = reminder_state(order, started, reminders, at)
        return {
            **state,
            "started_at": started,
            "target_seconds": STAGE_TARGETS[order.status][0],
            "target_label": "Reminder every 24h",
        }
    target_seconds, clock = STAGE_TARGETS.get(order.status, (None, "continuous"))
    start = order.created_at if clock == "first_response" else started
    elapsed = business_seconds_between(start, at) if clock == "business" else elapsed_seconds(start, at)
    overdue = bool(target_seconds is not None and elapsed > target_seconds)
    return {
        "state": "overdue" if overdue else "healthy",
        "started_at": started,
        "elapsed_seconds": elapsed,
        "target_seconds": target_seconds,
        "target_label": format_duration(target_seconds),
        "message": f"{format_duration(elapsed)} in stage",
    }


@dataclass
class CompletedOrderMetrics:
    production_seconds: float
    fulfillment_seconds: float
    customer_wait_seconds: float
    delivery_seconds: float | None


def completed_metrics(events: list[StageEvent]) -> CompletedOrderMetrics | None:
    entries = stage_entries(events)
    received = entries.get("received")
    shipped = entries.get("shipped")
    if not received or not shipped:
        return None
    customer_wait = 0.0
    production = 0.0
    for stage, start, end in stage_intervals(events, shipped):
        if start >= shipped:
            continue
        interval_end = min(end, shipped)
        if stage == "on_hold_photo":
            customer_wait += elapsed_seconds(start, interval_end)
        else:
            production += business_seconds_between(start, interval_end)
    delivered = entries.get("delivered")
    return CompletedOrderMetrics(
        production_seconds=production,
        fulfillment_seconds=elapsed_seconds(received, shipped),
        customer_wait_seconds=customer_wait,
        delivery_seconds=elapsed_seconds(shipped, delivered) if delivered else None,
    )


def operations_context(session, at: datetime) -> dict:
    orders = session.scalars(select(Order).order_by(Order.created_at.desc())).all()
    events = session.scalars(select(StageEvent).order_by(StageEvent.at)).all()
    reminders = session.scalars(select(ReminderEvent).order_by(ReminderEvent.sent_at)).all()
    events_by_order: dict[int, list[StageEvent]] = {}
    reminders_by_order: dict[int, list[ReminderEvent]] = {}
    for event in events:
        events_by_order.setdefault(event.order_id, []).append(event)
    for reminder in reminders:
        reminders_by_order.setdefault(reminder.order_id, []).append(reminder)

    states: dict[int, dict] = {}
    stage_rows = []
    for stage in STAGE_LABELS:
        stage_orders = [order for order in orders if order.status == stage]
        for order in stage_orders:
            states[order.id] = order_stage_state(
                order,
                events_by_order.get(order.id, []),
                reminders_by_order.get(order.id, []),
                at,
            )
        ordered = sorted(stage_orders, key=lambda order: states[order.id]["started_at"])
        overdue = [order for order in stage_orders if states[order.id]["state"] in {"overdue", "attention"}]
        stage_rows.append({
            "status": stage,
            "label": STAGE_LABELS[stage],
            "count": len(stage_orders),
            "overdue_count": len(overdue),
            "oldest": ordered[0] if ordered else None,
            "latest": ordered[-1] if ordered else None,
            "oldest_state": states.get(ordered[0].id) if ordered else None,
            "target_label": states[ordered[0].id]["target_label"] if ordered else stage_target_label(stage),
        })

    since = at - timedelta(days=7)
    previous_since = since - timedelta(days=7)
    current: list[CompletedOrderMetrics] = []
    previous: list[CompletedOrderMetrics] = []
    delivered_count = 0
    for order in orders:
        order_events = events_by_order.get(order.id, [])
        entries = stage_entries(order_events)
        delivered = entries.get("delivered")
        if delivered and delivered >= since:
            delivered_count += 1
        shipped = entries.get("shipped")
        metrics = completed_metrics(order_events)
        if not shipped or not metrics:
            continue
        if shipped >= since:
            current.append(metrics)
        elif shipped >= previous_since:
            previous.append(metrics)

    production = [item.production_seconds for item in current]
    previous_production = [item.production_seconds for item in previous]
    fulfillment = [item.fulfillment_seconds for item in current]
    customer_wait = [item.customer_wait_seconds for item in current]
    delivery = [item.delivery_seconds for item in current if item.delivery_seconds is not None]
    production_median = median(production) if production else None
    previous_median = median(previous_production) if previous_production else None
    if production_median is None:
        production_state = "unknown"
    elif production_median <= PRODUCTION_GREEN_SECONDS:
        production_state = "healthy"
    elif production_median <= PRODUCTION_WARNING_SECONDS:
        production_state = "warning"
    else:
        production_state = "critical"
    trend_percent = None
    if production_median is not None and previous_median:
        trend_percent = ((production_median - previous_median) / previous_median) * 100

    attention_orders = [order for order in orders if states.get(order.id, {}).get("state") in {"overdue", "attention"}]
    overall_state = "attention" if attention_orders else "healthy"
    headline = "Production needs attention" if attention_orders else "Production is on track"
    return {
        "orders": orders,
        "events_by_order": events_by_order,
        "reminders_by_order": reminders_by_order,
        "states": states,
        "stage_rows": stage_rows,
        "attention_orders": attention_orders,
        "overall_state": overall_state,
        "headline": headline,
        "performance": {
            "production_median": production_median,
            "production_p90": percentile(production, 0.9),
            "fulfillment_average": mean(fulfillment) if fulfillment else None,
            "customer_wait_average": mean(customer_wait) if customer_wait else None,
            "delivery_average": mean(delivery) if delivery else None,
            "shipped_count": len(current),
            "delivered_count": delivered_count,
            "production_state": production_state,
            "trend_percent": trend_percent,
        },
    }


def stage_target_label(stage: str) -> str:
    if stage == "on_hold_photo":
        return "Reminder every 24h"
    if stage == "delivered":
        return "Complete"
    target = STAGE_TARGETS.get(stage)
    return format_duration(target[0]) if target else "—"
