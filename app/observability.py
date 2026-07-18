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


Path("logs").mkdir(exist_ok=True)
logger = logging.getLogger("shopfloor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.FileHandler("logs/app.jsonl")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

statsd = DogStatsd(
    host=os.getenv("DD_AGENT_HOST", "127.0.0.1"),
    port=int(os.getenv("DD_DOGSTATSD_PORT", "8125")),
    constant_tags=["service:shopfloor", f"env:{os.getenv('DD_ENV', 'development')}"],
)


def log_event(event: str, **fields) -> None:
    logger.info(event, extra={"fields": fields})


def count(metric: str, tags: list[str] | None = None) -> None:
    statsd.increment(metric, tags=tags)


def gauge(metric: str, value: float, tags: list[str] | None = None) -> None:
    statsd.gauge(metric, value, tags=tags)


def histogram(metric: str, value: float, tags: list[str] | None = None) -> None:
    statsd.histogram(metric, value, tags=tags)
