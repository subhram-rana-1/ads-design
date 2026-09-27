"""Assembles the campaign config.

Every number here is computed. The model contributes four pieces of prose: the
bid-strategy rationale, the primary KPI phrasing, the brand-safety note, and one
sentence per publisher on why it gets its share. Asking it for the numbers as
well would make the config unreproducible and the arithmetic unverifiable, for
no gain.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from app.core import economics
from app.core.allocation import allocate
from app.gemini import schemas
from app.gemini.client import generate_json
from app.models.domain import Brief
from app.models.recommendation import (
    AudiencePlan,
    BidStrategy,
    BrandSafety,
    BudgetPlan,
    CampaignConfig,
    Flight,
    FrequencyCap,
    Measurement,
    PublisherAllocation,
    PublisherPlan,
    Targeting,
)

logger = logging.getLogger(__name__)

FLIGHT_DAYS = 30
ATTRIBUTION_WINDOW_DAYS = 7

# Bid at 70% of break-even, not at it. The projection rests on assumed CTR and
# CVR; bidding at break-even means any error at all makes the campaign
# unprofitable. The 30% is headroom for the plan being wrong.
TARGET_CPA_HEADROOM = 0.70

_AGE_RANGE = re.compile(r"(\d+)\s*-\s*(\d+)")
_INCOME_TIER_ORDER = ["mid", "mid-high", "high"]

PRICING_MODEL_BY_OBJECTIVE = {
    "conversions": "CPA",
    "traffic": "CPC",
    "awareness": "CPM",
}


async def build_campaign_config(
    brief: Brief,
    publisher_plan: PublisherPlan,
    audience_plan: AudiencePlan,
) -> CampaignConfig:
    objective = brief.campaign_objective or "conversions"
    daily_budget = brief.daily_budget_usd or 0.0
    product_value = brief.average_product_value_usd or 0.0
    break_even = economics.break_even_cpa(product_value, brief.gross_margin_pct)

    recommended = publisher_plan.recommended
    allocations = allocate(
        [(s.publisher_id, s.composite_score) for s in recommended],
        daily_budget,
    )
    by_id = {s.publisher_id: s for s in recommended}

    lines: list[dict] = []
    for a in allocations:
        scored = by_id[a.publisher_id]
        projection = economics.project(
            daily_spend_usd=a.daily_budget_usd,
            cpm_usd=scored.publisher.cpm_usd,
            product_value_usd=product_value,
            gross_margin_pct=brief.gross_margin_pct,
        )
        lines.append({"allocation": a, "scored": scored, "projection": projection})

    strategy = await _strategy_prose(
        brief=brief,
        objective=objective,
        lines=lines,
        publisher_plan=publisher_plan,
        audience_plan=audience_plan,
        break_even=break_even,
    )
    rationales = strategy["allocation_rationales"]

    allocation_models = [
        PublisherAllocation(
            publisher_id=line["allocation"].publisher_id,
            publisher_name=line["scored"].publisher.name,
            daily_budget_usd=line["allocation"].daily_budget_usd,
            share_pct=line["allocation"].share_pct,
            cpm_usd=line["scored"].publisher.cpm_usd,
            est_impressions=line["projection"]["est_impressions"],
            est_clicks=line["projection"]["est_clicks"],
            est_conversions=line["projection"]["est_conversions"],
            est_cpa_usd=line["projection"]["est_cpa_usd"],
            rationale=rationales.get(
                line["allocation"].publisher_id,
                f"Ranked {line['scored'].composite_score:.2f} on the publisher rubric.",
            ),
        )
        for line in lines
    ]

    start = date.today()
    return CampaignConfig(
        objective=objective,
        pricing_model=PRICING_MODEL_BY_OBJECTIVE[objective],
        bid_strategy=_bid_strategy(
            objective=objective,
            break_even=break_even,
            daily_budget=daily_budget,
            lines=lines,
            rationale=strategy["bid_strategy_rationale"],
        ),
        budget=BudgetPlan(
            daily_usd=round(daily_budget, 2),
            suggested_total_usd=round(daily_budget * FLIGHT_DAYS, 2),
            flight_days=FLIGHT_DAYS,
            # Even pacing because the projection assumes steady-state delivery.
            # Accelerated pacing would spend the daily budget in the first hours
            # and invalidate the per-day numbers next to it.
            pacing="even",
        ),
        allocation=allocation_models,
        targeting=_targeting(audience_plan, publisher_plan),
        flight=Flight(
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=FLIGHT_DAYS)).isoformat(),
        ),
        # Three a day is the standard display frequency ceiling: enough for
        # recall, below the point where the same shopper starts resenting it.
        frequency_cap=FrequencyCap(impressions=3, per="day"),
        brand_safety=BrandSafety(
            excluded_publisher_ids=[s.publisher_id for s in publisher_plan.excluded],
            notes=strategy["brand_safety_notes"],
        ),
        measurement=Measurement(
            primary_kpi=strategy["primary_kpi"],
            break_even_cpa_usd=break_even,
            attribution_window_days=ATTRIBUTION_WINDOW_DAYS,
        ),
    )


def _bid_strategy(
    *,
    objective: str,
    break_even: float,
    daily_budget: float,
    lines: list[dict],
    rationale: str,
) -> BidStrategy:
    if objective == "conversions":
        return BidStrategy(
            type="target_cpa",
            target_cpa_usd=round(break_even * TARGET_CPA_HEADROOM, 2),
            rationale=rationale,
        )

    if objective == "traffic":
        total_clicks = sum(line["projection"]["est_clicks"] for line in lines)
        max_cpc = round(daily_budget / total_clicks, 2) if total_clicks else None
        return BidStrategy(type="max_cpc", max_cpc_usd=max_cpc, rationale=rationale)

    total_spend = sum(line["allocation"].daily_budget_usd for line in lines)
    weighted_cpm = (
        sum(line["allocation"].daily_budget_usd * line["scored"].publisher.cpm_usd for line in lines)
        / total_spend
        if total_spend
        else 0.0
    )
    return BidStrategy(type="fixed_cpm", cpm_bid_usd=round(weighted_cpm, 2), rationale=rationale)


def _targeting(audience_plan: AudiencePlan, publisher_plan: PublisherPlan) -> Targeting:
    """Unioned from the selected personas and the bought publishers, not invented.

    Targeting that does not follow from the plan above it is decoration. Every
    value here is traceable to a persona or a publisher in the campaign.
    """
    personas = [s.persona for s in audience_plan.selected]
    publishers = [s.publisher for s in publisher_plan.recommended]

    bounds = [_parse_age(p.age_range) for p in personas]
    bounds = [b for b in bounds if b]
    age_range = f"{min(b[0] for b in bounds)}-{max(b[1] for b in bounds)}" if bounds else "18-65"

    skews = {p.gender_skew for p in personas}
    if len(skews) == 1:
        gender_skew = next(iter(skews))
    elif any("female" in s for s in skews):
        gender_skew = "female-leaning"
    else:
        gender_skew = "balanced"

    tiers = {p.audience.income_tier for p in publishers}
    geos = sorted({geo for p in publishers for geo in p.audience.top_geos})
    if "nationwide" in geos:
        geos = ["nationwide"]

    return Targeting(
        persona_ids=[p.id for p in personas],
        age_range=age_range,
        gender_skew=gender_skew,
        income_tiers=[t for t in _INCOME_TIER_ORDER if t in tiers],
        geos=geos,
        category_context=sorted({p.category for p in publishers}),
    )


def _parse_age(value: str) -> tuple[int, int] | None:
    match = _AGE_RANGE.search(value or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


async def _strategy_prose(
    *,
    brief: Brief,
    objective: str,
    lines: list[dict],
    publisher_plan: PublisherPlan,
    audience_plan: AudiencePlan,
    break_even: float,
) -> dict:
    """Ask for the qualitative half only. The payload already contains every
    computed number, so the rationale can cite real figures instead of guessing."""
    payload = {
        "brief": brief.model_dump(mode="json"),
        "objective": objective,
        "pricing_model": PRICING_MODEL_BY_OBJECTIVE[objective],
        "break_even_cpa_usd": break_even,
        "target_cpa_usd": round(break_even * TARGET_CPA_HEADROOM, 2),
        "allocation": [
            {
                "publisher_id": line["allocation"].publisher_id,
                "publisher_name": line["scored"].publisher.name,
                "category": line["scored"].publisher.category,
                "composite_score": line["scored"].composite_score,
                "daily_budget_usd": line["allocation"].daily_budget_usd,
                "share_pct": line["allocation"].share_pct,
                "cpm_usd": line["scored"].publisher.cpm_usd,
                "est_impressions": line["projection"]["est_impressions"],
                "est_conversions": line["projection"]["est_conversions"],
                "est_cpa_usd": line["projection"]["est_cpa_usd"],
            }
            for line in lines
        ],
        "excluded_publishers": [
            {"name": s.publisher.name, "reason": s.reason} for s in publisher_plan.excluded[:8]
        ],
        "selected_personas": [s.persona.name for s in audience_plan.selected],
    }

    try:
        result = await generate_json(
            prompt_file="campaign_strategy.md",
            payload=payload,
            response_schema=schemas.CAMPAIGN_STRATEGY,
            temperature=0.5,
            label="campaign_strategy",
        )
    except Exception as exc:  # noqa: BLE001
        # The config is fully computable without this call. Losing the prose is
        # a degraded output; failing the whole run over it would be worse.
        logger.warning("Campaign strategy prose failed, using computed fallbacks: %s", exc)
        result = {}

    return {
        "bid_strategy_rationale": result.get("bid_strategy_rationale")
        or f"Break-even CPA is ${break_even:.2f}; bidding at {int(TARGET_CPA_HEADROOM * 100)}% of it leaves room for the CTR and CVR assumptions to be wrong.",
        "primary_kpi": result.get("primary_kpi") or "Cost per conversion",
        "brand_safety_notes": result.get("brand_safety_notes")
        or "No specific brand-safety concerns identified beyond the excluded publishers.",
        "allocation_rationales": {
            item["publisher_id"]: item["rationale"]
            for item in result.get("allocation_rationales", [])
            if item.get("publisher_id") and item.get("rationale")
        },
    }
