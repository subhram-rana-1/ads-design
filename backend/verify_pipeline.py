"""Offline end-to-end check of the generation pipeline.

Replaces every Gemini call with a schema-shaped stub, then runs the real
orchestrator and asserts the invariants that matter. This verifies wiring,
parsing, scoring, allocation, the 3-5 ad set rule and exact budget summing
without needing an API key or spending a request.

Run inside the backend container:

    docker compose exec backend python verify_pipeline.py

It is a development script, not part of the app. Nothing imports it.
"""

from __future__ import annotations

import asyncio
import random

from app.catalog import loader
from app.models.domain import Brief
from app.services import campaign_builder, creative_engine, orchestrator, persona_engine, publisher_engine
from app.store.memory import store

random.seed(7)

PUB_ATTRS = ["relevance", "audience_overlap", "price_fit", "scale_fit", "context_fit"]
PERSONA_ATTRS = [
    "category_affinity", "messaging_fit", "price_alignment", "publisher_reach", "disinterest_conflict",
]


async def fake_generate_json(*, prompt_file, payload, response_schema, temperature=0.4, label=""):
    publishers = loader.publishers()
    personas = loader.personas()

    if prompt_file == "publisher_scoring.md":
        return {
            "publishers": [
                {
                    "publisher_id": p.id,
                    "attribute_scores": [
                        {"attribute": a, "reason": f"stub reason for {a}", "score": round(random.uniform(-1, 1), 2)}
                        for a in PUB_ATTRS
                    ],
                }
                for p in publishers
            ]
        }

    if prompt_file == "publisher_verdict.md":
        return {
            "catalog_fit": {"explanation": "Stubbed catalog fit.", "verdict": "strong"},
            "publishers": [
                {
                    "publisher_id": s["publisher_id"],
                    "reason": f"stub verdict for {s['publisher_name']}",
                    "verdict": s["provisional_verdict"],
                    "override_reason": None,
                }
                for s in payload["scored_publishers"]
            ],
        }

    if prompt_file == "persona_scoring.md":
        return {
            "personas": [
                {
                    "persona_id": p.id,
                    "attribute_scores": [
                        {"attribute": a, "reason": f"stub reason for {a}", "score": round(random.uniform(-1, 1), 2)}
                        for a in PERSONA_ATTRS
                    ],
                }
                for p in personas
            ]
        }

    if prompt_file == "persona_selection.md":
        return {
            "personas": [
                {
                    "persona_id": s["persona_id"],
                    "reason": "stub selection reason",
                    "selected": s["provisionally_selected"],
                    "override_reason": None,
                }
                for s in payload["scored_personas"]
            ]
        }

    if prompt_file == "creative_generation.md":
        return {
            "ad_sets": [
                {
                    "persona_id": p["persona_id"],
                    "creatives": [
                        {
                            "angle": angle,
                            # Deliberately over-length, to prove the trimmer holds.
                            "headline": f"A stub headline for {p['name']} that is far too long to fit the sixty character slot",
                            "body": "Stub body copy. " * 20,
                            "persona_fit_note": "stub fit note",
                        }
                        for angle in ["benefit_led", "social_proof", "offer_led"]
                    ],
                }
                for p in payload["personas"]
            ]
        }

    if prompt_file == "campaign_strategy.md":
        return {
            "bid_strategy_rationale": "Stub bid rationale.",
            "primary_kpi": "Cost per first order",
            "brand_safety_notes": "Stub brand safety note.",
            "allocation_rationales": [
                {"publisher_id": a["publisher_id"], "rationale": f"stub rationale for {a['publisher_name']}"}
                for a in payload["allocation"]
            ],
        }

    raise AssertionError(f"Unstubbed prompt: {prompt_file}")


def patch() -> None:
    for module in (publisher_engine, persona_engine, creative_engine, campaign_builder):
        module.generate_json = fake_generate_json


async def run_case(name: str, brief: Brief) -> None:
    session = store.create_session(name)
    session.brief = brief
    session.brief.is_complete = True

    await orchestrator.run_generation(session.id)

    assert session.status == "ready", f"{name}: status={session.status} error={session.error}"
    rec = store.get_recommendation(session.recommendation_id)

    plan = rec.publisher_plan
    config = rec.campaign_config

    assert len(plan.recommended) + len(plan.excluded) == 20, "all 20 publishers must get a verdict"
    for s in plan.recommended + plan.excluded:
        assert len(s.attribute_scores) == 5, f"{s.publisher_id} missing attributes"
        assert all(-1.0 <= a.score <= 1.0 for a in s.attribute_scores), "score out of range"
        assert s.reason, "empty reason"
        assert s.publisher.cpm_usd > 0, "publisher not hydrated with a CPM"

    audiences = rec.audience_plan
    assert len(audiences.selected) + len(audiences.not_selected) == 10, "all 10 personas accounted for"
    assert 3 <= len(audiences.selected) <= 5, f"selected {len(audiences.selected)} personas"
    assert all(len(a.creatives) == 3 for a in rec.ad_sets), "every ad set needs 3 creatives"
    assert len(rec.ad_sets) == len(audiences.selected), "one ad set per selected persona"

    for ad_set in rec.ad_sets:
        for creative in ad_set.creatives:
            assert len(creative.headline) <= 60, f"headline {len(creative.headline)} chars"
            assert len(creative.body) <= 160, f"body {len(creative.body)} chars"
        angles = [c.angle for c in ad_set.creatives]
        assert len(set(angles)) == 3, f"duplicate angles: {angles}"

    total = round(sum(a.daily_budget_usd for a in config.allocation), 2)
    assert abs(total - config.budget.daily_usd) < 0.005, f"allocation sums to {total}, budget {config.budget.daily_usd}"
    assert config.measurement.break_even_cpa_usd > 0
    assert len(config.allocation) <= 6
    assert set(config.targeting.persona_ids) == {s.persona_id for s in audiences.selected}
    assert config.brand_safety.excluded_publisher_ids == [s.publisher_id for s in plan.excluded]

    bid = config.bid_strategy
    print(
        f"  {name:<34} {len(plan.recommended):>2} pub / {len(audiences.selected)} personas / "
        f"{sum(len(a.creatives) for a in rec.ad_sets)} creatives | "
        f"${config.budget.daily_usd:>9,.2f}/day -> {total:>9,.2f} exact | "
        f"{bid.type} {config.objective}"
    )


async def main() -> None:
    patch()
    print("Running pipeline with stubbed LLM responses\n")

    cases = [
        ("dog food, $300/day", Brief(business_overview="Premium grain-free dog food for senior dogs.",
                                     daily_budget_usd=300, average_product_value_usd=70,
                                     campaign_objective="conversions")),
        ("tiny budget, $20/day", Brief(business_overview="Protein bars.",
                                       daily_budget_usd=20, average_product_value_usd=12,
                                       campaign_objective="conversions")),
        ("large budget, $5000/day", Brief(business_overview="Italian leather handbags.",
                                          daily_budget_usd=5000, average_product_value_usd=1200,
                                          campaign_objective="awareness")),
        ("fractional budget, $99.50/day", Brief(business_overview="Linen bedding.",
                                                daily_budget_usd=99.5, average_product_value_usd=180,
                                                campaign_objective="traffic")),
        ("no objective given", Brief(business_overview="Refillable cleaning products.",
                                     daily_budget_usd=450, average_product_value_usd=35)),
    ]

    for name, brief in cases:
        await run_case(name, brief)

    print(f"\nAll {len(cases)} cases passed every invariant.")
    print(f"Store: {store.stats()}")


if __name__ == "__main__":
    asyncio.run(main())
