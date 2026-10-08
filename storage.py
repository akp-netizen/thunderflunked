"""
Persistence layer for the Faculty Development Game.

The game keeps all live state in two in-memory module globals in app.py
(`games` and `active_code`). That is fast, simple, and exactly what the frozen
release candidate was tested against. For deployment we must not lose that state
when the server process restarts (Render restarts dynos routinely), so this
module snapshots the entire in-memory state to a database and restores it on
boot.

Design (intentionally boring and reliable):
  * The whole state is a single small JSON document (a full two-team game is
    well under 10 KB). We store it as ONE row of TEXT. No ORM models, no
    migrations, no schema that has to track the game's evolving shape.
  * Works against PostgreSQL in production (Render) and SQLite for local
    development / tests through one SQLAlchemy code path.
  * The caller (app.py) owns the in-memory dicts and decides when to save.
    This module never imports app.py, so there is no circular dependency and
    the game logic is untouched.

Nothing here changes gameplay. It only reads and writes the state blob.
"""

import json
import os

from sqlalchemy import create_engine, text

_engine = None
_enabled = False


def is_enabled():
    return _enabled


def normalize_database_url(url):
    """Render (and Heroku-style) connection strings start with `postgres://` or
    `postgresql://`. SQLAlchemy needs an explicit driver; we use psycopg
    (psycopg 3). SQLite URLs are passed through unchanged.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def init_engine(database_url):
    """Create the engine and ensure the single-row state table exists.

    Returns True if persistence is now active, False if no database was
    configured (in which case the game runs purely in memory, exactly like the
    original prototype)."""
    global _engine, _enabled
    if not database_url:
        _engine = None
        _enabled = False
        return False

    url = normalize_database_url(database_url)
    kwargs = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        # A single-worker threaded server shares one SQLite connection across
        # request threads; allow that. (Postgres has no such restriction.)
        kwargs["connect_args"] = {"check_same_thread": False}

    _engine = create_engine(url, **kwargs)
    with _engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS game_state ("
            "  id INTEGER PRIMARY KEY,"
            "  data TEXT NOT NULL"
            ")"
        ))
    _enabled = True
    return True


def init_from_env():
    """Enable persistence from the DATABASE_URL environment variable if present.
    Used on Render. Returns True if persistence is active."""
    return init_engine(os.environ.get("DATABASE_URL"))


def save_state(games, active_code):
    """Write the complete in-memory state as one JSON row. No-op if persistence
    is disabled."""
    if not _enabled:
        return
    payload = json.dumps({"games": games, "active_code": active_code})
    with _engine.begin() as conn:
        # Single logical row (id=1). Delete-then-insert is portable across
        # SQLite and PostgreSQL and avoids dialect-specific UPSERT syntax.
        conn.execute(text("DELETE FROM game_state WHERE id = 1"))
        conn.execute(text("INSERT INTO game_state (id, data) VALUES (1, :d)"),
                     {"d": payload})


def load_state():
    """Return (games, active_code) from the database, or ({}, None) if there is
    no saved state yet or persistence is disabled.

    NOTE: JSON object keys are always strings. Some of the game's dictionaries
    are keyed by integer checkpoint index; app.py re-normalizes those after
    calling this function (see _normalize_loaded_state)."""
    if not _enabled:
        return {}, None
    with _engine.begin() as conn:
        row = conn.execute(text("SELECT data FROM game_state WHERE id = 1")).fetchone()
    if not row or not row[0]:
        return {}, None
    obj = json.loads(row[0])
    return obj.get("games", {}), obj.get("active_code")


def ping():
    """Lightweight connectivity check for the health endpoint. Returns True when
    persistence is disabled (nothing to check) or the database answers."""
    if not _enabled:
        return True
    with _engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True


def _reset_for_tests():
    """Dispose the engine and disable persistence. Used by the test suite so one
    test's temporary database never leaks into another."""
    global _engine, _enabled
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _enabled = False
