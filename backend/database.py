"""
SQLAlchemy database setup.
DATABASE_URL is read from config (i.e. .env) — never hardcoded.

Local dev (default):
    DATABASE_URL=sqlite:///./xauusd_signals.db
Production (Supabase Postgres):
    DATABASE_URL=postgresql+psycopg2://postgres:<pwd>@db.<ref>.supabase.co:5432/postgres
    or the pooled connection:
    DATABASE_URL=postgresql+psycopg2://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:6543/postgres
"""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from config import settings

# SQLite requires check_same_thread=False for FastAPI's thread-per-request model.
# PostgreSQL and other drivers ignore unknown connect_args, so this is safe to keep.
_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

# Engine kwargs differ between SQLite (file-based, no pool needed) and Postgres
# (network, needs pool_pre_ping + sane pool size for Supabase free tier).
_engine_kwargs = {"connect_args": _connect_args, "echo": False}
if not _is_sqlite:
    _engine_kwargs.update(
        pool_pre_ping=True,       # health-check each connection; auto-reconnect
        pool_size=5,              # Supabase free tier caps ~10 direct connections
        max_overflow=5,
        pool_recycle=1800,        # recycle connections every 30 min
    )

engine = create_engine(settings.database_url, **_engine_kwargs)

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
