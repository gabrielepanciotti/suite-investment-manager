"""Database layer for the InvestmentManager node: engine/session wiring + models.

DSN from env `DATABASE_URL` (see `.env`). Node DB port (Life registry): 11338.

Models live here (not in a separate `models.py`) so that a single import —
`investment_manager_backend.db`, exactly what `migrations/env.py` imports —
registers everything on `suite_backend_core.Base.metadata` for Alembic
autogenerate. No `create_all`/writes happen at import time: the app stays pure,
importable with no `DATABASE_URL` configured (the dependency raises 503
instead of failing at import).

Domain (see `.claude/docs/database.md`, `.claude/agents/posizioni.md`):
- `Broker` — a broker/platform the owner holds positions on. Names are not
  guessed: an unconfirmed broker is stored with `name_confirmed=False` and a
  descriptive placeholder, never invented (see backlog + issue tracking the
  confirmation).
- `PositionSnapshot` — one ingest event (e.g. "these screenshots, on this
  date") grouping the positions read at that moment. Positions are never
  overwritten in place: each ingest creates a new snapshot, so the position
  history is a first-class read (`.claude/docs/database.md` calls this out
  explicitly: "storico posizioni nel tempo").
- `Position` — one instrument's state within a snapshot. Best-effort ingest:
  fields not legible with certainty from the source are left NULL and the row
  is flagged `uncertain=True` with `uncertain_notes` explaining why, never
  filled with a guessed value (invariant in `.claude/agents/posizioni.md`).

`visibility` follows the Life per-row standard (`node-realignment.md §"Condivisibilità
del dato"`): default `'private'`, since this node's `kind` is `istanza` (personal
financial data) — the column exists for consistency with the rest of the Life
network even though this node never exports a public bundle today.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column, relationship
from suite_backend_core import Base, get_session, make_engine, make_session_factory
from suite_backend_core.config import get_settings


def _utcnow() -> datetime:
    """Timezone-aware UTC now (`datetime.utcnow` is deprecated)."""
    return datetime.now(UTC)


class Broker(Base):
    """A broker/platform the owner holds positions on.

    Broker support is not yet defined (`.claude/docs/project_spec.md`): a
    broker discovered from a screenshot whose identity is not readable with
    certainty is stored with `name_confirmed=False` and a descriptive
    placeholder `slug`/`display_name` — never a guessed real broker name. The
    confirmation is tracked as a GitHub issue, not invented here.
    """

    __tablename__ = "brokers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, default="private", server_default="private"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    snapshots: Mapped[list[PositionSnapshot]] = relationship(back_populates="broker")


class PositionSnapshot(Base):
    """One ingest event: the positions read from a broker at a point in time.

    Positions are append-only across snapshots (never overwritten in place) so
    the portfolio history over time is a plain query, per
    `.claude/docs/database.md` ("storico posizioni nel tempo").
    """

    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_id: Mapped[int] = mapped_column(
        ForeignKey("brokers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(30), nullable=False, default="screenshot"
    )
    source_ref: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, default="private", server_default="private"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    broker: Mapped[Broker] = relationship(back_populates="snapshots")
    positions: Mapped[list[Position]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class Position(Base):
    """One instrument's state within a `PositionSnapshot`.

    Best-effort ingest from screenshots: `average_price` (prezzo medio) is
    routinely not shown by any broker UI seen so far and is left NULL rather
    than guessed. `ticker` is left NULL unless the source clearly states an
    unambiguous ticker (an instrument's display name alone is not a ticker).
    `plan_badge` stores the raw recurring-plan marker text as shown by the
    source (e.g. "P", "⟲ 20") instead of assuming a unit (days? purchases?)
    that isn't stated anywhere in the source. `recurring_amount` +
    `recurring_currency` hold the actual monthly PAC contribution once it is
    confirmed from a source that states it explicitly (e.g. a "Programmate"/
    "Ogni mese" screen) — left NULL (with `uncertain=True`) when a row is
    known to have an active plan (`is_recurring_plan=True`) but no source has
    shown the amount yet, never backfilled from the `plan_badge` number alone
    unless a separate source confirms the unit is currency.
    """

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("position_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    instrument_name: Mapped[str] = mapped_column(String(300), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(30))
    quantity: Mapped[str | None] = mapped_column(Numeric(20, 8))
    average_price: Mapped[str | None] = mapped_column(Numeric(14, 4))
    last_price: Mapped[str | None] = mapped_column(Numeric(14, 4))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    market_value: Mapped[str] = mapped_column(Numeric(14, 2), nullable=False)
    change_pct: Mapped[str | None] = mapped_column(Numeric(8, 4))
    is_recurring_plan: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    plan_badge: Mapped[str | None] = mapped_column(String(20))
    recurring_amount: Mapped[str | None] = mapped_column(Numeric(10, 2))
    recurring_currency: Mapped[str | None] = mapped_column(String(3))
    uncertain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    uncertain_notes: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, default="private", server_default="private"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    snapshot: Mapped[PositionSnapshot] = relationship(back_populates="positions")

    __table_args__ = (
        Index("ix_positions_instrument_name", "instrument_name"),
        UniqueConstraint(
            "snapshot_id", "instrument_name", name="uq_positions_snapshot_instrument"
        ),
    )


class InstrumentClassification(Base):
    """What an instrument *is* — asset class, region, sector, size bucket.

    The classification belongs to the instrument, not to a snapshot: one row
    per instrument, reused by every snapshot that mentions it (join key:
    `positions.instrument_name`). Before this table existed, the same
    judgement lived hand-written inside one-off scripts in `old/`, so every
    recomputation meant editing code; keeping it as data is what makes the
    allocation engine parametric.

    `confirmed` separates *verified* from *inferred*: True only when the fact
    is certain (typically because the instrument's own name states it — an
    ETF called "Physical Gold" is gold), False when it was deduced and still
    needs checking against the fund's factsheet/ISIN. Anything genuinely
    unknown is left NULL and explained in `notes` — never defaulted to a
    plausible-looking value (node invariant: an unverified datum is marked as
    such, never invented).

    A position whose `instrument_name` has no row here is NOT given a default
    class: the engine reports it in an explicit `unclassified` list, because
    silently bucketing an unknown instrument would corrupt every percentage
    downstream while looking perfectly fine.

    Vocabularies (open on purpose — a new region/sector is a new row, not a
    migration): `asset_class` in {'equity', 'bond', 'gold', 'cash'};
    `region` e.g. 'world', 'usa', 'europe', 'emerging', 'china', 'taiwan';
    `sector` e.g. 'core', 'tech', 'energy', 'other'; `size_bucket` e.g.
    'large', 'small_mid', 'broad'. They are plain strings, validated by the
    ingest path that writes them rather than by a DB enum, so that adding
    'japan' tomorrow costs nothing.
    """

    __tablename__ = "instrument_classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instrument_name: Mapped[str] = mapped_column(
        String(300), nullable=False, unique=True
    )
    isin: Mapped[str | None] = mapped_column(String(12))
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False)
    region: Mapped[str | None] = mapped_column(String(30))
    sector: Mapped[str | None] = mapped_column(String(30))
    size_bucket: Mapped[str | None] = mapped_column(String(20))
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, default="private", server_default="private"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class InvestmentDirective(Base):
    """An allocation/rebalancing rule set agreed with the owner.

    Structured (not just prose) so `analisi-mercato` can query it: target
    allocation percentages are plain columns, while the variable-shape parts
    (satellites, gaps to fill, candidate instruments researched ahead of
    time) are JSON lists of small dicts. Only the most recent `status="active"`
    directive should normally drive recommendations; superseding one instead
    of editing it in place keeps the same append-only history discipline as
    `PositionSnapshot`.

    `candidate_instruments` entries look like:
        {"isin": "IE00BHZRR147", "name": "Franklin FTSE China UCITS ETF",
         "category": "china-satellite-all-china", "ter_pct": "0.19",
         "thesis": "...", "source": "china.md memorandum"}
    `satellites` entries look like:
        {"name": "AI + energie rinnovabili", "size": "importante (non marginale, non core)"}
    `gaps` entries look like:
        {"theme": "China", "target_pct_min": "5", "target_pct_max": "10",
         "current_pct": "0", "notes": "..."}

    `allocation_targets` holds the *machine-readable* targets beyond the three
    asset-class columns above, so that changing a target changes every derived
    number without touching code (`investment_manager_backend.allocation`).
    Entries look like:
        {"dimension": "region" | "sector" | "size_bucket", "key": "china",
         "pct_min": "5", "pct_max": "10", "notes": "..."}
    Percentages are of the *whole* portfolio and are stored as decimal strings
    (never floats: money and percentages are `Decimal` end to end). `pct_max`
    may legitimately be absent — an uncapped target is a deliberate choice and
    the entry's `notes` must say so, otherwise a later reader cannot tell it
    from an omission. `gaps` above stays as the prose/history field it already
    was: it is a frozen narrative of a moment, while `allocation_targets` is
    the input the engine recomputes from.

    `selection_rules` answers the *other* question: once `allocation_targets`
    has decided how much money goes where, which concrete instrument do we
    buy? That criterion outlives any single proposal, so it is stored as data
    rather than restated in each recommendation's prose. Entries look like:
        {"rule": "prefer-lower-ter", "priority": 1, "statement": "...",
         "tradeoff": "...", "exception": "..."}
    `tradeoff` records what the rule is knowingly traded against (a cheaper
    ETF is not automatically the better one), and `exception` the case where
    the rule is allowed to yield — both agreed explicitly, so that a later
    reader does not mistake a deliberate deviation for a mistake.
    """

    __tablename__ = "directives"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    agreed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_equity_pct: Mapped[str | None] = mapped_column(Numeric(5, 2))
    target_bonds_pct: Mapped[str | None] = mapped_column(Numeric(5, 2))
    target_gold_pct: Mapped[str | None] = mapped_column(Numeric(5, 2))
    diversification_rule: Mapped[str | None] = mapped_column(Text)
    allocation_targets: Mapped[list | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    selection_rules: Mapped[list | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    satellites: Mapped[list | None] = mapped_column(JSON)
    gaps: Mapped[list | None] = mapped_column(JSON)
    candidate_instruments: Mapped[list | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, default="private", server_default="private"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class InvestmentRecommendation(Base):
    """One rebalancing proposal produced by `analisi-mercato` for the owner.

    Node invariant (`.claude/docs/project_spec.md`): this table records a
    *proposal*, never an execution — nothing here is ever sent to a broker
    automatically. `status` tracks what the owner actually did with it
    (`proposed` until he reports back), it is never flipped by this node on
    its own initiative.

    `actions` is the structured line-by-line proposal, a JSON list of dicts
    shaped like:
        {"when": "now" | "2026-09-24", "kind": "buy" | "pac_new" | "pac_pause"
         | "pac_increase", "instrument": "Franklin FTSE China UCITS ETF",
         "isin": "IE00BHZRR147", "amount_eur": "500.00", "broker_slug":
         "broker-non-confermato-1-quote-decimali", "rationale": "..."}
    `portfolio_snapshot` freezes the allocation numbers (current vs target vs
    post-trade) the recommendation was computed against, so a later reader
    does not have to re-derive them from `positions` as prices move.
    """

    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="proposed", server_default="proposed"
    )
    directive_id: Mapped[int | None] = mapped_column(
        ForeignKey("directives.id", ondelete="SET NULL"), nullable=True, index=True
    )
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    budget_now_eur: Mapped[str | None] = mapped_column(Numeric(10, 2))
    budget_later_eur: Mapped[str | None] = mapped_column(Numeric(10, 2))
    budget_later_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rationale: Mapped[str | None] = mapped_column(Text)
    portfolio_snapshot: Mapped[dict | None] = mapped_column(JSON)
    actions: Mapped[list | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(
        String(10), nullable=False, default="private", server_default="private"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    directive: Mapped[InvestmentDirective | None] = relationship()


_settings = get_settings()

_engine: AsyncEngine | None
_session_factory: async_sessionmaker[AsyncSession] | None

if _settings.database_url:
    _engine = make_engine(_settings.database_url)
    _session_factory = make_session_factory(_engine)
else:
    _engine = None
    _session_factory = None


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an `AsyncSession`.

    Raises 503 when `DATABASE_URL` is not configured, e.g. dev without a DB.
    """
    if _session_factory is None:
        raise HTTPException(status_code=503, detail="Database not configured")

    async for session in get_session(_session_factory):
        yield session
