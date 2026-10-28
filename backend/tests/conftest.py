"""Test fixtures for the DB-backed position-ingest endpoints.

Mirrors `Health/backend/tests/conftest.py`: `db.py` caches the engine/session
factory in module globals at import time, so each test gets a throwaway
SQLite file wired into those globals and torn down explicitly. SQLite is
enough — the schema uses no Postgres-only construct, and the unique
constraint these tests care about (`uq_positions_snapshot_instrument`) is
enforced by SQLite too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investment_manager_backend import db  # noqa: E402
from suite_backend_core import Base  # noqa: E402


def _reset_db_globals() -> None:
    db._engine = None
    db._session_factory = None


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    """An empty schema on a throwaway SQLite file, wired into `db`."""
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    _reset_db_globals()
    # `db._settings` is built once at import time from a cached `get_settings()`
    # (module-level, not lazy — see `db.py`'s docstring); clear the cache so this
    # fixture's env var actually takes effect instead of the frozen import-time one.
    db.get_settings.cache_clear()
    db._settings = db.get_settings()
    db._engine = db.make_engine(db._settings.database_url)
    db._session_factory = db.make_session_factory(db._engine)
    async with db._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield db._session_factory
    await db._engine.dispose()
    _reset_db_globals()
    os.environ.pop("DATABASE_URL", None)


@pytest_asyncio.fixture
async def client(session_factory):
    """ASGI client sharing the fixture's database."""
    from investment_manager_backend.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
