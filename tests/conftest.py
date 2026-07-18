import os

os.environ["DATABASE_URL"] = "sqlite:///test.db"

import pytest

from app.database import Base, engine


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
