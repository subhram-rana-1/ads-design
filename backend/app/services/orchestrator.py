"""Runs the generation pipeline and records progress.

The pipeline runs as a background task that writes into the store. The SSE
endpoint observes that record; it does not own the work. A browser that
disconnects mid-run therefore does not cancel the generation, and a refresh
replays the progress log from the beginning and catches up.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from app import config
from app.catalog import loader
from app.core import economics
from app.core.rate_card import RATE_CARD_NOTE
from app.models.recommendation import (
    AdsCampaignRecommendation,
    Assumptions,
    GenerationMeta,
)
from app.services import brief_agent, campaign_builder, creative_engine, persona_engine, publisher_engine
from app.store.memory import store

logger = logging.getLogger(__name__)

# Deliberately opaque. These say what is being decided on the advertiser's
# behalf, not how the system is built. Call counts, catalog sizes and
# concurrency are implementation detail; an advertiser reading "scoring all 20
# publishers in parallel" learns nothing they can act on.
PHASES = {
    "assess": "Reading your brief and sizing up where your buyer actually shops…",
    "shortlist": "Weighing each placement against your product, price and budget…",
    "audience": "Deciding which shopper groups are worth their own message…",
    "creatives": "Writing ad copy tuned to each group…",
    "config": "Allocating budget, setting bids and checking the economics…",
}


async def run_generation(session_id: str) -> None:
    session = store.get_session(session_id)
    session.status = "generating"
    session.error = None
    session.touch()

    started = time.monotonic()
    llm_calls = 0

    try:
        brief_agent.ensure_objective(session.brief)

        # ── Stage 1 ────────────────────────────────────────────────────
        # Placement scoring and audience scoring are independent: every persona
        # attribute the model judges compares the persona against the brief
        # alone. Reach, the one attribute that needed the buy, is computed in
        # core.reach afterwards. So both fan-outs leave together — 30 calls in
        # one burst instead of 20 then 10.
        _start(session_id, "assess")
        publisher_scores, persona_scores = await asyncio.gather(
            publisher_engine.score_all(session.brief),
            persona_engine.score_all(session.brief),
        )
        llm_calls += len(loader.publishers()) + len(loader.personas())
        _complete(session_id, "assess", "Placements and shopper groups assessed")

        # ── Stage 2 ────────────────────────────────────────────────────
        _start(session_id, "shortlist")
        publisher_plan, catalog_fit = await publisher_engine.build_publisher_plan(
            session.brief, publisher_scores
        )
        llm_calls += 1
        top = publisher_plan.recommended[0].publisher.name if publisher_plan.recommended else None
        _complete(
            session_id,
            "shortlist",
            f"Strongest match: {top}" if top else "Shortlist ready",
        )

        # ── Stage 3 ────────────────────────────────────────────────────
        # Selection still waits for the buy: whether a group deserves its own
        # ad set depends on whether these placements can reach them.
        _start(session_id, "audience")
        audience_plan = await persona_engine.build_audience_plan(
            session.brief, publisher_plan, persona_scores
        )
        llm_calls += 1
        _complete(
            session_id,
            "audience",
            "Speaking to: " + ", ".join(s.persona.name for s in audience_plan.selected),
        )

        # ── Stage 4 ────────────────────────────────────────────────────
        # Copy and config both depend on the audience plan but not on each
        # other, so they overlap.
        _start(session_id, "creatives")
        _start(session_id, "config")
        ad_sets, campaign_config = await asyncio.gather(
            creative_engine.build_ad_sets(session.brief, audience_plan, publisher_plan),
            campaign_builder.build_campaign_config(session.brief, publisher_plan, audience_plan),
        )
        llm_calls += 2
        _complete(session_id, "creatives", "Ad copy written for every group")
        _complete(
            session_id,
            "config",
            f"${campaign_config.budget.daily_usd:,.0f}/day allocated, "
            f"bidding {campaign_config.bid_strategy.type.replace('_', ' ')}",
        )

        recommendation = AdsCampaignRecommendation(
            id=str(uuid.uuid4()),
            session_id=session_id,
            campaign_name=session.name,
            brief=session.brief,
            catalog_fit=catalog_fit,
            publisher_plan=publisher_plan,
            audience_plan=audience_plan,
            ad_sets=ad_sets,
            campaign_config=campaign_config,
            generation_meta=GenerationMeta(
                model=config.GEMINI_MODEL,
                duration_ms=int((time.monotonic() - started) * 1000),
                llm_call_count=llm_calls,
                assumptions=Assumptions(
                    gross_margin_pct=session.brief.gross_margin_pct,
                    assumed_ctr=economics.ASSUMED_CTR,
                    assumed_cvr=economics.ASSUMED_CVR,
                    rate_card_note=RATE_CARD_NOTE,
                ),
            ),
        )

        store.save_recommendation(recommendation)
        session.recommendation_id = recommendation.id
        session.status = "ready"
        session.touch()

        store.append_progress(
            session_id,
            "ready",
            {
                "recommendation_id": recommendation.id,
                "duration_ms": recommendation.generation_meta.duration_ms,
            },
        )

    except Exception as exc:  # noqa: BLE001 - surfaced to the user, never swallowed
        logger.exception("Generation failed for session %s", session_id)
        session.status = "failed"
        session.error = str(exc)
        session.touch()
        # No partial results. A half-built campaign presented as a campaign is
        # worse than an error message.
        store.append_progress(session_id, "error", {"message": str(exc)})


def _start(session_id: str, phase: str) -> None:
    store.append_progress(session_id, "phase_start", {"phase": phase, "label": PHASES[phase]})


def _complete(session_id: str, phase: str, summary: str) -> None:
    store.append_progress(session_id, "phase_complete", {"phase": phase, "summary": summary})
