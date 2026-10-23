from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from igneous.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session() -> Session:
    with SessionLocal() as s:
        yield s
        s.commit()


def get_session():
    """FastAPI dependency."""
    with SessionLocal() as s:
        yield s


def schema_problem(s: Session) -> str | None:
    """None when the database is at the newest migration; otherwise what is wrong and the command that fixes it.
    Every table comes from a migration (#181), so code running ahead of its schema fails here, not mid-request."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    api = Path(__file__).resolve().parents[1]
    config = Config(str(api / "alembic.ini"))
    config.set_main_option("script_location", str(api / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    try:
        current = s.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        s.rollback()
        current = None
    if current == head:
        return None
    return (f"database {engine.url.database} is at migration {current or 'none'}, the code needs {head}: "
            "run `cd api && uv run alembic upgrade head`")
