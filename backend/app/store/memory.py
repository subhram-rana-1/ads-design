"""In-memory store. A dict, a lock, and no illusions about durability.

Restarting the backend wipes every session. That is stated in the UI rather
than hidden, because a demo that pretends to persist is worse than one that
says it does not.

Single process only. `--workers 1` is set in docker-compose.yml for this exact
reason: a second worker would hold a second copy of these dicts and a session
would resolve about half the time.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

from app.models.domain import Session, SessionSummary
from app.models.recommendation import AdsCampaignRecommendation


class SessionNotFound(KeyError):
    pass


class MemoryStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._recommendations: dict[str, AdsCampaignRecommendation] = {}
        # Append-only per session. The SSE endpoint replays from index 0, which
        # is what makes reattaching after a refresh free.
        self._progress: dict[str, list[dict[str, Any]]] = {}
        self._generation_started: set[str] = set()
        self._lock = threading.Lock()

    # -- sessions ---------------------------------------------------------

    def create_session(self, name: str) -> Session:
        session = Session(id=str(uuid.uuid4()), name=name)
        with self._lock:
            self._sessions[session.id] = session
            self._progress[session.id] = []
        return session

    def get_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def list_sessions(self) -> list[SessionSummary]:
        sessions = sorted(self._sessions.values(), key=lambda s: s.updated_at, reverse=True)
        return [
            SessionSummary(
                id=s.id,
                name=s.name,
                status=s.status,
                created_at=s.created_at,
                updated_at=s.updated_at,
                has_recommendation=s.recommendation_id is not None,
            )
            for s in sessions
        ]

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                raise SessionNotFound(session_id)
            self._progress.pop(session_id, None)
            self._generation_started.discard(session_id)
            if session.recommendation_id:
                self._recommendations.pop(session.recommendation_id, None)

    # -- recommendations --------------------------------------------------

    def save_recommendation(self, recommendation: AdsCampaignRecommendation) -> None:
        with self._lock:
            self._recommendations[recommendation.id] = recommendation

    def get_recommendation(self, recommendation_id: str) -> AdsCampaignRecommendation:
        rec = self._recommendations.get(recommendation_id)
        if rec is None:
            raise SessionNotFound(recommendation_id)
        return rec

    # -- generation progress ----------------------------------------------

    def claim_generation(self, session_id: str) -> bool:
        """Return True if this caller owns the generation run.

        Two browser tabs hitting the SSE endpoint must not start two pipelines.
        """
        with self._lock:
            if session_id in self._generation_started:
                return False
            self._generation_started.add(session_id)
            return True

    def append_progress(self, session_id: str, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._progress.setdefault(session_id, []).append({"event": event, "data": data})

    def progress(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._progress.get(session_id, []))

    def reset_generation(self, session_id: str) -> None:
        """Used by retry after a failure."""
        with self._lock:
            self._progress[session_id] = []
            self._generation_started.discard(session_id)

    def stats(self) -> dict[str, int]:
        return {"sessions": len(self._sessions), "recommendations": len(self._recommendations)}


store = MemoryStore()
