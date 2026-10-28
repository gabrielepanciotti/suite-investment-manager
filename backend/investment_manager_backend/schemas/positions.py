"""Ingest payloads for `/api/positions/ingest/*` — one shape per broker UI.

Trade Republic and Scalable Capital show different columns, so a single
generic "position" input would force one broker's fields to be silently
optional for the other and would lose the compile-time guarantee that, say,
a Scalable Capital payload never carries a `quantity` nobody read from the
screen (`.claude/agents/posizioni.md`: never invent a value). Two request
models, one per broker, keep each shape honest about what its UI actually
shows; both feed the same `Position`/`PositionSnapshot` tables
(`investment_manager_backend.db`).

Reading the screenshot itself (OCR/vision) happens upstream, in the agent
that receives the image — these models are the "I already have the read
values, now persist them" boundary described in issue #2.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class TradeRepublicPositionIn(BaseModel):
    """One row from Trade Republic's tabular position list.

    Columns actually shown by that UI: Instrument / Position (quantity, in
    fractional shares) / Last Price / Change% / Market value. Average
    purchase price and ticker are not shown there and are left `None` rather
    than guessed — set `uncertain=True` (+ `uncertain_notes`) for any field
    that was on screen but not legible with certainty.
    """

    instrument_name: str
    ticker: str | None = None
    quantity: Decimal | None = None
    last_price: Decimal | None = None
    change_pct: Decimal | None = None
    market_value: Decimal
    currency: str = "EUR"
    uncertain: bool = False
    uncertain_notes: str | None = None


class ScalableCapitalPositionIn(BaseModel):
    """One row from Scalable Capital's list UI.

    That UI shows only market value + change%, plus a recurring-plan badge
    ("P", "⟲20", ...) next to instruments bought via a savings plan; no
    quantity or price is ever visible there. `plan_badge` is kept as the raw
    badge text — its unit (days? purchases?) is never stated by the UI, so it
    is never interpreted as one.
    """

    instrument_name: str
    ticker: str | None = None
    market_value: Decimal
    change_pct: Decimal | None = None
    currency: str = "EUR"
    plan_badge: str | None = None
    uncertain: bool = False
    uncertain_notes: str | None = None


class PositionOut(BaseModel):
    id: int
    instrument_name: str
    ticker: str | None
    quantity: Decimal | None
    average_price: Decimal | None
    last_price: Decimal | None
    currency: str
    market_value: Decimal
    change_pct: Decimal | None
    is_recurring_plan: bool
    plan_badge: str | None
    uncertain: bool
    uncertain_notes: str | None

    model_config = {"from_attributes": True}


class SnapshotIngestIn(BaseModel):
    """Shared envelope: when the screenshot was taken and where it came from.

    `snapshot_at` defaults to "now" only if the caller has no better
    information — normally the agent knows the screenshot's timestamp
    (upload time, EXIF, or what the owner said) and should pass it explicitly,
    since positions are read as a point-in-time snapshot, not overwritten.
    """

    snapshot_at: datetime = Field(default_factory=lambda: datetime.now())
    source: str = "screenshot"
    source_ref: str | None = None


class TradeRepublicIngestIn(SnapshotIngestIn):
    positions: list[TradeRepublicPositionIn]


class ScalableCapitalIngestIn(SnapshotIngestIn):
    positions: list[ScalableCapitalPositionIn]


class SnapshotOut(BaseModel):
    id: int
    broker_id: int
    broker_slug: str
    snapshot_at: datetime
    source: str
    source_ref: str | None
    positions: list[PositionOut]

    model_config = {"from_attributes": True}
