"""Derived portfolio view: `GET /api/portfolio/allocation`.

Everything this endpoint returns is computed live from three stored inputs —
the latest `PositionSnapshot` per broker, `instrument_classifications`, and
one `InvestmentDirective` — by the pure engine in
`investment_manager_backend.allocation`. No allocation number is persisted:
changing a target in the directive changes the answer on the next call, which
is exactly the property this feature exists for.

This router only does I/O and shape conversion (rows -> engine dataclasses ->
Pydantic). Any arithmetic belongs in `allocation.py`, where it can be tested
without a database.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db
from ..allocation import (
    PARTITION_DIMENSION,
    AllocationTarget,
    Breakdown,
    Classification,
    DirectiveTargets,
    Gap,
    PositionValue,
    build_breakdown,
    classify_positions,
    compute_gaps,
    propose_allocation,
)
from ..schemas.portfolio import (
    AllocationOut,
    AllocationProposalOut,
    BreakdownOut,
    DirectiveOut,
    GapOut,
    SnapshotRefOut,
    UnclassifiedPositionOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


def _require_db():
    if db._session_factory is None:
        raise HTTPException(
            status_code=503, detail="database del nodo non configurato"
        )
    return db._session_factory


def _as_decimal(value, field_name: str) -> Decimal | None:
    """Percentages are stored as decimal strings in JSON; never as floats.

    A malformed value is a 422 rather than a silent skip: a target the reader
    believes is being applied, but isn't, is worse than an error.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=422,
            detail=f"valore non numerico in {field_name}: {value!r}",
        ) from exc


async def _latest_snapshots(session: AsyncSession) -> list[tuple[db.Broker, db.PositionSnapshot]]:
    """The most recent snapshot of each broker.

    Snapshots are append-only (a re-ingest is a new snapshot, never an
    overwrite), so "the portfolio now" is the newest snapshot per broker —
    summing every snapshot would count the same holdings several times.
    Ties on `snapshot_at` are broken by `id` (the later ingest wins).
    """
    latest = (
        select(
            db.PositionSnapshot.broker_id,
            func.max(db.PositionSnapshot.snapshot_at).label("max_at"),
        )
        .group_by(db.PositionSnapshot.broker_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(db.Broker, db.PositionSnapshot)
            .join(db.PositionSnapshot, db.PositionSnapshot.broker_id == db.Broker.id)
            .join(
                latest,
                (db.PositionSnapshot.broker_id == latest.c.broker_id)
                & (db.PositionSnapshot.snapshot_at == latest.c.max_at),
            )
            .order_by(db.Broker.slug, db.PositionSnapshot.id.desc())
        )
    ).all()

    seen: set[int] = set()
    result: list[tuple[db.Broker, db.PositionSnapshot]] = []
    for broker, snapshot in rows:
        if broker.id in seen:
            continue
        seen.add(broker.id)
        result.append((broker, snapshot))
    return result


async def _load_directive(
    session: AsyncSession, directive_slug: str | None
) -> db.InvestmentDirective:
    stmt = select(db.InvestmentDirective)
    if directive_slug:
        stmt = stmt.where(db.InvestmentDirective.slug == directive_slug)
    else:
        stmt = stmt.where(db.InvestmentDirective.status == "active")
    stmt = stmt.order_by(
        db.InvestmentDirective.agreed_at.desc(), db.InvestmentDirective.id.desc()
    )
    directive = (await session.execute(stmt)).scalars().first()
    if directive is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"direttiva '{directive_slug}' non trovata"
                if directive_slug
                else "nessuna direttiva con status='active': i target sono un "
                "dato concordato con il proprietario, non un default da inventare"
            ),
        )
    return directive


def _directive_targets(directive: db.InvestmentDirective) -> DirectiveTargets:
    targets: list[AllocationTarget] = []
    for index, raw in enumerate(directive.allocation_targets or []):
        dimension = raw.get("dimension")
        key = raw.get("key")
        if not dimension or not key:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"allocation_targets[{index}] senza 'dimension'/'key' nella "
                    f"direttiva '{directive.slug}'"
                ),
            )
        targets.append(
            AllocationTarget(
                dimension=dimension,
                key=key,
                pct_min=_as_decimal(
                    raw.get("pct_min"), f"allocation_targets[{index}].pct_min"
                ),
                pct_max=_as_decimal(
                    raw.get("pct_max"), f"allocation_targets[{index}].pct_max"
                ),
                notes=raw.get("notes"),
            )
        )
    return DirectiveTargets(
        slug=directive.slug,
        target_equity_pct=_as_decimal(
            directive.target_equity_pct, "target_equity_pct"
        ),
        target_bonds_pct=_as_decimal(directive.target_bonds_pct, "target_bonds_pct"),
        target_gold_pct=_as_decimal(directive.target_gold_pct, "target_gold_pct"),
        allocation_targets=tuple(targets),
    )


def _breakdown_out(breakdown: Breakdown) -> BreakdownOut:
    def entries(items):
        return [
            {
                "key": e.key,
                "amount_eur": e.amount_eur,
                "pct": e.pct,
                "instruments": list(e.instruments),
            }
            for e in items
        ]

    return BreakdownOut(
        total_eur=breakdown.total_eur,
        by_asset_class=entries(breakdown.by_asset_class),
        by_region=entries(breakdown.by_region),
        by_sector=entries(breakdown.by_sector),
        by_size_bucket=entries(breakdown.by_size_bucket),
        region_sector_matrix=[
            {
                "region": c.region,
                "sector": c.sector,
                "amount_eur": c.amount_eur,
                "pct": c.pct,
                "instruments": list(c.instruments),
            }
            for c in breakdown.region_sector_matrix
        ],
    )


def _gap_out(gap: Gap) -> GapOut:
    return GapOut(
        dimension=gap.dimension,
        key=gap.key,
        current_eur=gap.current_eur,
        current_pct=gap.current_pct,
        target_pct=gap.target_pct,
        target_pct_min=gap.target_pct_min,
        target_pct_max=gap.target_pct_max,
        target_eur=gap.target_eur,
        gap_eur=gap.gap_eur,
        status=gap.status,
        notes=gap.notes,
    )


@router.get("/allocation", response_model=AllocationOut)
async def get_allocation(
    budget: Decimal = Query(
        Decimal("0"),
        ge=0,
        description="Nuovo denaro da ripartire, in EUR. I target sono "
        "applicati al totale POST nuovo denaro.",
    ),
    directive_slug: str | None = Query(
        None,
        description="Direttiva da usare. Default: la più recente con "
        "status='active'.",
    ),
) -> AllocationOut:
    """Live allocation: breakdown, gaps vs the directive, budget split.

    Uses the latest snapshot of each broker. Positions with no classification
    are listed in `unclassified` and excluded from the percentages, never
    assigned a default class.
    """
    factory = _require_db()
    async with factory() as session:
        snapshots = await _latest_snapshots(session)
        directive = await _load_directive(session, directive_slug)

        snapshot_ids = [s.id for _, s in snapshots]
        positions: list[PositionValue] = []
        per_snapshot_counts: dict[int, int] = {sid: 0 for sid in snapshot_ids}
        if snapshot_ids:
            rows = (
                await session.execute(
                    select(db.Position).where(db.Position.snapshot_id.in_(snapshot_ids))
                )
            ).scalars().all()
            slug_by_snapshot = {s.id: b.slug for b, s in snapshots}
            for row in rows:
                per_snapshot_counts[row.snapshot_id] += 1
                positions.append(
                    PositionValue(
                        instrument_name=row.instrument_name,
                        market_value=Decimal(row.market_value),
                        broker_slug=slug_by_snapshot.get(row.snapshot_id),
                    )
                )

        classification_rows = (
            await session.execute(select(db.InstrumentClassification))
        ).scalars().all()
        classifications = [
            Classification(
                instrument_name=c.instrument_name,
                asset_class=c.asset_class,
                region=c.region,
                sector=c.sector,
                size_bucket=c.size_bucket,
                isin=c.isin,
                confirmed=c.confirmed,
                notes=c.notes,
            )
            for c in classification_rows
        ]

        directive_out = DirectiveOut(
            id=directive.id,
            slug=directive.slug,
            title=directive.title,
            status=directive.status,
            agreed_at=directive.agreed_at,
            target_equity_pct=_as_decimal(
                directive.target_equity_pct, "target_equity_pct"
            ),
            target_bonds_pct=_as_decimal(
                directive.target_bonds_pct, "target_bonds_pct"
            ),
            target_gold_pct=_as_decimal(directive.target_gold_pct, "target_gold_pct"),
            allocation_targets=directive.allocation_targets,
            notes=directive.notes,
        )
        targets = _directive_targets(directive)

    result = classify_positions(positions, classifications)
    breakdown = build_breakdown(result)
    gaps = compute_gaps(breakdown, targets, budget)

    # Only the asset-class dimension partitions the portfolio: splitting one
    # budget over overlapping region/sector gaps would spend the same euro
    # twice. The other gaps are still returned, to be read as constraints.
    partition_gaps = [g for g in gaps if g.dimension == PARTITION_DIMENSION]
    proposal = propose_allocation(partition_gaps, budget)

    warnings: list[str] = []
    if result.unclassified:
        warnings.append(
            f"{len(result.unclassified)} posizioni ({result.unclassified_eur} EUR) "
            "non hanno una classificazione: sono escluse dalle percentuali e dai "
            "gap, non assegnate a una classe di default."
        )
    unconfirmed = [c.instrument_name for c in classifications if not c.confirmed]
    if unconfirmed:
        warnings.append(
            f"{len(unconfirmed)} classificazioni non verificate "
            "(confirmed=False): dedotte, da confermare su factsheet/ISIN."
        )
    if not snapshots:
        warnings.append("Nessuno snapshot di posizioni in archivio.")

    return AllocationOut(
        computed_at=datetime.now(UTC),
        budget_eur=budget,
        snapshots=[
            SnapshotRefOut(
                broker_slug=broker.slug,
                snapshot_id=snapshot.id,
                snapshot_at=snapshot.snapshot_at,
                position_count=per_snapshot_counts.get(snapshot.id, 0),
            )
            for broker, snapshot in snapshots
        ],
        directive=directive_out,
        portfolio_total_eur=result.classified_eur + result.unclassified_eur,
        classified_total_eur=result.classified_eur,
        unclassified_total_eur=result.unclassified_eur,
        breakdown=_breakdown_out(breakdown),
        gaps=[_gap_out(g) for g in gaps],
        proposal=AllocationProposalOut(
            budget_eur=proposal.budget_eur,
            total_deficit_eur=proposal.total_deficit_eur,
            allocated_eur=proposal.allocated_eur,
            unallocated_eur=proposal.unallocated_eur,
            slices=[
                {
                    "dimension": s.dimension,
                    "key": s.key,
                    "deficit_eur": s.deficit_eur,
                    "amount_eur": s.amount_eur,
                    "covers_full_gap": s.covers_full_gap,
                }
                for s in proposal.slices
            ],
            notes=list(proposal.notes),
        ),
        proposal_dimension=PARTITION_DIMENSION,
        unclassified=[
            UnclassifiedPositionOut(
                instrument_name=p.instrument_name,
                market_value_eur=p.market_value,
                broker_slug=p.broker_slug,
            )
            for p in result.unclassified
        ],
        warnings=warnings,
    )
