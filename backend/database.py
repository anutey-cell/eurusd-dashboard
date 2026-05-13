"""
SQLAlchemy database setup.
DATABASE_URL is read from config (i.e. .env) — never hardcoded.
Swap to PostgreSQL by setting DATABASE_URL=postgresql+psycopg2://... in .env.
"""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from config import settings

# SQLite requires check_same_thread=False for FastAPI's thread-per-request model.
# PostgreSQL and other drivers ignore unknown connect_args, so this is safe to keep.
_connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
)

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency: yields a DB session and closes it after the request."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session():
    """Context manager for use outside request scope (e.g. startup tasks)."""
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
