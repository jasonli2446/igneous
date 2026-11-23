from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from igneous.config import settings

if context.config.config_file_name:
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)

engine = create_engine(settings.database_url, poolclass=NullPool)
with engine.connect() as conn:
    context.configure(connection=conn)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
