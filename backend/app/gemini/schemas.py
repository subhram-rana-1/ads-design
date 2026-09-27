"""Response schemas, written in Gemini's own schema dialect.

Two constraints shape everything here:

- `additionalProperties` is not supported, so there are no free-form maps. Every
  object has a fixed, declared set of keys.
- `propertyOrdering` is set explicitly on every object. Generation is
  left-to-right, so ordering is a quality lever, not documentation: a model that
  writes `score` before `reason` picks a number and then justifies it, while one
  that writes `reason` first reasons and then scores. The latter is better, and
  it is what these orderings enforce.

Catalog fields never appear in any output schema. The model returns ids and
judgements; the API joins the catalog objects back in. A model that is never
asked to write an AOV cannot get one wrong, and the output is roughly five times
smaller, which is directly visible as latency.
"""

from __future__ import annotations

from typing import Any

STRING = {"type": "STRING"}
NUMBER = {"type": "NUMBER"}
BOOLEAN = {"type": "BOOLEAN"}

PUBLISHER_ATTRIBUTES = [
    "relevance",
    "audience_overlap",
    "price_fit",
    "scale_fit",
    "context_fit",
]

# `publisher_reach` is absent on purpose: it is computed in core.reach from age
# bands and gender splits rather than judged. Leaving it out of the enum is what
# lets persona scoring run without waiting for the publisher buy.
PERSONA_ATTRIBUTES = [
    "category_affinity",
    "messaging_fit",
    "price_alignment",
    "disinterest_conflict",
]


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Object schema with propertyOrdering pinned to declaration order."""
    return {
        "type": "OBJECT",
        "properties": properties,
        "required": required,
        "propertyOrdering": list(properties.keys()),
    }


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "ARRAY", "items": items}


def _enum(values: list[str]) -> dict[str, Any]:
    return {"type": "STRING", "enum": values}


def _attribute_score(allowed: list[str]) -> dict[str, Any]:
    return _obj(
        {
            "attribute": _enum(allowed),
            # reason before score, deliberately: judge, then quantify.
            "reason": {
                "type": "STRING",
                "description": "One or two sentences citing specific catalog values.",
            },
            "score": {
                "type": "NUMBER",
                "description": "-1.0 to 1.0 inclusive. -1 actively opposed, 0 orthogonal, 1 fully aligned.",
            },
        },
        ["attribute", "reason", "score"],
    )


# --------------------------------------------------------------------------
# Session naming
# --------------------------------------------------------------------------

SESSION_NAME = _obj(
    {"campaign_name": {"type": "STRING", "description": "3-5 words, title case, no quotes."}},
    ["campaign_name"],
)


# --------------------------------------------------------------------------
# Brief collection
# --------------------------------------------------------------------------

BRIEF_COLLECTOR = _obj(
    {
        "reply": {
            "type": "STRING",
            "description": (
                "The message shown to the advertiser. When asking for information, use "
                "a short opening line, a blank line, then one '  • **Field name** — question' "
                "bullet per item. When not asking for anything, one or two plain sentences "
                "with no bullets and no bold."
            ),
        },
        "extracted": _obj(
            {
                "business_overview": {
                    "type": "STRING",
                    "nullable": True,
                    "description": "What they sell and to whom, in their own framing. Null if still unknown.",
                },
                "daily_budget_usd": {"type": "NUMBER", "nullable": True},
                "average_product_value_usd": {"type": "NUMBER", "nullable": True},
                "campaign_objective": {
                    "type": "STRING",
                    "enum": ["conversions", "traffic", "awareness"],
                    "nullable": True,
                },
            },
            [],
        ),
        "missing_fields": _array(STRING),
        "is_complete": BOOLEAN,
    },
    ["reply", "extracted", "missing_fields", "is_complete"],
)


# --------------------------------------------------------------------------
# Publishers: score, then collate
# --------------------------------------------------------------------------

# One publisher per call. Scoring fans out, so the response carries no id: the
# caller already knows which publisher it asked about, and a model that never
# writes an id cannot return the wrong one.
PUBLISHER_SCORING = _obj(
    {"attribute_scores": _array(_attribute_score(PUBLISHER_ATTRIBUTES))},
    ["attribute_scores"],
)

PUBLISHER_VERDICT = _obj(
    {
        "catalog_fit": _obj(
            {
                "explanation": {
                    "type": "STRING",
                    "description": "Two or three sentences. If the fit is poor, name the actual mismatch.",
                },
                "verdict": _enum(["strong", "partial", "poor"]),
            },
            ["explanation", "verdict"],
        ),
        "publishers": _array(
            _obj(
                {
                    "publisher_id": STRING,
                    "reason": {
                        "type": "STRING",
                        "description": "One collated sentence the advertiser reads. Do not restate every attribute.",
                    },
                    "verdict": _enum(["recommended", "excluded"]),
                    "override_reason": {
                        "type": "STRING",
                        "nullable": True,
                        "description": "Only when disagreeing with the provisional verdict. Null otherwise.",
                    },
                },
                ["publisher_id", "reason", "verdict"],
            )
        ),
    },
    ["catalog_fit", "publishers"],
)


# --------------------------------------------------------------------------
# Personas: score, then select
# --------------------------------------------------------------------------

# One persona per call, same reasoning as PUBLISHER_SCORING.
PERSONA_SCORING = _obj(
    {"attribute_scores": _array(_attribute_score(PERSONA_ATTRIBUTES))},
    ["attribute_scores"],
)

PERSONA_SELECTION = _obj(
    {
        "personas": _array(
            _obj(
                {
                    "persona_id": STRING,
                    "reason": {
                        "type": "STRING",
                        "description": "Why this persona is or is not worth an ad set for this business.",
                    },
                    "selected": BOOLEAN,
                    "override_reason": {"type": "STRING", "nullable": True},
                },
                ["persona_id", "reason", "selected"],
            )
        )
    },
    ["personas"],
)


# --------------------------------------------------------------------------
# Creatives
# --------------------------------------------------------------------------

CREATIVE_GENERATION = _obj(
    {
        "ad_sets": _array(
            _obj(
                {
                    "persona_id": STRING,
                    "creatives": _array(
                        _obj(
                            {
                                "angle": _enum(["benefit_led", "social_proof", "offer_led"]),
                                "headline": {"type": "STRING", "description": "Maximum 60 characters."},
                                "body": {"type": "STRING", "description": "Maximum 160 characters."},
                                "persona_fit_note": {
                                    "type": "STRING",
                                    "description": "Which messaging preference this uses and which disinterest it avoids.",
                                },
                            },
                            ["angle", "headline", "body", "persona_fit_note"],
                        )
                    ),
                },
                ["persona_id", "creatives"],
            )
        )
    },
    ["ad_sets"],
)


# --------------------------------------------------------------------------
# Campaign strategy (prose only — every number is computed in code)
# --------------------------------------------------------------------------

CAMPAIGN_STRATEGY = _obj(
    {
        "bid_strategy_rationale": {
            "type": "STRING",
            "description": "Two or three sentences referencing the actual target and break-even numbers supplied.",
        },
        "primary_kpi": {
            "type": "STRING",
            "description": "Short phrase, e.g. 'Cost per subscription signup'.",
        },
        "brand_safety_notes": {
            "type": "STRING",
            "description": "Contexts this brand should not appear in, given the excluded publishers.",
        },
        "allocation_rationales": _array(
            _obj(
                {
                    "publisher_id": STRING,
                    "rationale": {
                        "type": "STRING",
                        "description": "One sentence on why this publisher gets this share of the daily budget.",
                    },
                },
                ["publisher_id", "rationale"],
            )
        ),
    },
    ["bid_strategy_rationale", "primary_kpi", "brand_safety_notes", "allocation_rationales"],
)
