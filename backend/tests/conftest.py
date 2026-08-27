"""Isolate tests from the real database. Point db at a throwaway sqlite file
per test session so `uv run pytest` never pollutes backend/data/chief.sqlite3.

This must run before app.db opens its connection, so set it at import time."""

import os
import tempfile

import pytest

# Redirect the DB path before anything imports/opens the connection.
_tmp = tempfile.mkdtemp(prefix="cos-test-")
os.environ["COS_DB_PATH"] = os.path.join(_tmp, "test.sqlite3")

from app import config  # noqa: E402
config.DB_PATH = os.environ["COS_DB_PATH"]
from app import db  # noqa: E402
db.DB_PATH = os.environ["COS_DB_PATH"]


@pytest.fixture(autouse=True, scope="session")
def _init_schema():
    db.get_conn()  # creates schema on the temp DB
    yield
