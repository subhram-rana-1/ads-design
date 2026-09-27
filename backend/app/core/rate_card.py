"""Synthetic CPM rate card.

`publishers.json` carries reach (`monthly_impressions`) and shopper value
(`avg_order_value_usd`) but no price. Budget allocation, reach projection and
bid strategy all need a cost basis, so one is derived here.

Deriving it in code rather than asking the model for it is deliberate: the
numbers are then reproducible across runs, sortable, and explainable from three
lines of arithmetic instead of a paragraph of model prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module import-free
    from app.models.domain import Publisher

# Higher-income inventory prices higher, as it does in real retail media:
# the advertiser is buying access to more spending power per impression.
INCOME_TIER_BASE_CPM: dict[str, float] = {
    "mid": 8.0,
    "mid-high": 12.0,
    "high": 18.0,
}

# Purchase intent varies by category. Someone booking a fitness class is deeper
# into a considered spend than someone reordering paper towels at 11pm, and the
# impression is worth more. Instant delivery is discounted for exactly that
# reason: enormous volume, shallow intent.
CATEGORY_MULTIPLIER: dict[str, float] = {
    "wellness_services": 1.35,
    "wellness_dtc": 1.30,
    "beauty": 1.25,
    "home": 1.20,
    "apparel": 1.15,
    "pet": 1.10,
    "beverages": 1.10,
    "groceries": 1.05,
    "meal_kits": 1.00,
    "instant_delivery": 0.85,
}

DEFAULT_BASE_CPM = 10.0
DEFAULT_MULTIPLIER = 1.0
MAX_AOV_UPLIFT = 0.30
AOV_UPLIFT_DIVISOR = 500.0

RATE_CARD_NOTE = (
    "CPMs are synthesised from income tier x category intent x AOV, because the "
    "publisher catalog has no price field. Range is roughly $7-$28, which is a "
    "plausible retail-media spread, but these are invented numbers."
)


def cpm_for(publisher: "Publisher") -> float:
    """Return a CPM in USD for one publisher.

    AOV adds up to +30%: a publisher whose shoppers spend $198 an order is
    worth more per impression than one whose shoppers spend $28, even at the
    same income tier, because the downstream order is larger.
    """
    base = INCOME_TIER_BASE_CPM.get(publisher.audience.income_tier, DEFAULT_BASE_CPM)
    multiplier = CATEGORY_MULTIPLIER.get(publisher.category, DEFAULT_MULTIPLIER)
    aov_uplift = 1.0 + min(publisher.avg_order_value_usd / AOV_UPLIFT_DIVISOR, MAX_AOV_UPLIFT)
    return round(base * multiplier * aov_uplift, 2)
