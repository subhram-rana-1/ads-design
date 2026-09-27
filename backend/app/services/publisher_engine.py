"""Publisher selection: score every publisher, then collate verdicts.

Two LLM calls with code in between:

    step 1 (LLM)  score all 20 publishers on 5 attributes, with reasons
          (code)  weighted composite -> provisional bucket at the threshold
    step 2 (LLM)  read the scores, write one collated reason per publisher,
                  confirm or override the bucket

The division is the point. Composites computed in code are reproducible across
runs and can be checked by hand; prose is where the model earns its place. The
override channel exists because a threshold is blunt — a publisher can score
well and still be the wrong place for a brand — but an override has to be
justified in writing, and that justification is shown in the UI.
"""

from __future__ import annotations

import asyncio
import logging

from app import config
from app.catalog import loader
from app.core.scoring import PUBLISHER_WEIGHTS, RECOMMEND_THRESHOLD, clamp_score, composite
from app.gemini import schemas
from app.gemini.client import generate_json
from app.models.domain import Brief
from app.models.recommendation import AttributeScore, CatalogFit, PublisherPlan, ScoredPublisher

logger = logging.getLogger(__name__)


async def score_all(brief: Brief) -> dict[str, list[AttributeScore]]:
    """Step 1 only. Split out so the orchestrator can start this concurrently
    with persona scoring, which no longer depends on it."""
    return await _score(brief, loader.publishers())


async def build_publisher_plan(
    brief: Brief, raw_scores: dict[str, list[AttributeScore]]
) -> tuple[PublisherPlan, CatalogFit]:
    publishers = loader.publishers()
    ranked = _apply_rubric(raw_scores, publishers)
    verdicts, catalog_fit = await _collate(brief, ranked)

    scored: list[ScoredPublisher] = []
    for entry in ranked:
        publisher = entry["publisher"]
        verdict_entry = verdicts.get(publisher.id, {})
        final_verdict = verdict_entry.get("verdict") or entry["provisional_verdict"]
        override = _clean(verdict_entry.get("override_reason"))

        # An override_reason without an actual disagreement is noise in the UI.
        if final_verdict == entry["provisional_verdict"]:
            override = None

        scored.append(
            ScoredPublisher(
                publisher_id=publisher.id,
                publisher=publisher,
                composite_score=entry["composite"],
                attribute_scores=entry["attribute_scores"],
                reason=verdict_entry.get("reason") or _fallback_reason(entry),
                verdict=final_verdict,
                provisional_verdict=entry["provisional_verdict"],
                override_reason=override,
            )
        )

    recommended = sorted(
        [s for s in scored if s.verdict == "recommended"],
        key=lambda s: s.composite_score,
        reverse=True,
    )
    excluded = sorted(
        [s for s in scored if s.verdict == "excluded"],
        key=lambda s: s.composite_score,
    )

    # A plan with nothing to buy is not a plan. If the model excluded everything,
    # promote the single best-scoring publisher and let catalog_fit say why the
    # whole thing is thin.
    if not recommended and excluded:
        best = max(excluded, key=lambda s: s.composite_score)
        best.verdict = "recommended"
        best.override_reason = (
            "Promoted as the least-bad option so the plan has somewhere to run. "
            "Treat this as a test buy, not a recommendation."
        )
        recommended = [best]
        excluded = [s for s in excluded if s.publisher_id != best.publisher_id]

    return PublisherPlan(recommended=recommended, excluded=excluded), catalog_fit


async def _score(brief: Brief, publishers) -> dict[str, list[AttributeScore]]:
    """Fan out one call per publisher, then fan in.

    Three reasons this beats scoring all 20 in a single call:

    1. **Latency.** The single call produced ~4,000 output tokens and took 90s+.
       Output generation is sequential and roughly linear in length. Twenty
       calls of ~200 tokens, run concurrently, cost about as long as one.
    2. **Attention.** Twenty publishers and a hundred scores in one response
       means the later entries get a tired, pattern-matched version of the
       rubric. Each call here gets the full rubric for one publisher.
    3. **Blast radius.** A malformed entry now costs one publisher instead of
       the entire phase, and a short response cannot hit the token ceiling and
       come back as truncated JSON.

    The cost is that `scale_fit` loses its comparative frame, which is why each
    call receives computed catalog percentiles (`publisher_scale_context`).
    """
    results = await asyncio.gather(
        *(_score_one(brief, publisher) for publisher in publishers),
        return_exceptions=True,
    )

    by_id: dict[str, list[AttributeScore]] = {}
    failures: list[str] = []

    for publisher, result in zip(publishers, results):
        if isinstance(result, BaseException):
            # Scored as fully neutral rather than dropped. A publisher that
            # silently vanishes from the catalog mid-run is a far more confusing
            # failure than one that shows up at the bottom with a zero.
            logger.warning("Scoring failed for %s: %s", publisher.id, result)
            failures.append(publisher.id)
            by_id[publisher.id] = []
        else:
            by_id[publisher.id] = result

    if failures:
        logger.warning("%d/%d publishers failed to score: %s", len(failures), len(publishers), failures)
    return by_id


async def _score_one(brief: Brief, publisher) -> list[AttributeScore]:
    result = await generate_json(
        prompt_file="publisher_scoring.md",
        payload={
            "brief": brief.model_dump(mode="json"),
            "publisher": publisher.model_dump(),
            "catalog_context": loader.publisher_scale_context(publisher),
        },
        response_schema=schemas.PUBLISHER_SCORING,
        temperature=0.3,
        label=f"publisher_scoring[{publisher.id}]",
        thinking_budget=config.GEMINI_SCORING_THINKING_BUDGET,
    )
    return [
        AttributeScore(
            attribute=s["attribute"],
            score=clamp_score(s.get("score", 0.0)),
            reason=s.get("reason") or "",
        )
        for s in result.get("attribute_scores", [])
        if s.get("attribute") in PUBLISHER_WEIGHTS
    ]


def _apply_rubric(raw_scores: dict[str, list[AttributeScore]], publishers) -> list[dict]:
    ranked = []
    for publisher in publishers:
        attribute_scores = raw_scores.get(publisher.id, [])
        score = composite(attribute_scores, PUBLISHER_WEIGHTS)
        ranked.append(
            {
                "publisher": publisher,
                "attribute_scores": attribute_scores,
                "composite": score,
                "provisional_verdict": "recommended" if score >= RECOMMEND_THRESHOLD else "excluded",
            }
        )
    ranked.sort(key=lambda e: e["composite"], reverse=True)
    return ranked


async def _collate(brief: Brief, ranked: list[dict]) -> tuple[dict[str, dict], CatalogFit]:
    payload = {
        "brief": brief.model_dump(mode="json"),
        "recommend_threshold": RECOMMEND_THRESHOLD,
        "scored_publishers": [
            {
                "publisher_id": e["publisher"].id,
                "publisher_name": e["publisher"].name,
                "category": e["publisher"].category,
                "composite_score": e["composite"],
                "provisional_verdict": e["provisional_verdict"],
                "attribute_scores": [
                    {"attribute": a.attribute, "score": a.score, "reason": a.reason}
                    for a in e["attribute_scores"]
                ],
            }
            for e in ranked
        ],
    }

    result = await generate_json(
        prompt_file="publisher_verdict.md",
        payload=payload,
        response_schema=schemas.PUBLISHER_VERDICT,
        temperature=0.4,
        label="publisher_verdict",
    )

    verdicts = {
        item["publisher_id"]: item
        for item in result.get("publishers", [])
        if item.get("publisher_id")
    }

    fit = result.get("catalog_fit") or {}
    catalog_fit = CatalogFit(
        verdict=fit.get("verdict") or "partial",
        explanation=fit.get("explanation") or "No catalog fit assessment was returned.",
    )
    return verdicts, catalog_fit


def _fallback_reason(entry: dict) -> str:
    if not entry["attribute_scores"]:
        return "Not scored by the model; treated as neutral and excluded."
    best = max(entry["attribute_scores"], key=lambda a: a.score)
    return best.reason or f"Composite score {entry['composite']:.2f}."


def _clean(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
