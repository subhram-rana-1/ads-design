"""Budget allocation across recommended publishers.

Score-weighted split with a floor, a cap, and exact-sum rounding. This is the
one piece of arithmetic in the system with real edge cases, so they are handled
explicitly rather than discovered live.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_SHARE = 0.05
MAX_SHARE = 0.40

# Spreading a daily budget across fifteen publishers is not thoroughness, it is
# bad media planning: every line is too thin to learn anything from.
MAX_PUBLISHERS = 6

# Below this a line item cannot buy enough impressions to be worth reporting on,
# so the publisher count drops instead.
MIN_DAILY_PER_PUBLISHER_USD = 5.0

_WATER_FILL_ITERATIONS = 10


@dataclass(frozen=True)
class Allocation:
    publisher_id: str
    share_pct: float
    daily_budget_usd: float


def allocate(
    candidates: list[tuple[str, float]],
    daily_budget_usd: float,
) -> list[Allocation]:
    """Split `daily_budget_usd` across `candidates`.

    `candidates` is `(publisher_id, composite_score)` ordered best first.

    1. Take the top `MAX_PUBLISHERS`, reduced further if the budget cannot give
       each line at least `MIN_DAILY_PER_PUBLISHER_USD`.
    2. Weight by composite, floored at 0.01 so a barely-positive publisher gets
       a sliver instead of causing a divide-by-zero.
    3. Water-fill: clamp to [MIN_SHARE, MAX_SHARE] and redistribute the excess
       across the unclamped entries until stable.
    4. Convert to cents, floor, and hand the remainder to the top-scoring
       publisher so the total equals the budget exactly.
    """
    if not candidates or daily_budget_usd <= 0:
        return []

    count = min(
        len(candidates),
        MAX_PUBLISHERS,
        max(1, int(daily_budget_usd // MIN_DAILY_PER_PUBLISHER_USD)),
    )
    chosen = candidates[:count]

    weights = [max(score, 0.01) for _, score in chosen]
    total_weight = sum(weights)
    shares = [w / total_weight for w in weights]
    shares = _water_fill(shares)

    return _to_dollars(chosen, shares, daily_budget_usd)


def _water_fill(shares: list[float]) -> list[float]:
    """Clamp shares into [MIN_SHARE, MAX_SHARE], keeping the sum at 1.0.

    The bounds are only applied when they are satisfiable. With two publishers
    a 40% cap is arithmetically impossible (2 x 0.40 < 1.0), so the cap is
    dropped rather than producing a plan that does not spend the budget.
    """
    count = len(shares)
    low = MIN_SHARE if count * MIN_SHARE <= 1.0 else 0.0
    high = MAX_SHARE if count * MAX_SHARE >= 1.0 else 1.0

    current = list(shares)
    for _ in range(_WATER_FILL_ITERATIONS):
        clamped = [min(max(s, low), high) for s in current]
        excess = 1.0 - sum(clamped)
        if abs(excess) < 1e-9:
            return clamped

        # Which entries can absorb the excess depends on its sign. An entry
        # sitting exactly at the floor cannot give anything away but can still
        # take more; treating both bounds as "stuck" was wrong and fell through
        # to a normalise that quietly discarded the cap — a 3-way split came
        # back as 80/10/10.
        if excess > 0:
            movable = [i for i, s in enumerate(clamped) if s < high]
        else:
            movable = [i for i, s in enumerate(clamped) if s > low]

        if not movable:
            # Genuinely unsatisfiable: the bounds cannot sum to 1.0 at this
            # count. Normalising is the only option left.
            total = sum(clamped) or 1.0
            return [s / total for s in clamped]

        movable_total = sum(clamped[i] for i in movable)
        for i in movable:
            weight = clamped[i] / movable_total if movable_total > 0 else 1.0 / len(movable)
            clamped[i] += excess * weight
        current = clamped

    # Re-clamp after the last pass so the bounds hold even if the loop ran out
    # of iterations before converging.
    current = [min(max(s, low), high) for s in current]

    total = sum(current) or 1.0
    return [s / total for s in current]


def _to_dollars(
    chosen: list[tuple[str, float]],
    shares: list[float],
    daily_budget_usd: float,
) -> list[Allocation]:
    """Round to cents such that the allocations sum to the budget exactly.

    Working in integer cents and giving the remainder to the highest-scoring
    publisher is what makes `sum(allocations) == daily_budget_usd` true rather
    than approximately true, which matters because the number is on screen next
    to the budget the advertiser typed.
    """
    total_cents = int(round(daily_budget_usd * 100))
    cents = [int(share * total_cents) for share in shares]
    cents[0] += total_cents - sum(cents)

    return [
        Allocation(
            publisher_id=publisher_id,
            share_pct=round(c / total_cents * 100, 2) if total_cents else 0.0,
            daily_budget_usd=round(c / 100, 2),
        )
        for (publisher_id, _), c in zip(chosen, cents)
    ]
