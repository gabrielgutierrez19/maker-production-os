import os

os.environ["DATABASE_URL"] = "sqlite:///test.db"
os.environ["DATADOG_WEBHOOK_SECRET"] = ""

import pytest

from app.database import Base, engine
from app.incidents import incidents


@pytest.fixture(autouse=True)
def clean_database():
    import app.main as main

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    incidents.clear()
    main.chaos_poison_next = False
    main.chaos_slow_until = None
    main.last_http_publish_at = None
    yield
    incidents.clear()
    main.chaos_poison_next = False
    main.chaos_slow_until = None
    main.last_http_publish_at = None
    Base.metadata.drop_all(engine)
