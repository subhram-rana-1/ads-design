"""Loads the publisher and persona catalogs once, at startup.

Validating through Pydantic at boot means malformed catalog data is a container
that refuses to start, not a 500 on the fourth click of a demo.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from app import config
from app.core.rate_card import cpm_for
from app.models.domain import Persona, Publisher

_SAMPLE_LINE = re.compile(r"^(\d+)\.\s+(.*)$")


@lru_cache(maxsize=1)
def publishers() -> list[Publisher]:
    raw = json.loads(config.PUBLISHERS_PATH.read_text())
    loaded = [Publisher.model_validate(item) for item in raw]
    for publisher in loaded:
        publisher.cpm_usd = cpm_for(publisher)
    return loaded


@lru_cache(maxsize=1)
def personas() -> list[Persona]:
    raw = json.loads(config.PERSONAS_PATH.read_text())
    return [Persona.model_validate(item) for item in raw]


@lru_cache(maxsize=1)
def publishers_by_id() -> dict[str, Publisher]:
    return {p.id: p for p in publishers()}


@lru_cache(maxsize=1)
def personas_by_id() -> dict[str, Persona]:
    return {p.id: p for p in personas()}


@lru_cache(maxsize=1)
def sample_briefs() -> list[dict]:
    """The 15 example advertiser one-liners, numbered as in the source file.

    The awkward ones (#5, #7, #15) are included and unlabelled. An interviewer
    who picks one should see the system handle it, not a curated happy path.
    """
    text = config.SAMPLE_BRIEFS_PATH.read_text()
    out: list[dict] = []
    for line in text.splitlines():
        match = _SAMPLE_LINE.match(line.strip())
        if match:
            out.append({"number": int(match.group(1)), "text": match.group(2).strip()})
    return out


def publisher_catalog_for_prompt() -> list[dict]:
    """Catalog as the scoring prompt sees it.

    `cpm_usd` is included so price_fit and scale_fit have a cost basis to reason
    about. Everything else is verbatim from the source file.
    """
    return [p.model_dump() for p in publishers()]


@lru_cache(maxsize=1)
def _distributions() -> dict[str, list[float]]:
    return {
        "monthly_impressions": sorted(float(p.monthly_impressions) for p in publishers()),
        "avg_order_value_usd": sorted(float(p.avg_order_value_usd) for p in publishers()),
        "cpm_usd": sorted(float(p.cpm_usd) for p in publishers()),
    }


def _spread(values: list[float]) -> dict:
    return {
        "min": values[0],
        "median": values[len(values) // 2],
        "max": values[-1],
    }


def _percentile_rank(value: float, values: list[float]) -> int:
    """Share of the catalog at or below `value`, 0-100."""
    at_or_below = sum(1 for v in values if v <= value)
    return round(at_or_below / len(values) * 100)


def publisher_scale_context(publisher: Publisher) -> dict:
    """Where one publisher sits in the catalog, computed rather than eyeballed.

    `scale_fit` is the one publisher attribute that is inherently comparative —
    "can this inventory absorb the budget" is meaningless without knowing the
    catalog spans 30x. Since scoring now runs one publisher per call, the model
    cannot see the others, so the reference frame is supplied here instead.

    Handing over a computed percentile is better than the full catalog anyway:
    the model is not asked to rank 20 numbers correctly, it is told the answer
    and asked to judge against it.
    """
    dist = _distributions()
    return {
        "catalog_size": len(publishers()),
        "monthly_impressions": _spread(dist["monthly_impressions"]),
        "avg_order_value_usd": _spread(dist["avg_order_value_usd"]),
        "cpm_usd": _spread(dist["cpm_usd"]),
        "this_publisher_percentiles": {
            "monthly_impressions": _percentile_rank(
                publisher.monthly_impressions, dist["monthly_impressions"]
            ),
            "avg_order_value_usd": _percentile_rank(
                publisher.avg_order_value_usd, dist["avg_order_value_usd"]
            ),
            "cpm_usd": _percentile_rank(publisher.cpm_usd, dist["cpm_usd"]),
        },
    }


def persona_catalog_for_prompt() -> list[dict]:
    return [p.model_dump() for p in personas()]


def warm() -> dict[str, int]:
    """Force-load at startup so failures happen at boot."""
    return {"publishers": len(publishers()), "personas": len(personas()), "sample_briefs": len(sample_briefs())}
