"""Catalog and session entities."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

Objective = Literal["conversions", "traffic", "awareness"]
SessionStatus = Literal["collecting", "generating", "ready", "failed"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


class GenderSplit(BaseModel):
    female: float
    male: float
    other: float = 0.0


class Audience(BaseModel):
    age_skew: str
    gender_split: GenderSplit
    top_geos: list[str]
    income_tier: str


class Publisher(BaseModel):
    id: str
    name: str
    category: str
    subcategories: list[str]
    monthly_impressions: int
    avg_order_value_usd: float
    audience: Audience
    notes: str

    # Synthesised at load time by core.rate_card. The catalog has no price
    # field, and a media plan without a cost basis cannot allocate a budget.
    cpm_usd: float = 0.0


class Persona(BaseModel):
    id: str
    name: str
    age_range: str
    gender_skew: str
    description: str
    category_affinities: list[str]
    price_sensitivity: str
    messaging_preferences: list[str]
    disinterested_in: list[str]
    typical_aov_usd: float


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    timestamp: datetime = Field(default_factory=utcnow)


class Brief(BaseModel):
    business_overview: str | None = None
    daily_budget_usd: float | None = None
    average_product_value_usd: float | None = None
    campaign_objective: Objective | None = None

    # Never asked for. It exists so the CPA projection has a break-even to
    # compare against, and it is labelled as an assumption in the UI.
    gross_margin_pct: float = 40.0

    is_complete: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    clarification_rounds: int = 0


class Session(BaseModel):
    id: str
    name: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    status: SessionStatus = "collecting"
    chat_history: list[ChatMessage] = Field(default_factory=list)
    brief: Brief = Field(default_factory=Brief)
    recommendation_id: str | None = None
    error: str | None = None

    def touch(self) -> None:
        self.updated_at = utcnow()


class SessionSummary(BaseModel):
    """What the sidebar needs. Deliberately not the whole session."""

    id: str
    name: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    has_recommendation: bool
