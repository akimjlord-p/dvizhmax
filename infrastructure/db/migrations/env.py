import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine, pool

from infrastructure.db import Base

target_metadata = Base.metadata

load_dotenv()


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL (postgresql+psycopg://user:password@host:5432/db)")
    return url


if context.is_offline_mode():
    context.configure(
        dialect_name="postgresql", target_metadata=target_metadata,
        literal_binds=True, compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
