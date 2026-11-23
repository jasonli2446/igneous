"""Every test run is hermetic and has a database of its own.

- Settings start from their defaults. Neither api/.env nor IGNEOUS_* variables in the shell reach the tests (nor the
  subprocesses they start), and the Bedrock token is removed from the environment, so no test can make a hosted
  model call or read your data folder. The web app's URL is the test client's (http://testserver).
- The run's database, igneous_test_<pid>, lives on the Postgres server your configuration points at, never in the
  database it names. `test_db` creates and migrates it once per run and drops it when the run ends; databases left
  behind by runs that died are dropped first. Two runs at once (two worktrees, two agents) never touch each other.
- `db` gives a test a clean graph inside one transaction that is rolled back afterwards. Every session the code under
  test opens through `igneous.db.SessionLocal` (API routes and MCP tools included) joins that transaction. Readers'
  results cached per graph version (graph/dirty.py) start empty too.
- `committed_db` is for code that commits from another thread or process (jobs, subprocesses): it empties every
  table, and what the test writes is really committed.

Off CI, tests that need Postgres skip when it is not running.
"""
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from igneous.config import Settings, settings

API = Path(__file__).resolve().parents[1]
TEST_DB_PREFIX = "igneous_test_"
TEST_DB = f"{TEST_DB_PREFIX}{os.getpid()}"
DATA_DIR = tempfile.mkdtemp(prefix="igneous-test-data-")

_server = make_url(settings.database_url)  # the configured server; only its database name is replaced
for _name, _field in Settings.model_fields.items():
    setattr(settings, _name, _field.get_default(call_default_factory=True))
settings.database_url = _server.set(database=TEST_DB).render_as_string(hide_password=False)
settings.data_dir = DATA_DIR
# The web app is where FastAPI's test client calls from, so the API's local-only guard (igneous/access.py) lets it in.
settings.web_url = "http://testserver"

# Subprocesses (the CLI, the MCP server) build their own Settings: give them every value explicitly, which takes
# precedence over api/.env, and nothing else from the shell.
for _key in [k for k in os.environ if k.startswith("IGNEOUS_")] + ["AWS_BEARER_TOKEN_BEDROCK"]:
    os.environ.pop(_key, None)
for _name in Settings.model_fields:
    os.environ[f"IGNEOUS_{_name.upper()}"] = str(getattr(settings, _name))


def _admin():
    return create_engine(make_url(settings.database_url).set(database="postgres"), isolation_level="AUTOCOMMIT")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _drop_abandoned(c) -> None:
    """Drop test databases (igneous_test_<pid>, and scratch ones named igneous_test_<pid>_<purpose>) whose run is
    no longer alive on this machine."""
    for (name,) in c.execute(text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
                             {"p": TEST_DB_PREFIX + "%"}):
        pid = name.removeprefix(TEST_DB_PREFIX).split("_", 1)[0]
        if pid.isdigit() and int(pid) != os.getpid() and not _alive(int(pid)):
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def test_db():
    from alembic import command
    from alembic.config import Config

    from igneous.db import engine

    if engine.url.database != TEST_DB:  # something imported igneous.db before this file ran
        pytest.fail(f"refusing to run DB tests against {engine.url.database!r}")
    admin = _admin()
    try:
        with admin.connect() as c:
            _drop_abandoned(c)
            c.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
            c.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    except Exception as e:
        admin.dispose()
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Postgres not available: {type(e).__name__}")
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "migrations"))
    command.upgrade(config, "head")
    try:
        yield
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        admin.dispose()


def _empty(s) -> None:
    from igneous.graph import dirty

    tables = s.execute(text("""SELECT tablename FROM pg_tables
                               WHERE schemaname = 'public' AND tablename <> 'alembic_version'""")).scalars().all()
    s.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    s.execute(text("INSERT INTO graph_version (id) VALUES (true)"))  # the one row migration 0007 seeds
    dirty.forget()  # every test's graph starts at the same version: no reader's answer carries over


@pytest.fixture
def db(test_db):
    """A session on an empty graph; everything written during the test is rolled back."""
    from igneous.db import SessionLocal, engine

    saved = dict(SessionLocal.kw)
    with engine.connect() as connection:
        outer = connection.begin()
        SessionLocal.configure(bind=connection, join_transaction_mode="create_savepoint")
        try:
            with SessionLocal() as s:
                _empty(s)
                yield s
        finally:
            SessionLocal.kw = saved
            outer.rollback()


@pytest.fixture
def committed_db(test_db):
    """An empty graph whose writes are committed, for code that runs in other threads or processes."""
    from igneous.db import SessionLocal

    with SessionLocal() as s:
        _empty(s)
        s.commit()
        yield s


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(DATA_DIR, ignore_errors=True)
