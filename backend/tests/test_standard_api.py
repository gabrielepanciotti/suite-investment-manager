"""Life tracker API conventions exposed by InvestmentManager."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def app_client():
    """Client for routes that do not need the database fixture."""
    from investment_manager_backend.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client


@pytest.mark.asyncio
async def test_entries_use_standard_page_and_cursor(client, session_factory):
    async with session_factory() as session:
        from investment_manager_backend import db

        broker = db.Broker(slug="test-broker", display_name="Broker di test")
        session.add(broker)
        await session.flush()
        snapshot = db.PositionSnapshot(
            broker_id=broker.id,
            snapshot_at=datetime(2026, 10, 27, 10, tzinfo=UTC),
            source="test",
        )
        session.add(snapshot)
        await session.flush()
        session.add_all(
            [
                db.Position(
                    snapshot_id=snapshot.id,
                    instrument_name=f"Strumento {index}",
                    market_value=str(index),
                    currency="EUR",
                )
                for index in range(3)
            ]
        )
        await session.commit()

    first = await client.get("/api/entries", params={"limit": 2})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert set(first_body) == {"items", "total", "next_cursor"}
    assert first_body["total"] == 3
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"] is not None
    assert first_body["next_cursor"] != "2"

    second = await client.get(
        "/api/entries",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["total"] == 3
    assert len(second_body["items"]) == 1
    assert second_body["next_cursor"] is None


@pytest.mark.asyncio
async def test_entries_reject_invalid_limit_with_uniform_error(app_client):
    response = await app_client.get("/api/entries", params={"limit": 201})

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body
    assert "error" in body
    assert body["error"] == {
        "code": "validation_error",
        "message": "Il valore fornito non è valido.",
        "field": "limit",
    }
    assert body["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.asyncio
async def test_unknown_route_uses_uniform_error(app_client):
    response = await app_client.get("/api/non-esiste")

    assert response.status_code == 404
    body = response.json()
    assert "detail" in body
    assert "error" in body
    assert body["error"] == {
        "code": "not_found",
        "message": "Risorsa non trovata.",
    }
    assert body["detail"] == "Not Found"


@pytest.mark.asyncio
async def test_overview_is_always_available(app_client):
    response = await app_client.get("/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "InvestmentManager"
    assert body["slug"] == "investmentManager"
    assert body["famiglia"] == "tracker"
    assert "/api/entries" in body["routes"]
    assert "/overview" in body["routes"]
