"""The generated campaign. One `AdsCampaignRecommendation` per completed brief."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.domain import Brief, Objective, Persona, Publisher, utcnow

CreativeAngle = Literal["benefit_led", "social_proof", "offer_led"]


class AttributeScore(BaseModel):
    attribute: str
    score: float  # -1.0 .. 1.0 inclusive
    reason: str


class ScoredPublisher(BaseModel):
    publisher_id: str
    # Hydrated server-side from the catalog. The model is never asked to echo
    # catalog fields, so it cannot misspell an AOV or invent an impression count.
    publisher: Publisher
    composite_score: float
    attribute_scores: list[AttributeScore]
    reason: str
    verdict: Literal["recommended", "excluded"]
    provisional_verdict: Literal["recommended", "excluded"]
    override_reason: str | None = None


class PublisherPlan(BaseModel):
    recommended: list[ScoredPublisher]
    excluded: list[ScoredPublisher]


class ScoredPersona(BaseModel):
    persona_id: str
    persona: Persona
    composite_score: float
    attribute_scores: list[AttributeScore]
    reason: str
    selected: bool
    override_reason: str | None = None


class AudiencePlan(BaseModel):
    selected: list[ScoredPersona]
    not_selected: list[ScoredPersona]


class Creative(BaseModel):
    angle: CreativeAngle
    headline: str
    body: str
    persona_fit_note: str


class AdSet(BaseModel):
    persona_id: str
    persona_name: str
    creatives: list[Creative]


class CatalogFit(BaseModel):
    verdict: Literal["strong", "partial", "poor"]
    explanation: str


# --------------------------------------------------------------------------
# Campaign config
# --------------------------------------------------------------------------


class BidStrategy(BaseModel):
    type: Literal["target_cpa", "max_cpc", "fixed_cpm"]
    target_cpa_usd: float | None = None
    max_cpc_usd: float | None = None
    cpm_bid_usd: float | None = None
    rationale: str


class BudgetPlan(BaseModel):
    daily_usd: float
    suggested_total_usd: float
    flight_days: int
    pacing: Literal["even", "accelerated"]


class PublisherAllocation(BaseModel):
    publisher_id: str
    publisher_name: str
    daily_budget_usd: float
    share_pct: float
    cpm_usd: float
    est_impressions: int
    est_clicks: int
    est_conversions: float
    est_cpa_usd: float | None
    rationale: str


class Targeting(BaseModel):
    persona_ids: list[str]
    age_range: str
    gender_skew: str
    income_tiers: list[str]
    geos: list[str]
    category_context: list[str]


class Flight(BaseModel):
    start_date: str
    end_date: str


class FrequencyCap(BaseModel):
    impressions: int
    per: Literal["day", "week"]


class BrandSafety(BaseModel):
    excluded_publisher_ids: list[str]
    notes: str


class Measurement(BaseModel):
    primary_kpi: str
    break_even_cpa_usd: float
    attribution_window_days: int


class CampaignConfig(BaseModel):
    objective: Objective
    pricing_model: Literal["CPM", "CPC", "CPA"]
    bid_strategy: BidStrategy
    budget: BudgetPlan
    allocation: list[PublisherAllocation]
    targeting: Targeting
    flight: Flight
    frequency_cap: FrequencyCap
    brand_safety: BrandSafety
    measurement: Measurement


class Assumptions(BaseModel):
    """Everything in the output that is invented rather than given.

    Rendered as a footer in the UI. Presenting synthesised numbers as facts is
    the fastest way to lose an interviewer's trust; stating them is cheap.
    """

    gross_margin_pct: float
    assumed_ctr: float
    assumed_cvr: float
    rate_card_note: str


class GenerationMeta(BaseModel):
    model: str
    duration_ms: int
    llm_call_count: int
    assumptions: Assumptions


class AdsCampaignRecommendation(BaseModel):
    id: str
    session_id: str
    campaign_name: str
    created_at: datetime = Field(default_factory=utcnow)
    brief: Brief
    catalog_fit: CatalogFit
    publisher_plan: PublisherPlan
    audience_plan: AudiencePlan
    ad_sets: list[AdSet]
    campaign_config: CampaignConfig
    generation_meta: GenerationMeta
