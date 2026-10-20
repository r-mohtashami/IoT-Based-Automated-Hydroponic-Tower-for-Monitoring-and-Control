import os
from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def verify_database_connection() -> None:
    """Raise if the configured database cannot answer a simple query."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
