"""Allocation engine + `GET /api/portfolio/allocation`.

Most of these tests never touch a database: the engine
(`investment_manager_backend.allocation`) is pure by design, so a portfolio
can be fabricated in three lines and the arithmetic checked exactly. The last
two tests go through the real endpoint on the throwaway SQLite fixture from
`conftest.py`, to pin the wiring (latest snapshot per broker, directive
lookup, unclassified reporting).

The property that matters most is `test_changing_directive_targets_changes_gaps`:
same positions, two directives, different gaps — with no code change. That is
the whole reason the classification and the targets moved from one-off
scripts into the database.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from investment_manager_backend.allocation import (
    AllocationTarget,
    Classification,
    DirectiveTargets,
    PositionValue,
    build_breakdown,
    classify_positions,
    compute_gaps,
    propose_allocation,
)


def _d(value: str) -> Decimal:
    return Decimal(value)


# A small fabricated portfolio: 1.000 EUR total, deliberately not resembling
# A representative portfolio (a pure engine must not need real data to be tested).
POSITIONS = [
    PositionValue("Euro Tech ETF", _d("300.00")),
    PositionValue("Euro Core ETF", _d("200.00")),
    PositionValue("US Tech ETF", _d("300.00")),
    PositionValue("Global Govt Bond", _d("150.00")),
    PositionValue("Physical Gold", _d("50.00")),
]

CLASSIFICATIONS = [
    Classification("Euro Tech ETF", "equity", "europe", "tech", "large"),
    Classification("Euro Core ETF", "equity", "europe", "core", "small_mid"),
    Classification("US Tech ETF", "equity", "usa", "tech", "large"),
    Classification("Global Govt Bond", "bond", "world", "core"),
    Classification("Physical Gold", "gold", "world"),
]


def _breakdown():
    return build_breakdown(classify_positions(POSITIONS, CLASSIFICATIONS))


def test_breakdown_totals_and_region_sector_matrix():
    breakdown = _breakdown()

    assert breakdown.total_eur == _d("1000.00")

    by_class = {e.key: e for e in breakdown.by_asset_class}
    assert by_class["equity"].amount_eur == _d("800.00")
    assert by_class["equity"].pct == _d("80.00")
    assert by_class["bond"].amount_eur == _d("150.00")
    assert by_class["gold"].pct == _d("5.00")

    by_region = {e.key: e.amount_eur for e in breakdown.by_region}
    assert by_region == {
        "europe": _d("500.00"),
        "usa": _d("300.00"),
        "world": _d("200.00"),
    }
    by_sector = {e.key: e.amount_eur for e in breakdown.by_sector}
    assert by_sector["tech"] == _d("600.00")
    assert by_sector["core"] == _d("350.00")
    # Gold has no sector: bucketed as 'unspecified', never silently dropped.
    assert by_sector["unspecified"] == _d("50.00")

    matrix = {(c.region, c.sector): c for c in breakdown.region_sector_matrix}
    assert matrix[("europe", "tech")].amount_eur == _d("300.00")
    assert matrix[("europe", "core")].amount_eur == _d("200.00")
    assert matrix[("usa", "tech")].amount_eur == _d("300.00")
    assert matrix[("world", "core")].amount_eur == _d("150.00")
    assert matrix[("world", "unspecified")].amount_eur == _d("50.00")
    assert matrix[("usa", "tech")].pct == _d("30.00")
    # Every euro is in exactly one cell.
    assert sum(c.amount_eur for c in breakdown.region_sector_matrix) == _d("1000.00")
    assert matrix[("europe", "tech")].instruments == ("Euro Tech ETF",)


def test_unclassified_position_is_reported_and_does_not_break_the_maths():
    positions = [*POSITIONS, PositionValue("Mystery Fund", _d("500.00"))]

    result = classify_positions(positions, CLASSIFICATIONS)

    assert [p.instrument_name for p in result.unclassified] == ["Mystery Fund"]
    assert result.unclassified_eur == _d("500.00")
    assert result.classified_eur == _d("1000.00")

    breakdown = build_breakdown(result)
    # The unknown position is NOT given a default class and does not inflate
    # the denominator: percentages stay those of what we actually know.
    assert breakdown.total_eur == _d("1000.00")
    assert all(
        "Mystery Fund" not in e.instruments for e in breakdown.by_asset_class
    )
    by_class = {e.key: e.pct for e in breakdown.by_asset_class}
    assert by_class["equity"] == _d("80.00")


def test_changing_directive_targets_changes_gaps():
    """THE property: the targets are a parameter, not a constant in code."""
    breakdown = _breakdown()
    new_money = _d("0")

    conservative = DirectiveTargets(
        slug="test-60-30-10",
        target_equity_pct=_d("60"),
        target_bonds_pct=_d("30"),
        target_gold_pct=_d("10"),
    )
    aggressive = DirectiveTargets(
        slug="test-80-10-10",
        target_equity_pct=_d("80"),
        target_bonds_pct=_d("10"),
        target_gold_pct=_d("10"),
    )

    g1 = {g.key: g for g in compute_gaps(breakdown, conservative, new_money)}
    g2 = {g.key: g for g in compute_gaps(breakdown, aggressive, new_money)}

    # Same positions, different directive -> different targets and gaps.
    assert g1["equity"].target_eur == _d("600.00")
    assert g1["equity"].gap_eur == _d("200.00")  # over target
    assert g1["equity"].status == "over"
    assert g1["bond"].gap_eur == _d("-150.00")  # under target

    assert g2["equity"].target_eur == _d("800.00")
    assert g2["equity"].gap_eur == _d("0.00")
    assert g2["equity"].status == "on_target"
    assert g2["bond"].target_eur == _d("100.00")
    assert g2["bond"].gap_eur == _d("50.00")

    # ...and the gold target, unchanged between the two, stays put.
    assert g1["gold"].target_eur == g2["gold"].target_eur == _d("100.00")


def test_targets_apply_to_the_total_after_new_money():
    breakdown = _breakdown()
    directive = DirectiveTargets(
        slug="test-65-20-15",
        target_equity_pct=_d("65"),
        target_bonds_pct=_d("20"),
        target_gold_pct=_d("15"),
    )

    gaps = {g.key: g for g in compute_gaps(breakdown, directive, _d("1000.00"))}

    # 2.000 post-money, not 1.000.
    assert gaps["equity"].target_eur == _d("1300.00")
    assert gaps["bond"].target_eur == _d("400.00")
    assert gaps["gold"].target_eur == _d("300.00")
    assert gaps["gold"].gap_eur == _d("-250.00")
    # `current_pct` stays a percentage of what is held today.
    assert gaps["gold"].current_pct == _d("5.00")


def test_band_targets_under_within_and_uncapped():
    breakdown = _breakdown()
    directive = DirectiveTargets(
        slug="test-bands",
        allocation_targets=(
            # 0 held -> under the floor.
            AllocationTarget("region", "china", pct_min=_d("5"), pct_max=_d("10")),
            # 200 held = 20% -> inside 10-30%.
            AllocationTarget(
                "size_bucket", "small_mid", pct_min=_d("10"), pct_max=_d("30")
            ),
            # 600 held = 60%, no ceiling -> never reported as 'over'.
            AllocationTarget(
                "sector", "tech", pct_min=_d("20"), notes="tetto assente per scelta"
            ),
        ),
    )

    gaps = {g.key: g for g in compute_gaps(breakdown, directive, _d("0"))}

    assert gaps["china"].status == "under"
    assert gaps["china"].target_eur == _d("50.00")
    assert gaps["china"].gap_eur == _d("-50.00")

    assert gaps["small_mid"].status == "within"
    assert gaps["small_mid"].gap_eur == _d("0.00")

    assert gaps["tech"].status == "within"
    assert gaps["tech"].target_pct_max is None
    assert gaps["tech"].notes == "tetto assente per scelta"


def test_budget_smaller_than_total_gap_is_split_proportionally():
    breakdown = _breakdown()
    directive = DirectiveTargets(
        slug="test-65-20-15",
        target_equity_pct=_d("65"),
        target_bonds_pct=_d("20"),
        target_gold_pct=_d("15"),
    )
    # Post-money total 2.000 -> equity 1.300 (deficit 500), bond 400 (deficit
    # 250), gold 300 (deficit 250): 1.000 of deficit for a 100 budget.
    gaps = compute_gaps(breakdown, directive, _d("1000.00"))
    proposal = propose_allocation(gaps, _d("100.00"))

    assert proposal.total_deficit_eur == _d("1000.00")
    assert proposal.allocated_eur == _d("100.00")
    assert proposal.unallocated_eur == _d("0")
    amounts = {s.key: s.amount_eur for s in proposal.slices}
    assert amounts == {
        "equity": _d("50.00"),
        "bond": _d("25.00"),
        "gold": _d("25.00"),
    }
    assert all(not s.covers_full_gap for s in proposal.slices)
    # Slices always sum to the budget, to the cent.
    assert sum(s.amount_eur for s in proposal.slices) == _d("100.00")


def test_budget_larger_than_total_gap_leaves_an_explicit_residual():
    breakdown = _breakdown()
    directive = DirectiveTargets(
        slug="test-65-20-15",
        target_equity_pct=_d("65"),
        target_bonds_pct=_d("20"),
        target_gold_pct=_d("15"),
    )
    # Targets measured on the current 1.000 (new_money=0): equity 650 (over by
    # 150), bond 200 (deficit 50), gold 150 (deficit 100) -> 150 of deficit,
    # against a 200 budget.
    gaps = compute_gaps(breakdown, directive, _d("0"))

    proposal = propose_allocation(gaps, _d("200.00"))

    amounts = {s.key: s.amount_eur for s in proposal.slices}
    assert amounts == {"bond": _d("50.00"), "gold": _d("100.00")}
    assert all(s.covers_full_gap for s in proposal.slices)
    # The rest is declared, never spread around by default.
    assert proposal.unallocated_eur == _d("50.00")
    assert proposal.allocated_eur == _d("150.00")
    assert any("unallocated_eur" in n for n in proposal.notes)


def test_proportional_split_assigns_stray_cents_without_losing_them():
    """1 cent cannot be split in three: largest-remainder, never a drift."""
    positions = [
        PositionValue("A", _d("0")),
        PositionValue("B", _d("0")),
        PositionValue("C", _d("100.00")),
    ]
    classifications = [
        Classification("A", "bond"),
        Classification("B", "gold"),
        Classification("C", "equity"),
    ]
    breakdown = build_breakdown(classify_positions(positions, classifications))
    directive = DirectiveTargets(
        slug="t",
        target_equity_pct=_d("50"),
        target_bonds_pct=_d("25"),
        target_gold_pct=_d("25"),
    )
    gaps = compute_gaps(breakdown, directive, _d("0"))

    proposal = propose_allocation(gaps, _d("0.01"))

    assert sum(s.amount_eur for s in proposal.slices) == _d("0.01")
    assert proposal.unallocated_eur == _d("0")


def test_no_underweight_class_leaves_the_whole_budget_unallocated():
    breakdown = _breakdown()
    directive = DirectiveTargets(slug="t", target_equity_pct=_d("10"))
    gaps = compute_gaps(breakdown, directive, _d("0"))

    proposal = propose_allocation(gaps, _d("500.00"))

    assert proposal.allocated_eur == _d("0")
    assert proposal.unallocated_eur == _d("500.00")
    assert proposal.notes  # says why, instead of returning a silent empty split


# --------------------------------------------------------------------------
# Endpoint (SQLite fixture from conftest.py)
# --------------------------------------------------------------------------


async def _seed(session_factory):
    from datetime import datetime, timezone

    from investment_manager_backend import db

    async with session_factory() as session:
        broker = db.Broker(slug="trade-republic", display_name="Trade Republic")
        session.add(broker)
        await session.flush()
        old = db.PositionSnapshot(
            broker_id=broker.id,
            snapshot_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        recent = db.PositionSnapshot(
            broker_id=broker.id,
            snapshot_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        session.add_all([old, recent])
        await session.flush()
        # Stale snapshot: must be ignored entirely, not summed in.
        session.add(
            db.Position(
                snapshot_id=old.id,
                instrument_name="Euro Tech ETF",
                market_value=Decimal("9999.00"),
            )
        )
        for name, value in [
            ("Euro Tech ETF", "300.00"),
            ("Global Govt Bond", "150.00"),
            ("Physical Gold", "50.00"),
            ("Mystery Fund", "500.00"),
        ]:
            session.add(
                db.Position(
                    snapshot_id=recent.id,
                    instrument_name=name,
                    market_value=Decimal(value),
                )
            )
        for c in CLASSIFICATIONS[:1] + CLASSIFICATIONS[3:]:
            session.add(
                db.InstrumentClassification(
                    instrument_name=c.instrument_name,
                    asset_class=c.asset_class,
                    region=c.region,
                    sector=c.sector,
                    size_bucket=c.size_bucket,
                    confirmed=True,
                )
            )
        session.add(
            db.InvestmentDirective(
                slug="test-active",
                title="Direttiva di test",
                status="active",
                agreed_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                target_equity_pct=Decimal("65"),
                target_bonds_pct=Decimal("20"),
                target_gold_pct=Decimal("15"),
                allocation_targets=[
                    {
                        "dimension": "region",
                        "key": "china",
                        "pct_min": "5",
                        "pct_max": "10",
                        "notes": "satellite geografico",
                    }
                ],
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_allocation_endpoint_uses_latest_snapshot_and_reports_unclassified(
    client, session_factory
):
    await _seed(session_factory)

    resp = await client.get("/api/portfolio/allocation", params={"budget": "100"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["directive"]["slug"] == "test-active"
    assert [s["position_count"] for s in body["snapshots"]] == [4]
    # 500 classified + 500 unclassified; the stale 9.999 snapshot is excluded.
    assert Decimal(body["classified_total_eur"]) == Decimal("500.00")
    assert Decimal(body["unclassified_total_eur"]) == Decimal("500.00")
    assert [u["instrument_name"] for u in body["unclassified"]] == ["Mystery Fund"]
    assert body["warnings"]

    matrix = {
        (c["region"], c["sector"]): Decimal(c["amount_eur"])
        for c in body["breakdown"]["region_sector_matrix"]
    }
    assert matrix[("europe", "tech")] == Decimal("300.00")

    gaps = {g["key"]: g for g in body["gaps"]}
    # Targets on 500 + 100 = 600 post-money.
    assert Decimal(gaps["equity"]["target_eur"]) == Decimal("390.00")
    assert Decimal(gaps["bond"]["target_eur"]) == Decimal("120.00")
    assert gaps["china"]["status"] == "under"

    # Budget split over asset classes only (the one non-overlapping dimension).
    assert body["proposal_dimension"] == "asset_class"
    slices = {s["key"]: Decimal(s["amount_eur"]) for s in body["proposal"]["slices"]}
    # equity deficit 90, gold deficit 40, bond is over target: no bond slice.
    assert set(slices) == {"equity", "gold"}
    assert sum(slices.values()) == Decimal("100.00")


@pytest.mark.asyncio
async def test_allocation_endpoint_404s_without_an_active_directive(
    client, session_factory
):
    """Targets are agreed with the owner: no directive, no invented default."""
    await _seed(session_factory)

    resp = await client.get(
        "/api/portfolio/allocation", params={"directive_slug": "non-esiste"}
    )

    assert resp.status_code == 404
