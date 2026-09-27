"""Session CRUD and the brief-collection chat turn.

Chat is plain request/response JSON, not SSE. A turn is one LLM call and
resolves in a couple of seconds; streaming it would add a transport and a set of
partial-state bugs to save very little. SSE is reserved for generation, where
the wait is real.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.models.domain import Brief, Session, SessionSummary
from app.models.recommendation import AdsCampaignRecommendation
from app.services import brief_agent
from app.store.memory import SessionNotFound, store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sessions"])


class CreateSessionRequest(BaseModel):
    initial_message: str | None = None


class ChatRequest(BaseModel):
    message: str


@router.post("/sessions", response_model=Session)
async def create_session(body: CreateSessionRequest) -> Session:
    """Create and name a session. The first turn is a separate, streamed call.

    `initial_message` is used only to name the session — it is not recorded as a
    message here. The client immediately POSTs the same text to `/chat`, which
    streams the reply. Running the turn inside creation would mean buffering the
    whole reply before the client saw anything, which is the thing streaming
    exists to avoid.
    """
    message = (body.initial_message or "").strip()
    name = await brief_agent.name_session(message) if message else "New Campaign"
    return store.create_session(name)


@router.get("/sessions", response_model=list[SessionSummary])
def list_sessions() -> list[SessionSummary]:
    return store.list_sessions()


@router.get("/sessions/{session_id}", response_model=Session)
def get_session(session_id: str) -> Session:
    return _get(session_id)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    try:
        store.delete_session(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": session_id}


@router.post("/sessions/{session_id}/chat")
async def chat(session_id: str, body: ChatRequest) -> StreamingResponse:
    """One turn, streamed as `delta` events then a final `done`.

    POST rather than GET, so this is not an `EventSource` — the client reads the
    body with a stream reader. Same SSE framing either way.
    """
    session = _get(session_id)

    if session.status != "collecting":
        # The composer locks client-side once the brief is complete. This is the
        # server-side half of the same rule.
        raise HTTPException(
            status_code=409,
            detail=f"This session is {session.status}; the brief is already closed.",
        )

    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    async def events():
        queue: asyncio.Queue = asyncio.Queue()

        async def on_delta(text: str) -> None:
            await queue.put(text)

        async def run() -> None:
            try:
                await brief_agent.run_turn_streamed(session, message, on_delta)
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield _sse("delta", {"text": chunk})

            await task  # re-raises whatever the turn failed with
            yield _sse(
                "done",
                {
                    "reply": session.chat_history[-1].content,
                    "brief": session.brief.model_dump(mode="json"),
                    "session_status": session.status,
                },
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the transcript
            logger.exception("Chat turn failed for session %s", session_id)
            yield _sse("error", {"message": str(exc)})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/sessions/{session_id}/recommendation", response_model=AdsCampaignRecommendation)
def get_recommendation(session_id: str) -> AdsCampaignRecommendation:
    session = _get(session_id)
    if not session.recommendation_id:
        raise HTTPException(status_code=404, detail=f"No recommendation yet (status: {session.status})")
    return store.get_recommendation(session.recommendation_id)


def _get(session_id: str) -> Session:
    try:
        return store.get_session(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="Session not found")
