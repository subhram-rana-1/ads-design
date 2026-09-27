"""Unit economics projection.

Turns a daily spend and a CPM into impressions, clicks, conversions and an
implied CPA, then compares that CPA against break-even. Comparing the two is
the campaign's honesty check: it says out loud whether the plan can be
profitable, rather than presenting a budget split and leaving the advertiser to
work it out.
"""

from __future__ import annotations

from typing import TypedDict

# Industry-typical placeholders, labelled as assumptions in the UI. A real
# system reads these from historical delivery data per publisher and per
# creative; there is no such data here and inventing per-publisher rates would
# be a more confident lie than inventing one.
ASSUMED_CTR = 0.0035  # 0.35% of impressions click

# 12%, not the ~2% you would use for generic display. This is the number people
# get wrong about commerce media: the ad appears on a checkout or post-purchase
# page, so the shopper clicking it is already mid-transaction with a payment
# method on file. Post-click conversion on retail-media placements runs an order
# of magnitude above open-web display, and using the open-web figure makes every
# line in every plan project a hopeless CPA — which looks like rigour and is
# actually just the wrong constant.
ASSUMED_CVR = 0.12


class Projection(TypedDict):
    est_impressions: int
    est_clicks: int
    est_conversions: float
    est_cpa_usd: float | None
    break_even_cpa_usd: float


def project(
    daily_spend_usd: float,
    cpm_usd: float,
    product_value_usd: float,
    gross_margin_pct: float,
) -> Projection:
    """Project one publisher line item for one day."""
    impressions = (daily_spend_usd / cpm_usd) * 1000 if cpm_usd > 0 else 0.0
    clicks = impressions * ASSUMED_CTR
    conversions = clicks * ASSUMED_CVR

    return {
        "est_impressions": round(impressions),
        "est_clicks": round(clicks),
        "est_conversions": round(conversions, 2),
        "est_cpa_usd": round(daily_spend_usd / conversions, 2) if conversions > 0 else None,
        "break_even_cpa_usd": break_even_cpa(product_value_usd, gross_margin_pct),
    }


def break_even_cpa(product_value_usd: float, gross_margin_pct: float) -> float:
    """The most an advertiser can pay per conversion and still not lose money.

    Gross profit per order, not revenue per order. Paying $80 to acquire an $80
    order at 40% margin loses $48.
    """
    return round(product_value_usd * gross_margin_pct / 100.0, 2)
