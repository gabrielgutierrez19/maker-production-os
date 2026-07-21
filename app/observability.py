import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from datadog import DogStatsd


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        })


log_path = Path(os.getenv("LOG_PATH", "logs/app.jsonl"))
log_path.parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("shopfloor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.FileHandler(log_path)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

statsd = None
if os.getenv("DD_METRICS_ENABLED", "true").lower() == "true":
    statsd = DogStatsd(
        host=os.getenv("DD_AGENT_HOST", "127.0.0.1"),
        port=int(os.getenv("DD_DOGSTATSD_PORT", "8125")),
        constant_tags=["service:shopfloor", f"env:{os.getenv('DD_ENV', 'development')}"],
    )


def log_event(event: str, **fields) -> None:
    logger.info(event, extra={"fields": fields})


def count(metric: str, value: int = 1, tags: list[str] | None = None) -> None:
    if statsd:
        statsd.increment(metric, value=value, tags=tags)


def gauge(metric: str, value: float, tags: list[str] | None = None) -> None:
    if statsd:
        statsd.gauge(metric, value, tags=tags)


def histogram(metric: str, value: float, tags: list[str] | None = None) -> None:
    if statsd:
        statsd.histogram(metric, value, tags=tags)


def http_reporting_enabled() -> bool:
    return bool(
        os.getenv("DD_API_KEY")
        and os.getenv("DD_HTTP_ENABLED", "false").lower() == "true"
    )


def datadog_site() -> str:
    return os.getenv("DD_SITE", "datadoghq.eu").removeprefix("https://").rstrip("/")


def _post_json(url: str, payload) -> bool:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "DD-API-KEY": os.getenv("DD_API_KEY", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning(
            "datadog_http_publish_failed",
            extra={"fields": {"error_type": type(exc).__name__, "endpoint": url}},
        )
        return False


def publish_metrics(points: list[tuple[str, float, list[str] | None]]) -> bool:
    if not http_reporting_enabled() or not points:
        return False
    timestamp = int(time.time())
    env = os.getenv("DD_ENV", "development")
    series = []
    for metric, value, tags in points:
        series.append({
            "metric": metric,
            "type": 0,
            "points": [{"timestamp": timestamp, "value": float(value)}],
            "resources": [{"name": "shopfloor-hosted", "type": "host"}],
            "tags": ["service:shopfloor", f"env:{env}", *(tags or [])],
        })
    return _post_json(f"https://api.{datadog_site()}/api/v2/series", {"series": series})


def publish_order_logs(items: list[dict]) -> bool:
    if not http_reporting_enabled() or not items:
        return False
    env = os.getenv("DD_ENV", "development")
    logs = []
    for item in items:
        logs.append({
            "service": "shopfloor",
            "ddsource": "shopfloor-operations",
            "ddtags": f"env:{env}",
            "status": "warn" if item.get("needs_attention") else "info",
            "message": item.pop("message", "Shopfloor order status"),
            **item,
        })
    return _post_json(f"https://http-intake.logs.{datadog_site()}/api/v2/logs", logs)
