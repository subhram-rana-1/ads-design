"""Creative generation.

One call produces every creative for every selected persona. Per-persona calls
were the obvious alternative and are worse: each call is blind to the others, so
three personas come back with three near-identical benefit-led headlines. A
single call lets the model see everything it is writing and keep the variants
genuinely distinct, which is the whole reason to generate variants at all.
"""

from __future__ import annotations

import logging

from app.gemini import schemas
from app.gemini.client import generate_json
from app.models.domain import Brief
from app.models.recommendation import AdSet, AudiencePlan, Creative, PublisherPlan

logger = logging.getLogger(__name__)

MAX_HEADLINE_CHARS = 60
MAX_BODY_CHARS = 160

ANGLES = ("benefit_led", "social_proof", "offer_led")


async def build_ad_sets(
    brief: Brief,
    audience_plan: AudiencePlan,
    publisher_plan: PublisherPlan,
) -> list[AdSet]:
    selected = audience_plan.selected
    if not selected:
        return []

    payload = {
        "brief": brief.model_dump(mode="json"),
        "placement_context": [
            {
                "name": s.publisher.name,
                "category": s.publisher.category,
                "notes": s.publisher.notes,
            }
            for s in publisher_plan.recommended
        ],
        "required_angles": list(ANGLES),
        "max_headline_chars": MAX_HEADLINE_CHARS,
        "max_body_chars": MAX_BODY_CHARS,
        "personas": [
            {
                "persona_id": s.persona.id,
                "name": s.persona.name,
                "description": s.persona.description,
                "age_range": s.persona.age_range,
                "gender_skew": s.persona.gender_skew,
                "price_sensitivity": s.persona.price_sensitivity,
                "typical_aov_usd": s.persona.typical_aov_usd,
                "messaging_preferences": s.persona.messaging_preferences,
                "disinterested_in": s.persona.disinterested_in,
                "why_selected": s.reason,
            }
            for s in selected
        ],
    }

    result = await generate_json(
        prompt_file="creative_generation.md",
        payload=payload,
        # Higher than the scoring calls. Judgement should be stable across runs;
        # ad copy should not be flat.
        temperature=0.9,
        response_schema=schemas.CREATIVE_GENERATION,
        label="creative_generation",
    )

    by_persona = {
        item["persona_id"]: item.get("creatives", [])
        for item in result.get("ad_sets", [])
        if item.get("persona_id")
    }

    ad_sets: list[AdSet] = []
    for scored in selected:
        raw = by_persona.get(scored.persona_id, [])
        if not raw:
            logger.warning("No creatives returned for %s", scored.persona_id)
            continue
        ad_sets.append(
            AdSet(
                persona_id=scored.persona_id,
                persona_name=scored.persona.name,
                creatives=_normalise(raw),
            )
        )
    return ad_sets


def _normalise(raw: list[dict]) -> list[Creative]:
    """Keep exactly one creative per angle, and hold the length limits.

    The limits are real placement constraints, not style preference — a headline
    that overflows its slot is truncated by the ad server, not by taste. Trimming
    at a word boundary here means an over-long headline degrades visibly but
    readably instead of ending mid-word.
    """
    seen: dict[str, Creative] = {}
    for item in raw:
        angle = item.get("angle")
        if angle not in ANGLES or angle in seen:
            continue
        seen[angle] = Creative(
            angle=angle,
            headline=_trim(item.get("headline", ""), MAX_HEADLINE_CHARS),
            body=_trim(item.get("body", ""), MAX_BODY_CHARS),
            persona_fit_note=(item.get("persona_fit_note") or "").strip(),
        )
    return [seen[angle] for angle in ANGLES if angle in seen]


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:") + "…"
