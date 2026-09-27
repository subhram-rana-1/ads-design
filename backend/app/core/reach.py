"""Deterministic audience reach.

`publisher_reach` asks whether a persona is demographically present on the
placements actually being bought. That is arithmetic over age bands and gender
splits, not a semantic judgement — so it is computed here rather than asked of
the model.

Two things fall out of that, and the second is the reason this module exists:

1. The number is reproducible and checkable by hand, like every other figure in
   the campaign.
2. It removes the only dependency persona scoring had on publisher selection.
   The remaining four persona attributes compare the persona against the brief
   alone, so all ten persona calls can now run concurrently with the twenty
   publisher calls instead of waiting behind them.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.models.recommendation import AttributeScore

if TYPE_CHECKING:  # pragma: no cover
    from app.models.domain import GenderSplit, Persona
    from app.models.recommendation import ScoredPublisher

_AGE = re.compile(r"(\d+)\s*-\s*(\d+)")

# Age carries more weight than gender: being the wrong age for a placement means
# the shopper is not there at all, while a gender skew only thins the audience.
AGE_WEIGHT = 0.6
GENDER_WEIGHT = 0.4


def publisher_reach(persona: "Persona", recommended: list["ScoredPublisher"]) -> AttributeScore:
    """Score one persona's demographic presence across the recommended buy."""
    if not recommended:
        return AttributeScore(
            attribute="publisher_reach",
            score=0.0,
            reason="No placements were selected, so reachability cannot be assessed.",
        )

    # Weighted by composite: being reachable on the placement taking most of the
    # budget matters more than being reachable on the smallest line.
    weights = [max(s.composite_score, 0.01) for s in recommended]
    fits = [
        AGE_WEIGHT * _age_overlap(persona.age_range, s.publisher.audience.age_skew)
        + GENDER_WEIGHT * _gender_fit(persona.gender_skew, s.publisher.audience.gender_split)
        for s in recommended
    ]

    raw = sum(f * w for f, w in zip(fits, weights)) / sum(weights)
    # raw is 0..1; map to the -1..1 scale the rest of the rubric uses, so an
    # average overlap lands near zero rather than looking like a positive.
    score = round(max(-1.0, min(1.0, raw * 2 - 1)), 2)

    best, best_fit = max(zip(recommended, fits), key=lambda pair: pair[1])
    return AttributeScore(
        attribute="publisher_reach",
        score=score,
        reason=(
            f"Demographic overlap across the selected placements averages {raw:.0%}, "
            f"weighted by how much budget each takes. Strongest on {best.publisher.name} "
            f"({best.publisher.audience.age_skew}, "
            f"{best.publisher.audience.gender_split.female:.0%} female) at {best_fit:.0%}."
        ),
    )


def _age_overlap(persona_range: str, publisher_range: str) -> float:
    """Share of the persona's age band that the publisher's audience covers.

    Deliberately asymmetric: the denominator is the persona's span, because the
    question is "can we reach this persona here", not "is this publisher's
    audience mostly this persona".
    """
    a = _parse(persona_range)
    b = _parse(publisher_range)
    if not a or not b:
        return 0.5  # unparseable band — neutral rather than a guess
    low, high = max(a[0], b[0]), min(a[1], b[1])
    if high <= low:
        return 0.0
    span = a[1] - a[0]
    return (high - low) / span if span else 0.0


def _gender_fit(persona_skew: str, split: "GenderSplit") -> float:
    """How well a publisher's gender split serves a persona's skew."""
    skew = (persona_skew or "").lower()
    female = split.female

    if "female-leaning" in skew:
        return min(1.0, female / 0.65)
    if skew.startswith("female"):
        return min(1.0, female / 0.80)
    if "male" in skew:
        return min(1.0, split.male / 0.60)
    # "balanced" — a 99%-female publisher serves a balanced persona badly, and
    # this is the term that says so.
    return max(0.0, 1.0 - 2 * abs(female - 0.5))


def _parse(value: str) -> tuple[int, int] | None:
    match = _AGE.search(value or "")
    return (int(match.group(1)), int(match.group(2))) if match else None
