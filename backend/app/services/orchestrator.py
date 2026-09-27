"""Runs the generation pipeline and records progress.

The pipeline runs as a background task that writes into the store. The SSE
endpoint observes that record; it does not own the work. A browser that
disconnects mid-run therefore does not cancel the generation, and a refresh
replays the progress log from the beginning and catches up.
"""

from __future__ import annotations

import logging
import time
import uuid

from app import config
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

PHASES = {
    "publishers": "Scoring all 20 publishers against your brief…",
    "personas": "Publishers ready — matching shopper personas…",
    "creatives": "Audiences locked — writing ad copy for each persona…",
    "config": "Building the campaign config and budget split…",
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

        _start(session_id, "publishers")
        publisher_plan, catalog_fit = await publisher_engine.build_publisher_plan(session.brief)
        llm_calls += 2
        _complete(
            session_id,
            "publishers",
            f"{len(publisher_plan.recommended)} recommended, {len(publisher_plan.excluded)} excluded "
            f"(catalog fit: {catalog_fit.verdict})",
        )

        _start(session_id, "personas")
        audience_plan = await persona_engine.build_audience_plan(session.brief, publisher_plan)
        llm_calls += 2
        _complete(
            session_id,
            "personas",
            f"{len(audience_plan.selected)} personas selected: "
            + ", ".join(s.persona.name for s in audience_plan.selected),
        )

        _start(session_id, "creatives")
        ad_sets = await creative_engine.build_ad_sets(session.brief, audience_plan, publisher_plan)
        llm_calls += 1
        creative_count = sum(len(a.creatives) for a in ad_sets)
        _complete(session_id, "creatives", f"{creative_count} creatives across {len(ad_sets)} ad sets")

        _start(session_id, "config")
        campaign_config = await campaign_builder.build_campaign_config(
            session.brief, publisher_plan, audience_plan
        )
        llm_calls += 1
        _complete(
            session_id,
            "config",
            f"${campaign_config.budget.daily_usd:,.0f}/day across "
            f"{len(campaign_config.allocation)} publishers",
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
