import os

os.environ["DATABASE_URL"] = "sqlite:///test.db"

import pytest

from app.database import Base, engine
from app.incidents import incidents


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    incidents.clear()
    yield
    incidents.clear()
    Base.metadata.drop_all(engine)
