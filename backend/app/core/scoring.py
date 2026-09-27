"""Composite scoring.

The model assigns per-attribute scores and writes reasons. This module turns
those scores into a single number. Keeping the arithmetic here rather than
asking the model for a composite means two runs of the same brief produce the
same ranking, and the ranking can be checked by hand.
"""

from __future__ import annotations

from typing import Iterable

from app.models.recommendation import AttributeScore

PUBLISHER_WEIGHTS: dict[str, float] = {
    "relevance": 0.30,
    "audience_overlap": 0.25,
    "price_fit": 0.20,
    "scale_fit": 0.15,
    "context_fit": 0.10,
}

PERSONA_WEIGHTS: dict[str, float] = {
    "category_affinity": 0.30,
    "messaging_fit": 0.25,
    "price_alignment": 0.20,
    "publisher_reach": 0.15,
    "disinterest_conflict": 0.10,
}

# A publisher clearing this on the weighted composite is provisionally
# recommended. Set where it is because a publisher scoring below ~0.35 is
# generally positive on one attribute and neutral-to-negative on the rest,
# which is not a buy.
RECOMMEND_THRESHOLD = 0.35

# Personas need a lower bar. There are only 10 of them and the brief asks for
# 3-5, so the threshold is a floor on plausibility, not a selection mechanism.
PERSONA_THRESHOLD = 0.20

MIN_PERSONAS = 3
MAX_PERSONAS = 5


def composite(attribute_scores: Iterable[AttributeScore], weights: dict[str, float]) -> float:
    """Weighted mean of the attribute scores, clamped to [-1, 1].

    An attribute the model failed to return counts as 0.0 rather than being
    dropped from the denominator. Dropping it would silently reward a model
    that skipped the attribute it scored worst on.
    """
    by_attribute = {s.attribute: s.score for s in attribute_scores}
    total_weight = sum(weights.values()) or 1.0
    weighted = sum(weights[name] * by_attribute.get(name, 0.0) for name in weights)
    return round(max(-1.0, min(1.0, weighted / total_weight)), 4)


def clamp_score(value: float) -> float:
    """Attribute scores arrive from an LLM. Trust the judgement, not the range."""
    return max(-1.0, min(1.0, float(value)))
