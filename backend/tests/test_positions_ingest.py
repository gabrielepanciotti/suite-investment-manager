"""Reusable ingest endpoints, one per broker UI (issue #2).

Trade Republic and Scalable Capital show different columns in their UI, so
each gets its own payload shape (`schemas/positions.py`) and its own route;
both land in the same `position_snapshots`/`positions` tables.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select


@pytest.mark.asyncio
async def test_trade_republic_ingest_creates_snapshot_and_positions(
    client, session_factory
):
    async with session_factory() as session:
        from investment_manager_backend import db

        session.add(db.Broker(slug="trade-republic", display_name="Trade Republic"))
        await session.commit()

    resp = await client.post(
        "/api/positions/ingest/trade-republic",
        json={
            "snapshot_at": "2026-09-22T09:00:00Z",
            "source": "screenshot",
            "source_ref": "shot1.jpg",
            "positions": [
                {
                    "instrument_name": "Msci World Ucits Hedged EUR (Acc)",
                    "quantity": "52.373619",
                    "last_price": "11.38",
                    "market_value": "595.88",
                },
                {
                    "instrument_name": "Tesla",
                    "quantity": "0.1992",
                    "last_price": "313.65",
                    "market_value": "62.48",
                    "uncertain": True,
                    "uncertain_notes": "Change% non leggibile",
                },
            ],
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["broker_slug"] == "trade-republic"
    assert body["source_ref"] == "shot1.jpg"
    assert len(body["positions"]) == 2
    tesla = next(p for p in body["positions"] if p["instrument_name"] == "Tesla")
    assert tesla["uncertain"] is True
    assert tesla["average_price"] is None
    assert tesla["plan_badge"] is None

    async with session_factory() as session:
        from investment_manager_backend import db

        snapshots = (await session.execute(select(db.PositionSnapshot))).scalars().all()
        assert len(snapshots) == 1
        positions = (await session.execute(select(db.Position))).scalars().all()
        assert len(positions) == 2


@pytest.mark.asyncio
async def test_scalable_capital_ingest_derives_recurring_plan_from_badge(
    client, session_factory
):
    async with session_factory() as session:
        from investment_manager_backend import db

        session.add(
            db.Broker(slug="scalable-capital", display_name="Scalable Capital")
        )
        await session.commit()

    resp = await client.post(
        "/api/positions/ingest/scalable-capital",
        json={
            "snapshot_at": "2026-09-22T09:00:00Z",
            "source_ref": "shot2.jpg",
            "positions": [
                {
                    "instrument_name": "Xtrackers MSCI World (Acc)",
                    "market_value": "1265.36",
                    "change_pct": "1.09",
                    "plan_badge": "P",
                },
                {
                    "instrument_name": "Brookfield Renewable",
                    "market_value": "52.45",
                    "change_pct": "0.63",
                },
            ],
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    positions = {p["instrument_name"]: p for p in body["positions"]}
    assert positions["Xtrackers MSCI World (Acc)"]["is_recurring_plan"] is True
    assert positions["Xtrackers MSCI World (Acc)"]["plan_badge"] == "P"
    assert positions["Brookfield Renewable"]["is_recurring_plan"] is False
    # Never shown by this UI: must stay NULL, never guessed.
    assert positions["Brookfield Renewable"]["quantity"] is None
    assert positions["Brookfield Renewable"]["average_price"] is None
    assert positions["Brookfield Renewable"]["last_price"] is None


@pytest.mark.asyncio
async def test_ingest_unknown_broker_slug_404s(client, session_factory):
    resp = await client.post(
        "/api/positions/ingest/trade-republic",
        json={
            "source_ref": "shot.jpg",
            "positions": [
                {"instrument_name": "Tesla", "market_value": "62.48"},
            ],
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ingest_rejects_duplicate_instrument_in_same_payload(
    client, session_factory
):
    async with session_factory() as session:
        from investment_manager_backend import db

        session.add(db.Broker(slug="trade-republic", display_name="Trade Republic"))
        await session.commit()

    resp = await client.post(
        "/api/positions/ingest/trade-republic",
        json={
            "source_ref": "shot.jpg",
            "positions": [
                {"instrument_name": "Tesla", "market_value": "62.48"},
                {"instrument_name": "Tesla", "market_value": "62.48"},
            ],
        },
    )
    assert resp.status_code == 422
    assert "positions[1].instrument_name" in str(resp.json()["detail"])
    assert resp.json()["error"]["field"] == "positions[1].instrument_name"

    async with session_factory() as session:
        from investment_manager_backend import db

        snapshots = (await session.execute(select(db.PositionSnapshot))).scalars().all()
        # Rejected payload must not leave a half-written snapshot behind.
        assert len(snapshots) == 0


@pytest.mark.asyncio
async def test_two_ingests_for_same_broker_create_two_snapshots(
    client, session_factory
):
    """Positions are append-only: a re-ingest is a new snapshot, not an
    overwrite of the previous one (`.claude/docs/database.md`)."""
    async with session_factory() as session:
        from investment_manager_backend import db

        session.add(db.Broker(slug="scalable-capital", display_name="Scalable Capital"))
        await session.commit()

    payload = {
        "source_ref": "shot.jpg",
        "positions": [{"instrument_name": "Tesla", "market_value": "62.48"}],
    }
    r1 = await client.post("/api/positions/ingest/scalable-capital", json=payload)
    r2 = await client.post("/api/positions/ingest/scalable-capital", json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["id"] != r2.json()["id"]

    async with session_factory() as session:
        from investment_manager_backend import db

        snapshots = (await session.execute(select(db.PositionSnapshot))).scalars().all()
        assert len(snapshots) == 2
