from contextlib import nullcontext
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from igneous.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)

API_DIR = Path(__file__).resolve().parents[1]
UPGRADE = "run `cd api && uv run alembic upgrade head`"


def get_session():
    """FastAPI dependency."""
    with SessionLocal() as s:
        yield s


def _read_only(_session, _transaction, connection) -> None:
    connection.exec_driver_sql("SET TRANSACTION READ ONLY")


def read_only_session() -> Session:
    """A session whose every transaction is read-only: Postgres refuses any write made through it."""
    s = SessionLocal()
    event.listen(s, "after_begin", _read_only)
    return s


def use_database(name: str) -> None:
    """Point every session from now on at the database `name` on the configured server, with the configured user
    and password (`igneous mcp --database`): the client that starts the process names the database, and no
    credential leaves api/.env."""
    global engine
    url = make_url(settings.database_url).set(database=name)
    settings.database_url = url.render_as_string(hide_password=False)
    engine.dispose()
    engine = create_engine(url, pool_pre_ping=True)
    SessionLocal.configure(bind=engine)


def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "migrations"))
    return ScriptDirectory.from_config(config)


def schema_status(bind=None) -> str | None:
    """None when the database is at the code's migration head; otherwise what is wrong and how to fix it.

    Every table comes from a migration, so the API, the CLI and the MCP server check this before touching data and
    refuse to run on anything else, instead of failing mid-request. `bind` is a Connection to check instead of a
    new connection from the engine."""
    script = _script()
    heads = script.get_heads()
    if len(heads) != 1:
        return (f"the code has {len(heads)} migration heads ({', '.join(sorted(heads))}): merge them with "
                "`cd api && uv run alembic merge heads`")
    head, url = heads[0], (bind.engine if bind is not None else engine).url
    name = url.database
    try:
        with nullcontext(bind) if bind is not None else engine.connect() as c:
            current = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except ProgrammingError:
        return f"database {name} has no schema yet: {UPGRADE}"
    except OperationalError as e:
        reason = str(e.orig).strip().splitlines()[0] if e.orig else type(e).__name__
        return (f"cannot reach database {name} at {url.host}:{url.port} ({reason}): start Postgres "
                "(`make db`) or set IGNEOUS_DATABASE_URL")
    if current == head:
        return None
    if current is None:
        return f"database {name} has no schema yet: {UPGRADE}"
    try:
        known = script.get_revision(current) is not None
    except Exception:  # alembic raises ResolutionError / CommandError for a revision it doesn't have
        known = False
    if not known:
        return (f"database {name} is at migration {current}, which this code doesn't have (it was migrated by "
                f"newer code): switch to that code, or downgrade to {head} with it")
    return f"database {name} is at migration {current}, the code needs {head}: {UPGRADE}"
