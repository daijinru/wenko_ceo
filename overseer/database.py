"""SQLite engine, Session factory, and Base for ORM models."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from overseer.config import get_config


class Base(DeclarativeBase):
    pass


_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        cfg = get_config()
        db_path = Path(cfg.database.path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{db_path}", echo=False)
        # Enable WAL mode for better concurrent reads
        @event.listens_for(_engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory


def get_session() -> Session:
    return get_session_factory()()


def init_db() -> None:
    """Create all tables and run migrations."""
    import overseer.models  # noqa: F401 — ensure models are registered
    Base.metadata.create_all(get_engine())
    _migrate_db()


def _migrate_db() -> None:
    """Run schema migrations for existing databases."""
    from sqlalchemy import inspect, text
    engine = get_engine()
    inspector = inspect(engine)
    if "executions" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("executions")]
        if "token_usage" not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE executions ADD COLUMN token_usage JSON"
                ))
                conn.commit()
    if "memories" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("memories")]
        with engine.connect() as conn:
            if "updated_at" not in columns:
                conn.execute(text(
                    "ALTER TABLE memories ADD COLUMN updated_at DATETIME"
                ))
            if "access_count" not in columns:
                conn.execute(text(
                    "ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 0"
                ))
            conn.commit()


def reset_db() -> None:
    """Reset engine and session factory (for testing)."""
    global _engine, _SessionFactory
    _engine = None
    _SessionFactory = None
