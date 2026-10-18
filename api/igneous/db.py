from contextlib import contextmanager

from sqlalchemy import create_engine
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
