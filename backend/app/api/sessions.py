"""Session CRUD and the brief-collection chat turn.

Chat is plain request/response JSON, not SSE. A turn is one LLM call and
resolves in a couple of seconds; streaming it would add a transport and a set of
partial-state bugs to save very little. SSE is reserved for generation, where
the wait is real.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.domain import Brief, Session, SessionSummary
from app.models.recommendation import AdsCampaignRecommendation
from app.services import brief_agent
from app.store.memory import SessionNotFound, store

router = APIRouter(tags=["sessions"])


class CreateSessionRequest(BaseModel):
    initial_message: str | None = None


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str
    brief: Brief
    session_status: str


@router.post("/sessions", response_model=Session)
async def create_session(body: CreateSessionRequest) -> Session:
    """Create a session, optionally running the first turn in the same round-trip.

    Clicking a sample brief calls exactly this: name the session and answer the
    opening message together, so the UI does not flash an empty transcript.
    """
    message = (body.initial_message or "").strip()
    name = await brief_agent.name_session(message) if message else "New Campaign"

    session = store.create_session(name)
    if message:
        try:
            await brief_agent.run_turn(session, message)
        except Exception:
            # Creation is atomic. A first turn that fails would otherwise leave
            # an empty session sitting in the sidebar that the user has to clean
            # up manually.
            store.delete_session(session.id)
            raise
    return session


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


@router.post("/sessions/{session_id}/chat", response_model=ChatResponse)
async def chat(session_id: str, body: ChatRequest) -> ChatResponse:
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

    reply = await brief_agent.run_turn(session, message)
    return ChatResponse(reply=reply, brief=session.brief, session_status=session.status)


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
