import json
import logging
import os
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
