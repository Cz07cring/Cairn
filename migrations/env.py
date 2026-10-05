"""Explicit migration process; API startup must never run migrations."""

import os

from alembic import context
from sqlalchemy import create_engine

url = os.environ["RING_DATABASE_URL"]
if not url.startswith("postgresql+psycopg://"):
    raise RuntimeError("PostgreSQL psycopg URL required")
engine = create_engine(url)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
