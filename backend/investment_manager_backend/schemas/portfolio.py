"""Response models for `/api/portfolio/allocation`.

Thin mirrors of the dataclasses in `investment_manager_backend.allocation`:
the engine stays pure and framework-free, these models only describe how its
result is serialised. Every monetary/percentage field is `Decimal` so nothing
becomes a float on the way out.

Nothing here is stored: the whole response is derived live from the latest
snapshots + the classifications + the directive. That is the point of the
feature — change a target in `directives.allocation_targets` (or in the three
asset-class columns) and the next call answers differently, with no code
change and no cached number to invalidate.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class BreakdownEntryOut(BaseModel):
    key: str
    amount_eur: Decimal
    pct: Decimal
    instruments: list[str]


class MatrixCellOut(BaseModel):
    """One cell of the region x sector matrix.

    The matrix is how the portfolio is reasoned about (a geographic satellite
    that itself contains sector satellites), so it is returned explicitly
    rather than left to the client to cross-tabulate.
    """

    region: str
    sector: str
    amount_eur: Decimal
    pct: Decimal
    instruments: list[str]


class BreakdownOut(BaseModel):
    total_eur: Decimal
    by_asset_class: list[BreakdownEntryOut]
    by_region: list[BreakdownEntryOut]
    by_sector: list[BreakdownEntryOut]
    by_size_bucket: list[BreakdownEntryOut]
    region_sector_matrix: list[MatrixCellOut]


class GapOut(BaseModel):
    """`gap_eur < 0` = under target (money to add); `> 0` = over target."""

    dimension: str
    key: str
    current_eur: Decimal
    current_pct: Decimal
    target_pct: Decimal | None
    target_pct_min: Decimal | None
    target_pct_max: Decimal | None
    target_eur: Decimal
    gap_eur: Decimal
    status: str
    notes: str | None


class AllocationSliceOut(BaseModel):
    dimension: str
    key: str
    deficit_eur: Decimal
    amount_eur: Decimal
    covers_full_gap: bool


class AllocationProposalOut(BaseModel):
    """How the budget splits across under-target classes.

    Asset classes only, never instruments: which ETF to buy is research and a
    human decision, and this node never places an order
    (`.claude/docs/project_spec.md`).
    """

    budget_eur: Decimal
    total_deficit_eur: Decimal
    allocated_eur: Decimal
    unallocated_eur: Decimal
    slices: list[AllocationSliceOut]
    notes: list[str]


class DirectiveOut(BaseModel):
    id: int
    slug: str
    title: str
    status: str
    agreed_at: datetime
    target_equity_pct: Decimal | None
    target_bonds_pct: Decimal | None
    target_gold_pct: Decimal | None
    allocation_targets: list[dict] | None
    notes: str | None


class UnclassifiedPositionOut(BaseModel):
    """A held position with no row in `instrument_classifications`.

    Reported, never bucketed into a default class: its money is real (it is
    included in `portfolio_total_eur`) but attributing it to an asset class
    nobody verified would silently corrupt every percentage above.
    """

    instrument_name: str
    market_value_eur: Decimal
    broker_slug: str | None


class SnapshotRefOut(BaseModel):
    """Which snapshot each broker's numbers come from (the most recent one)."""

    broker_slug: str
    snapshot_id: int
    snapshot_at: datetime
    position_count: int


class AllocationOut(BaseModel):
    computed_at: datetime
    budget_eur: Decimal
    snapshots: list[SnapshotRefOut]
    directive: DirectiveOut
    portfolio_total_eur: Decimal
    classified_total_eur: Decimal
    unclassified_total_eur: Decimal
    breakdown: BreakdownOut
    gaps: list[GapOut]
    proposal: AllocationProposalOut
    proposal_dimension: str
    unclassified: list[UnclassifiedPositionOut]
    warnings: list[str]
