from alembic import context
from sqlalchemy import create_engine

from igneous.config import settings

engine = create_engine(settings.database_url)
with engine.connect() as conn:
    context.configure(connection=conn)
    with context.begin_transaction():
        context.run_migrations()
