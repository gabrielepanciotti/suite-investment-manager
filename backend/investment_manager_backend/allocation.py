"""Portfolio allocation engine — pure functions, no database, no I/O.

This module is the answer to a concrete problem: the classification of each
instrument and the target allocation used to live as hand-written constants
inside one-off scripts in `old/`, so "what if bonds were 20% instead of 30%"
meant editing and re-running code. Here, every number is *derived*: feed in
the positions, their classifications and a directive, and the breakdown, the
gaps and the budget split come out. Change one target in the directive and
everything downstream changes with it — that is the whole point, and
`tests/test_allocation.py` pins it as a property.

Design rules, in order of importance:

1. **Pure.** Nothing in here touches Postgres, the clock, or the network. The
   caller (`routers/portfolio.py`) loads rows and converts them into the small
   frozen dataclasses below; the engine only computes. That is what makes it
   testable with fabricated data and reviewable without a DB.
2. **`Decimal` everywhere.** Money and percentages are never floats. Rounding
   is explicit (`ROUND_HALF_UP`, 2 decimals for EUR) and budget splits use
   largest-remainder so the parts add up to the budget to the cent.
3. **Unknown is never a default.** A position with no classification goes to
   an explicit `unclassified` list; it is counted in the portfolio total (the
   money exists) but never attributed to an asset class, region or sector it
   was not verified to belong to. A missing `region`/`sector`/`size_bucket` is
   bucketed under the reserved key `UNSPECIFIED`, which reads as "we don't
   know" rather than silently vanishing from the percentages.
4. **The engine does not pick instruments.** `propose_allocation` says how much
   money each under-target class should receive; *which* ETF to buy is a
   research/human judgement, and the node never executes an order anyway
   (`.claude/docs/project_spec.md`).

On overlapping targets: `asset_class` is a partition of the portfolio
(every classified euro is in exactly one of equity/bond/gold/cash), so a
budget split over its gaps is sound. `region`, `sector` and `size_bucket`
targets are *overlays* — a single position is simultaneously 'europe' and
'tech' — so their gaps are constraints to read, not a second partition to
split the same money over. `compute_gaps` returns them all, tagged with their
`dimension`; it is the caller's job to split a budget over one dimension at a
time (see `Gap.dimension` and the router's `PARTITION_DIMENSION`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")
HUNDRED = Decimal("100")

#: Reserved bucket key for a classified position whose region/sector/size is
#: not known. Distinct from `unclassified` (no classification row at all).
UNSPECIFIED = "unspecified"

#: The one dimension whose keys partition the portfolio without overlap.
PARTITION_DIMENSION = "asset_class"


def _eur(value: Decimal) -> Decimal:
    """Round an amount to cents, half-up (bankers' rounding would drift)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _pct(part: Decimal, total: Decimal) -> Decimal:
    """`part` as a percentage of `total`, 2 decimals. 0 when total is 0."""
    if total == ZERO:
        return ZERO
    return (part * HUNDRED / total).quantize(CENT, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionValue:
    """One held position, reduced to what the engine needs: name + value.

    `market_value` is in EUR. The node stores every position in EUR already
    (`db.Position.currency` defaults to EUR); a non-EUR position would have to
    be converted *before* reaching here, deliberately: the engine must never
    invent an exchange rate.
    """

    instrument_name: str
    market_value: Decimal
    broker_slug: str | None = None


@dataclass(frozen=True)
class Classification:
    """What an instrument is (mirror of `db.InstrumentClassification`)."""

    instrument_name: str
    asset_class: str
    region: str | None = None
    sector: str | None = None
    size_bucket: str | None = None
    isin: str | None = None
    confirmed: bool = False
    notes: str | None = None


@dataclass(frozen=True)
class AllocationTarget:
    """One non-asset-class target from `directives.allocation_targets`.

    `pct_max is None` means *uncapped on purpose* (the directive's note must
    say why). `pct_min is None` means "no floor", e.g. a pure ceiling.
    """

    dimension: str
    key: str
    pct_min: Decimal | None = None
    pct_max: Decimal | None = None
    notes: str | None = None


@dataclass(frozen=True)
class DirectiveTargets:
    """The parametric part of a directive, as the engine sees it.

    Deliberately not the ORM object: the engine takes the *numbers*, so a test
    can pass two different directives over the same positions and check that
    everything recomputes.
    """

    slug: str
    target_equity_pct: Decimal | None = None
    target_bonds_pct: Decimal | None = None
    target_gold_pct: Decimal | None = None
    allocation_targets: tuple[AllocationTarget, ...] = ()


# --------------------------------------------------------------------------
# B1 — classify
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifiedPosition:
    position: PositionValue
    classification: Classification


@dataclass(frozen=True)
class ClassificationResult:
    classified: tuple[ClassifiedPosition, ...]
    unclassified: tuple[PositionValue, ...]

    @property
    def classified_eur(self) -> Decimal:
        return _eur(sum((c.position.market_value for c in self.classified), ZERO))

    @property
    def unclassified_eur(self) -> Decimal:
        return _eur(sum((p.market_value for p in self.unclassified), ZERO))


def classify_positions(
    positions: list[PositionValue] | tuple[PositionValue, ...],
    classifications: list[Classification] | tuple[Classification, ...],
) -> ClassificationResult:
    """Split positions into classified and unclassified, joining on name.

    The join key is `instrument_name`, matched exactly — no fuzzy matching:
    guessing that "Tesla Inc." is the "Tesla" we classified is precisely the
    kind of invention this node forbids. An unmatched position is returned in
    `unclassified` so the caller can surface it; it is never dropped and never
    given a default class.
    """
    by_name = {c.instrument_name: c for c in classifications}
    classified: list[ClassifiedPosition] = []
    unclassified: list[PositionValue] = []
    for position in positions:
        match = by_name.get(position.instrument_name)
        if match is None:
            unclassified.append(position)
        else:
            classified.append(ClassifiedPosition(position=position, classification=match))
    return ClassificationResult(
        classified=tuple(classified), unclassified=tuple(unclassified)
    )


# --------------------------------------------------------------------------
# B2 — breakdown
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakdownEntry:
    key: str
    amount_eur: Decimal
    pct: Decimal
    instruments: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatrixCell:
    region: str
    sector: str
    amount_eur: Decimal
    pct: Decimal
    instruments: tuple[str, ...] = ()


@dataclass(frozen=True)
class Breakdown:
    """Aggregates over the classified positions.

    `total_eur` is the total of the *classified* money only, and every `pct`
    is a percentage of it. Unclassified money is reported separately by the
    caller: mixing it into the denominator would make the percentages depend
    on how much we happen not to know yet, which is the worst possible
    property for a target comparison.
    """

    total_eur: Decimal
    by_asset_class: tuple[BreakdownEntry, ...] = ()
    by_region: tuple[BreakdownEntry, ...] = ()
    by_sector: tuple[BreakdownEntry, ...] = ()
    by_size_bucket: tuple[BreakdownEntry, ...] = ()
    region_sector_matrix: tuple[MatrixCell, ...] = ()

    def amount_for(self, dimension: str, key: str) -> Decimal:
        """Current EUR held in one bucket of one dimension (0 if absent)."""
        entries = {
            "asset_class": self.by_asset_class,
            "region": self.by_region,
            "sector": self.by_sector,
            "size_bucket": self.by_size_bucket,
        }.get(dimension)
        if entries is None:
            raise ValueError(f"unknown breakdown dimension: {dimension!r}")
        for entry in entries:
            if entry.key == key:
                return entry.amount_eur
        return ZERO


def _group(
    classified: tuple[ClassifiedPosition, ...],
    attribute: str,
    total: Decimal,
) -> tuple[BreakdownEntry, ...]:
    sums: dict[str, Decimal] = {}
    names: dict[str, list[str]] = {}
    for item in classified:
        key = getattr(item.classification, attribute) or UNSPECIFIED
        sums[key] = sums.get(key, ZERO) + item.position.market_value
        names.setdefault(key, []).append(item.position.instrument_name)
    return tuple(
        BreakdownEntry(
            key=key,
            amount_eur=_eur(amount),
            pct=_pct(amount, total),
            instruments=tuple(sorted(names[key])),
        )
        # Biggest first: this is read by a human deciding what to buy.
        for key, amount in sorted(sums.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def build_breakdown(
    classified: ClassificationResult | tuple[ClassifiedPosition, ...] | list,
) -> Breakdown:
    """Aggregate classified positions by asset class, region, sector, size —
    plus the region x sector matrix.

    The matrix is the structure the portfolio is actually reasoned about with
    ("each geographic satellite also has a sector satellite inside it"), so it
    is a first-class output rather than something the reader has to
    reconstruct by crossing two flat lists.
    """
    if isinstance(classified, ClassificationResult):
        items = classified.classified
    else:
        items = tuple(classified)

    total = sum((i.position.market_value for i in items), ZERO)

    cells: dict[tuple[str, str], Decimal] = {}
    cell_names: dict[tuple[str, str], list[str]] = {}
    for item in items:
        key = (
            item.classification.region or UNSPECIFIED,
            item.classification.sector or UNSPECIFIED,
        )
        cells[key] = cells.get(key, ZERO) + item.position.market_value
        cell_names.setdefault(key, []).append(item.position.instrument_name)

    matrix = tuple(
        MatrixCell(
            region=region,
            sector=sector,
            amount_eur=_eur(amount),
            pct=_pct(amount, total),
            instruments=tuple(sorted(cell_names[(region, sector)])),
        )
        for (region, sector), amount in sorted(
            cells.items(), key=lambda kv: (-kv[1], kv[0])
        )
    )

    return Breakdown(
        total_eur=_eur(total),
        by_asset_class=_group(items, "asset_class", total),
        by_region=_group(items, "region", total),
        by_sector=_group(items, "sector", total),
        by_size_bucket=_group(items, "size_bucket", total),
        region_sector_matrix=matrix,
    )


# --------------------------------------------------------------------------
# B3 — gaps
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    """Distance between a bucket's current value and its target value.

    Sign convention, fixed once and used everywhere downstream:
    **`gap_eur < 0` means under target (money to add)**, `> 0` means over
    target. `target_eur` is computed on the total *after* the new money, so
    "how much equity should I hold once I've invested these 2.200 €" is the
    question actually being answered.

    `status` is one of 'under', 'over', 'within' (inside a min-max band, so
    nothing to do) or 'on_target'.
    """

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
    notes: str | None = None

    @property
    def is_under(self) -> bool:
        return self.gap_eur < ZERO

    @property
    def deficit_eur(self) -> Decimal:
        """How much is missing (positive), 0 when at or above target."""
        return -self.gap_eur if self.gap_eur < ZERO else ZERO


def _exact_gap(
    *,
    dimension: str,
    key: str,
    current: Decimal,
    total_now: Decimal,
    total_post: Decimal,
    target_pct: Decimal,
    notes: str | None = None,
) -> Gap:
    target = _eur(total_post * target_pct / HUNDRED)
    gap = _eur(current - target)
    return Gap(
        dimension=dimension,
        key=key,
        current_eur=_eur(current),
        current_pct=_pct(current, total_now),
        target_pct=target_pct,
        target_pct_min=target_pct,
        target_pct_max=target_pct,
        target_eur=target,
        gap_eur=gap,
        status="on_target" if gap == ZERO else ("under" if gap < ZERO else "over"),
        notes=notes,
    )


def _band_gap(
    *,
    target: AllocationTarget,
    current: Decimal,
    total_now: Decimal,
    total_post: Decimal,
) -> Gap:
    """Gap against a min-max band.

    Inside the band there is nothing to do, so the gap is 0 and the status is
    'within' — not 'on_target', which would wrongly suggest an exact hit. An
    absent `pct_max` means no ceiling (an explicit choice in this node's
    directive: the tech overweight is the thesis, not an oversight), so a
    position above the floor can never be reported as 'over'.
    """
    floor = _eur(total_post * target.pct_min / HUNDRED) if target.pct_min is not None else None
    ceiling = _eur(total_post * target.pct_max / HUNDRED) if target.pct_max is not None else None

    if floor is not None and current < floor:
        target_eur, status, applied = floor, "under", target.pct_min
    elif ceiling is not None and current > ceiling:
        target_eur, status, applied = ceiling, "over", target.pct_max
    else:
        target_eur, status, applied = _eur(current), "within", None

    return Gap(
        dimension=target.dimension,
        key=target.key,
        current_eur=_eur(current),
        current_pct=_pct(current, total_now),
        target_pct=applied,
        target_pct_min=target.pct_min,
        target_pct_max=target.pct_max,
        target_eur=target_eur,
        gap_eur=_eur(current) - target_eur,
        status=status,
        notes=target.notes,
    )


def compute_gaps(
    breakdown: Breakdown,
    directive: DirectiveTargets,
    new_money: Decimal = ZERO,
) -> list[Gap]:
    """Current vs target for every target the directive declares.

    Targets come from two places and both are honoured here: the three
    asset-class columns (`target_equity_pct`/`_bonds_pct`/`_gold_pct`) and the
    free-form `allocation_targets` rows (region / sector / size_bucket bands).

    Targets are applied to `breakdown.total_eur + new_money`, i.e. the
    portfolio as it will be once the new money is invested. This is the
    function that makes the whole thing parametric: nothing here knows what
    65/20/15 is — change the directive and every gap changes.
    """
    total_now = breakdown.total_eur
    total_post = total_now + new_money

    gaps: list[Gap] = []

    asset_class_targets = (
        ("equity", directive.target_equity_pct),
        ("bond", directive.target_bonds_pct),
        ("gold", directive.target_gold_pct),
    )
    for key, pct in asset_class_targets:
        if pct is None:
            continue
        gaps.append(
            _exact_gap(
                dimension=PARTITION_DIMENSION,
                key=key,
                current=breakdown.amount_for(PARTITION_DIMENSION, key),
                total_now=total_now,
                total_post=total_post,
                target_pct=pct,
            )
        )

    for target in directive.allocation_targets:
        gaps.append(
            _band_gap(
                target=target,
                current=breakdown.amount_for(target.dimension, target.key),
                total_now=total_now,
                total_post=total_post,
            )
        )

    return gaps


# --------------------------------------------------------------------------
# B4 — budget proposal
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AllocationSlice:
    dimension: str
    key: str
    deficit_eur: Decimal
    amount_eur: Decimal
    covers_full_gap: bool


@dataclass(frozen=True)
class AllocationProposal:
    """How to split a budget across the under-target buckets.

    `unallocated_eur` is money the targets do not ask for. It is reported, not
    spread around: inventing a destination for leftover cash would be a
    recommendation nobody made. Likewise the proposal names *classes*, never
    instruments — picking the ETF is research, and this node never places an
    order.
    """

    budget_eur: Decimal
    total_deficit_eur: Decimal
    allocated_eur: Decimal
    unallocated_eur: Decimal
    slices: tuple[AllocationSlice, ...] = ()
    notes: tuple[str, ...] = field(default=())


def propose_allocation(
    gaps: list[Gap] | tuple[Gap, ...],
    budget: Decimal,
) -> AllocationProposal:
    """Split `budget` across under-target buckets, proportionally to the gap.

    Two cases, both explicit:

    * **budget < total deficit** — every bucket gets its share of the budget,
      in proportion to its deficit. Cents are assigned by largest remainder so
      the slices sum to the budget exactly.
    * **budget >= total deficit** — every bucket gets exactly what it is
      missing and the remainder is returned as `unallocated_eur`, never
      distributed by default.

    The caller must pass gaps from a *single* non-overlapping dimension (see
    the module docstring): splitting one budget over both 'equity' and
    'europe' would spend the same euro twice.
    """
    budget = _eur(budget) if budget > ZERO else ZERO
    deficits = [g for g in gaps if g.is_under]
    total_deficit = _eur(sum((g.deficit_eur for g in deficits), ZERO))
    notes: list[str] = []

    if not deficits or budget == ZERO:
        if not deficits and budget > ZERO:
            notes.append(
                "Nessuna classe sotto target: il budget resta interamente non "
                "allocato, la destinazione è una decisione da prendere, non un "
                "calcolo."
            )
        return AllocationProposal(
            budget_eur=budget,
            total_deficit_eur=total_deficit,
            allocated_eur=ZERO,
            unallocated_eur=budget,
            slices=tuple(
                AllocationSlice(
                    dimension=g.dimension,
                    key=g.key,
                    deficit_eur=g.deficit_eur,
                    amount_eur=ZERO,
                    covers_full_gap=False,
                )
                for g in deficits
            ),
            notes=tuple(notes),
        )

    if budget >= total_deficit:
        slices = tuple(
            AllocationSlice(
                dimension=g.dimension,
                key=g.key,
                deficit_eur=g.deficit_eur,
                amount_eur=g.deficit_eur,
                covers_full_gap=True,
            )
            for g in deficits
        )
        unallocated = _eur(budget - total_deficit)
        if unallocated > ZERO:
            notes.append(
                "Il budget copre tutti i gap: l'eccedenza è dichiarata come "
                "unallocated_eur e non viene spalmata d'ufficio."
            )
        return AllocationProposal(
            budget_eur=budget,
            total_deficit_eur=total_deficit,
            allocated_eur=total_deficit,
            unallocated_eur=unallocated,
            slices=slices,
            notes=tuple(notes),
        )

    # Proportional split with largest-remainder cent assignment.
    raw = [(g, budget * g.deficit_eur / total_deficit) for g in deficits]
    floors = [(g, value.quantize(CENT, rounding=ROUND_DOWN)) for g, value in raw]
    assigned = sum((amount for _, amount in floors), ZERO)
    leftover_cents = int((budget - assigned) / CENT)
    remainders = sorted(
        range(len(raw)),
        key=lambda i: (-(raw[i][1] - floors[i][1]), -raw[i][0].deficit_eur),
    )
    amounts = [amount for _, amount in floors]
    for i in remainders[:leftover_cents]:
        amounts[i] += CENT

    slices = tuple(
        AllocationSlice(
            dimension=g.dimension,
            key=g.key,
            deficit_eur=g.deficit_eur,
            amount_eur=amount,
            covers_full_gap=amount >= g.deficit_eur,
        )
        for (g, _), amount in zip(raw, amounts, strict=True)
    )
    notes.append(
        "Budget inferiore alla somma dei gap: ripartito in proporzione al gap, "
        "nessuna classe viene portata a target."
    )
    return AllocationProposal(
        budget_eur=budget,
        total_deficit_eur=total_deficit,
        allocated_eur=_eur(sum(amounts, ZERO)),
        unallocated_eur=ZERO,
        slices=slices,
        notes=tuple(notes),
    )
