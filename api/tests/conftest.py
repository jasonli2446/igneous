"""Tests never touch the database IGNEOUS_DATABASE_URL names (#133).

Before any igneous module opens a connection, the configured URL is pointed at a dedicated `igneous_test`
database on the same server. DB-backed tests request the `test_db` fixture, which recreates and migrates it
once per run. Off CI, the tests that need Postgres skip when it is not running.
"""
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from igneous.config import settings

TEST_DB = "igneous_test"
API = Path(__file__).resolve().parents[1]

settings.database_url = make_url(settings.database_url).set(database=TEST_DB).render_as_string(hide_password=False)
os.environ["IGNEOUS_DATABASE_URL"] = settings.database_url  # for subprocesses


@pytest.fixture(scope="session")
def test_db():
    from alembic import command
    from alembic.config import Config

    from igneous.db import engine

    if engine.url.database != TEST_DB:  # something imported igneous.db before this file ran
        pytest.fail(f"refusing to run DB tests against {engine.url.database!r}")
    admin = create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    except Exception as e:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Postgres not available: {type(e).__name__}")
    finally:
        admin.dispose()
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    command.upgrade(config, "head")
    yield
    engine.dispose()
