"""Engine construction.

One function, and it exists for one reason: SQLite does not enforce foreign keys
unless you ask it to, per connection. Without the pragma below, a
`related_transaction_id` pointing at a row that does not exist would insert
happily and the refund chain in brief 16.8 would rot silently.

`journal_mode=WAL` is set for the same durability reason as section 16.1: the
ledger is a single file with no way to rebuild it, because the images are gone
after confirm.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, event, text
from sqlalchemy import create_engine as _create_engine
from sqlalchemy.engine import Connection


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def create_engine(url: str, **kwargs: Any) -> Engine:
    """Build an engine, with SQLite's foreign key enforcement turned on.

    Args:
        url: A SQLAlchemy URL, for example `sqlite:///raseed.db`.
        **kwargs: Passed through to SQLAlchemy.
    """
    engine = _create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def foreign_keys_enforced(connection: Connection) -> bool:
    """Whether the live connection is actually enforcing foreign keys."""
    if connection.engine.dialect.name != "sqlite":
        return True
    return bool(connection.execute(text("PRAGMA foreign_keys")).scalar())


__all__ = ["create_engine", "foreign_keys_enforced"]
