"""Persona selection. Same two-step shape as publishers, run afterwards.

The ordering is a real dependency, not a convenience: `publisher_reach` asks
whether a persona is actually present on the publishers being bought, which
cannot be answered until the publisher plan exists. That costs a sequential
round-trip and is worth it — a persona score that ignores reachability
recommends audiences the campaign cannot serve.
"""

from __future__ import annotations

import logging

from app.catalog import loader
from app.core.scoring import (
    MAX_PERSONAS,
    MIN_PERSONAS,
    PERSONA_THRESHOLD,
    PERSONA_WEIGHTS,
    clamp_score,
    composite,
)
from app.gemini import schemas
from app.gemini.client import generate_json
from app.models.domain import Brief
from app.models.recommendation import AttributeScore, AudiencePlan, PublisherPlan, ScoredPersona

logger = logging.getLogger(__name__)


async def build_audience_plan(brief: Brief, publisher_plan: PublisherPlan) -> AudiencePlan:
    personas = loader.personas()
    publisher_context = _publisher_context(publisher_plan)

    raw_scores = await _score(brief, personas, publisher_context)
    ranked = _apply_rubric(raw_scores, personas)
    decisions = await _select(brief, ranked, publisher_context)

    scored: list[ScoredPersona] = []
    for entry in ranked:
        persona = entry["persona"]
        decision = decisions.get(persona.id, {})
        selected = decision.get("selected")
        if selected is None:
            selected = entry["provisional_selected"]

        override = _clean(decision.get("override_reason"))
        if bool(selected) == entry["provisional_selected"]:
            override = None

        scored.append(
            ScoredPersona(
                persona_id=persona.id,
                persona=persona,
                composite_score=entry["composite"],
                attribute_scores=entry["attribute_scores"],
                reason=decision.get("reason") or _fallback_reason(entry),
                selected=bool(selected),
                override_reason=override,
            )
        )

    scored.sort(key=lambda s: s.composite_score, reverse=True)
    selected_personas = _enforce_bounds(scored)

    return AudiencePlan(
        selected=[s for s in scored if s.persona_id in selected_personas],
        not_selected=[s for s in scored if s.persona_id not in selected_personas],
    )


def _enforce_bounds(scored: list[ScoredPersona]) -> set[str]:
    """Hold the 3-5 ad set count from the requirements.

    Trimming takes the lowest composites; topping up takes the highest unselected
    ones. Both mutate `selected` on the objects so the UI never shows a persona
    marked selected that has no ad set.
    """
    chosen = [s for s in scored if s.selected]

    if len(chosen) > MAX_PERSONAS:
        for persona in chosen[MAX_PERSONAS:]:
            persona.selected = False
        chosen = chosen[:MAX_PERSONAS]

    if len(chosen) < MIN_PERSONAS:
        chosen_ids = {s.persona_id for s in chosen}
        for persona in scored:
            if len(chosen) >= MIN_PERSONAS:
                break
            if persona.persona_id in chosen_ids:
                continue
            persona.selected = True
            chosen.append(persona)
            chosen_ids.add(persona.persona_id)

    return {s.persona_id for s in chosen}


async def _score(brief: Brief, personas, publisher_context: list[dict]) -> dict[str, list[AttributeScore]]:
    result = await generate_json(
        prompt_file="persona_scoring.md",
        payload={
            "brief": brief.model_dump(mode="json"),
            "recommended_publishers": publisher_context,
            "personas": loader.persona_catalog_for_prompt(),
        },
        response_schema=schemas.PERSONA_SCORING,
        temperature=0.3,
        label="persona_scoring",
    )

    by_id: dict[str, list[AttributeScore]] = {}
    for item in result.get("personas", []):
        persona_id = item.get("persona_id")
        if not persona_id:
            continue
        by_id[persona_id] = [
            AttributeScore(
                attribute=s["attribute"],
                score=clamp_score(s.get("score", 0.0)),
                reason=s.get("reason") or "",
            )
            for s in item.get("attribute_scores", [])
            if s.get("attribute") in PERSONA_WEIGHTS
        ]

    for persona in personas:
        by_id.setdefault(persona.id, [])
    return by_id


def _apply_rubric(raw_scores: dict[str, list[AttributeScore]], personas) -> list[dict]:
    ranked = []
    for persona in personas:
        attribute_scores = raw_scores.get(persona.id, [])
        score = composite(attribute_scores, PERSONA_WEIGHTS)
        ranked.append(
            {
                "persona": persona,
                "attribute_scores": attribute_scores,
                "composite": score,
                "provisional_selected": score >= PERSONA_THRESHOLD,
            }
        )
    ranked.sort(key=lambda e: e["composite"], reverse=True)
    return ranked


async def _select(brief: Brief, ranked: list[dict], publisher_context: list[dict]) -> dict[str, dict]:
    payload = {
        "brief": brief.model_dump(mode="json"),
        "recommended_publishers": publisher_context,
        "min_personas": MIN_PERSONAS,
        "max_personas": MAX_PERSONAS,
        "scored_personas": [
            {
                "persona_id": e["persona"].id,
                "persona_name": e["persona"].name,
                "composite_score": e["composite"],
                "provisionally_selected": e["provisional_selected"],
                "attribute_scores": [
                    {"attribute": a.attribute, "score": a.score, "reason": a.reason}
                    for a in e["attribute_scores"]
                ],
            }
            for e in ranked
        ],
    }

    result = await generate_json(
        prompt_file="persona_selection.md",
        payload=payload,
        response_schema=schemas.PERSONA_SELECTION,
        temperature=0.4,
        label="persona_selection",
    )
    return {item["persona_id"]: item for item in result.get("personas", []) if item.get("persona_id")}


def _publisher_context(publisher_plan: PublisherPlan) -> list[dict]:
    """Just enough publisher detail for reachability. Not the whole catalog again."""
    return [
        {
            "publisher_id": s.publisher.id,
            "name": s.publisher.name,
            "category": s.publisher.category,
            "subcategories": s.publisher.subcategories,
            "audience": s.publisher.audience.model_dump(),
            "avg_order_value_usd": s.publisher.avg_order_value_usd,
            "notes": s.publisher.notes,
        }
        for s in publisher_plan.recommended
    ]


def _fallback_reason(entry: dict) -> str:
    if not entry["attribute_scores"]:
        return "Not scored by the model; treated as neutral."
    best = max(entry["attribute_scores"], key=lambda a: a.score)
    return best.reason or f"Composite score {entry['composite']:.2f}."


def _clean(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
