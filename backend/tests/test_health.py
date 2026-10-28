"""Database-aware health endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from investment_manager_backend import db
from investment_manager_backend.app import app


@pytest.mark.asyncio
async def test_health_reports_database_degradation_when_unconfigured(monkeypatch):
    monkeypatch.setattr(db, "_session_factory", None)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/health")

    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["database"] == "error"


@pytest.mark.asyncio
async def test_health_reports_database_ok(tmp_path, monkeypatch):
    engine = db.make_engine(f"sqlite+aiosqlite:///{tmp_path / 'health.db'}")
    session_factory = db.make_session_factory(engine)
    monkeypatch.setattr(db, "_session_factory", session_factory)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["database"] == "ok"
    finally:
        await engine.dispose()
