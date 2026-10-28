"""Reusable position ingest — one endpoint per broker UI (issue #2).

Replaces the one-off `scripts/ingest_2026_09_21_screenshot_positions.py`
(kept in `old/` as a historical reference of that first ingest). The agent
`posizioni` reads a screenshot upstream (OCR/vision, out of scope here) and
posts the values it read as one of the two payload shapes in
`..schemas.positions`; this module only validates and persists them as a new
`PositionSnapshot` + `Position` rows, never invents a missing value, and
never overwrites a previous snapshot in place.

Both brokers already exist in DB with `name_confirmed=True` (issue #1): a
slug this module doesn't recognize, or that isn't in `brokers` yet, is a 404
rather than an auto-created placeholder — that placeholder path was for the
first, unidentified ingest and doesn't apply once a broker's identity is
known.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..schemas.positions import (
    PositionOut,
    ScalableCapitalIngestIn,
    ScalableCapitalPositionIn,
    SnapshotOut,
    TradeRepublicIngestIn,
    TradeRepublicPositionIn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/positions", tags=["positions"])

TRADE_REPUBLIC_SLUG = "trade-republic"
SCALABLE_CAPITAL_SLUG = "scalable-capital"


def _require_db():
    if db._session_factory is None:
        raise HTTPException(
            status_code=503, detail="database del nodo non configurato"
        )
    return db._session_factory


def _duplicate_instrument_errors(instrument_names: list[str]) -> dict[str, str]:
    """The DB constraint (`uq_positions_snapshot_instrument`) would catch a
    repeated instrument too, but as an `IntegrityError` that reaches the
    client as a raw 500 mid-insert. Catching it here keeps the whole payload
    atomic: either every row is valid, or nothing is written.
    """
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for index, name in enumerate(instrument_names):
        if name in seen:
            errors[f"positions[{index}].instrument_name"] = (
                f"'{name}' compare più di una volta in questo snapshot"
            )
        seen.add(name)
    return errors


async def _get_broker(session: AsyncSession, slug: str) -> db.Broker:
    broker = (
        await session.execute(select(db.Broker).where(db.Broker.slug == slug))
    ).scalar_one_or_none()
    if broker is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"broker '{slug}' non trovato — deve esistere già in `brokers` "
                "(vedi .claude/docs/database.md); non viene creato al volo"
            ),
        )
    return broker


async def _ingest(
    *,
    broker_slug: str,
    snapshot_at,
    source: str,
    source_ref: str | None,
    positions,
    to_row,
) -> SnapshotOut:
    errors = _duplicate_instrument_errors([p.instrument_name for p in positions])
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    factory = _require_db()
    async with factory() as session:
        broker = await _get_broker(session, broker_slug)

        snapshot = db.PositionSnapshot(
            broker_id=broker.id,
            snapshot_at=snapshot_at,
            source=source,
            source_ref=source_ref,
        )
        session.add(snapshot)
        await session.flush()

        for raw in positions:
            session.add(to_row(snapshot.id, raw))

        await session.commit()
        await session.refresh(snapshot, attribute_names=["positions"])

        return SnapshotOut(
            id=snapshot.id,
            broker_id=broker.id,
            broker_slug=broker.slug,
            snapshot_at=snapshot.snapshot_at,
            source=snapshot.source,
            source_ref=snapshot.source_ref,
            positions=[
                PositionOut.model_validate(p, from_attributes=True)
                for p in snapshot.positions
            ],
        )


def _trade_republic_row(snapshot_id: int, raw: TradeRepublicPositionIn) -> db.Position:
    return db.Position(
        snapshot_id=snapshot_id,
        instrument_name=raw.instrument_name,
        ticker=raw.ticker,
        quantity=raw.quantity,
        average_price=None,  # never shown by this UI
        last_price=raw.last_price,
        currency=raw.currency,
        market_value=raw.market_value,
        change_pct=raw.change_pct,
        is_recurring_plan=False,  # this UI shows no recurring-plan badge
        plan_badge=None,
        uncertain=raw.uncertain,
        uncertain_notes=raw.uncertain_notes,
    )


def _scalable_capital_row(
    snapshot_id: int, raw: ScalableCapitalPositionIn
) -> db.Position:
    return db.Position(
        snapshot_id=snapshot_id,
        instrument_name=raw.instrument_name,
        ticker=raw.ticker,
        quantity=None,  # never shown by this UI
        average_price=None,  # never shown by this UI
        last_price=None,  # never shown by this UI
        currency=raw.currency,
        market_value=raw.market_value,
        change_pct=raw.change_pct,
        is_recurring_plan=raw.plan_badge is not None,
        plan_badge=raw.plan_badge,
        uncertain=raw.uncertain,
        uncertain_notes=raw.uncertain_notes,
    )


@router.post("/ingest/trade-republic")
async def ingest_trade_republic(payload: TradeRepublicIngestIn) -> SnapshotOut:
    """New snapshot from Trade Republic's tabular UI (fractional quantities)."""
    return await _ingest(
        broker_slug=TRADE_REPUBLIC_SLUG,
        snapshot_at=payload.snapshot_at,
        source=payload.source,
        source_ref=payload.source_ref,
        positions=payload.positions,
        to_row=_trade_republic_row,
    )


@router.post("/ingest/scalable-capital")
async def ingest_scalable_capital(payload: ScalableCapitalIngestIn) -> SnapshotOut:
    """New snapshot from Scalable Capital's list UI (market value + change% only)."""
    return await _ingest(
        broker_slug=SCALABLE_CAPITAL_SLUG,
        snapshot_at=payload.snapshot_at,
        source=payload.source,
        source_ref=payload.source_ref,
        positions=payload.positions,
        to_row=_scalable_capital_row,
    )
