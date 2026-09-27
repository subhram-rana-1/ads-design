"""The conversational half of the product.

One LLM call per user turn. It reads the transcript plus the brief as it stands,
replies, and returns whatever it managed to extract. Merging is done here rather
than by the model so a later turn can never blank a field that an earlier turn
established.
"""

from __future__ import annotations

import logging

from app import config
from app.gemini import schemas
from app.gemini.client import generate_json, stream_json
from app.models.domain import Brief, ChatMessage, Session

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ("business_overview", "daily_budget_usd", "average_product_value_usd")


async def name_session(first_message: str) -> str:
    """Name the session from the first message so the sidebar populates at once."""
    try:
        result = await generate_json(
            prompt_file="session_namer.md",
            payload={"advertiser_message": first_message},
            response_schema=schemas.SESSION_NAME,
            temperature=0.7,
            label="session_namer",
        )
        name = (result.get("campaign_name") or "").strip()
        return name or "Untitled Campaign"
    except Exception as exc:  # noqa: BLE001
        # A failed name is cosmetic. It must not block the actual conversation.
        logger.warning("Session naming failed, falling back: %s", exc)
        return (first_message[:40] + "...") if len(first_message) > 40 else first_message or "New Campaign"


async def run_turn(session: Session, user_message: str) -> str:
    """Process one advertiser message without streaming."""
    payload = _turn_payload(session, user_message)
    result = await generate_json(
        prompt_file="brief_collector.md",
        payload=payload,
        response_schema=schemas.BRIEF_COLLECTOR,
        temperature=0.5,
        label="brief_collector",
    )
    return _apply_turn(session, result)


async def run_turn_streamed(session: Session, user_message: str, on_delta) -> str:
    """Process one advertiser message, emitting the reply as it is written.

    `on_delta(text)` is awaited with each new slice. Everything after the stream
    ends — merging the brief, the clarification cap, the fallbacks — is shared
    with `run_turn`, so the two paths cannot drift apart.
    """
    payload = _turn_payload(session, user_message)
    result = await stream_json(
        prompt_file="brief_collector.md",
        payload=payload,
        response_schema=schemas.BRIEF_COLLECTOR,
        temperature=0.5,
        label="brief_collector",
        on_delta=on_delta,
    )
    return _apply_turn(session, result)


def _turn_payload(session: Session, user_message: str) -> dict:
    session.chat_history.append(ChatMessage(role="user", content=user_message))
    return {
        "conversation": [
            {"role": m.role, "content": m.content} for m in session.chat_history
        ],
        "brief_so_far": _brief_state(session.brief),
        "clarification_rounds_used": session.brief.clarification_rounds,
        "max_clarification_rounds": config.MAX_CLARIFICATION_ROUNDS,
    }


def _apply_turn(session: Session, result: dict) -> str:
    reply = (result.get("reply") or "").strip() or "Could you tell me a bit more about the business?"
    _merge(session.brief, result.get("extracted") or {})

    session.brief.missing_fields = _missing(session.brief)
    model_says_complete = bool(result.get("is_complete"))
    session.brief.is_complete = model_says_complete and not session.brief.missing_fields

    if not session.brief.is_complete:
        session.brief.clarification_rounds += 1
        # Hard stop on the loop. Five rounds of questions is an interrogation,
        # and the honest answer at that point is to generate with what exists
        # and let catalog_fit carry the caveat.
        if session.brief.clarification_rounds > config.MAX_CLARIFICATION_ROUNDS:
            session.brief.is_complete = True
            reply = (
                reply
                + "\n\nI'll work with what we have and flag anything I had to assume."
            )
            _apply_fallbacks(session.brief)

    session.chat_history.append(ChatMessage(role="assistant", content=reply))
    session.touch()
    return reply


def _brief_state(brief: Brief) -> dict:
    return {
        "business_overview": brief.business_overview,
        "daily_budget_usd": brief.daily_budget_usd,
        "average_product_value_usd": brief.average_product_value_usd,
        "campaign_objective": brief.campaign_objective,
    }


def _merge(brief: Brief, extracted: dict) -> None:
    """Fill blanks only. Nulls from the model are ignored, never written back.

    Without this, a turn where the advertiser answers one question would wipe
    the three fields they answered earlier, because the model returns null for
    anything it did not just see.
    """
    if not brief.business_overview and _text(extracted.get("business_overview")):
        brief.business_overview = _text(extracted["business_overview"])

    if brief.daily_budget_usd is None:
        value = _positive_number(extracted.get("daily_budget_usd"))
        if value is not None:
            brief.daily_budget_usd = value

    if brief.average_product_value_usd is None:
        value = _positive_number(extracted.get("average_product_value_usd"))
        if value is not None:
            brief.average_product_value_usd = value

    objective = extracted.get("campaign_objective")
    if objective in ("conversions", "traffic", "awareness"):
        brief.campaign_objective = objective


def _missing(brief: Brief) -> list[str]:
    state = _brief_state(brief)
    return [field for field in REQUIRED_FIELDS if not state.get(field)]


def _apply_fallbacks(brief: Brief) -> None:
    """Last resort after the clarification cap. Every value here is visible in
    the UI as an assumption, never presented as something the advertiser said."""
    if not brief.business_overview:
        brief.business_overview = "Not specified by the advertiser."
    if brief.daily_budget_usd is None:
        brief.daily_budget_usd = 250.0
    if brief.average_product_value_usd is None:
        brief.average_product_value_usd = 50.0
    brief.missing_fields = []


def ensure_objective(brief: Brief) -> None:
    """Objective is inferred rather than asked for. Conversions is the right
    default for commerce media: the inventory sits on checkout and post-purchase
    pages, where the shopper is already transacting."""
    if brief.campaign_objective is None:
        brief.campaign_objective = "conversions"


def _text(value) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _positive_number(value) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None
