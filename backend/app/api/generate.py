"""SSE endpoint for the generation phase.

The stream is a view over the store's append-only progress log, polled every
250ms. Polling an in-memory list is free, and it buys two things a queue would
not: reconnecting is just replaying from index 0, and a second browser tab
watching the same session sees the same events rather than stealing them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.services.orchestrator import run_generation
from app.store.memory import SessionNotFound, store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["generate"])

POLL_INTERVAL_SECONDS = 0.25
HEARTBEAT_SECONDS = 5.0
MAX_STREAM_SECONDS = 600.0


@router.get("/sessions/{session_id}/generate")
async def generate(session_id: str) -> StreamingResponse:
    try:
        session = store.get_session(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status == "collecting":
        if not session.brief.is_complete:
            raise HTTPException(status_code=409, detail="Brief is not complete yet")
        if store.claim_generation(session_id):
            # Fire and forget: the task owns the work, the stream only watches.
            asyncio.create_task(run_generation(session_id))

    return StreamingResponse(
        _event_stream(session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Belt and braces with nginx's proxy_buffering off.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions/{session_id}/retry")
def retry(session_id: str) -> dict:
    """Reset a failed session so generation can be started again."""
    try:
        session = store.get_session(session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.status != "failed":
        raise HTTPException(status_code=409, detail=f"Session is {session.status}, not failed")

    session.status = "collecting"
    session.error = None
    session.touch()
    store.reset_generation(session_id)
    return {"status": "reset"}


async def _event_stream(session_id: str) -> AsyncIterator[str]:
    sent = 0
    started = time.monotonic()
    last_heartbeat = started

    while True:
        events = store.progress(session_id)
        while sent < len(events):
            event = events[sent]
            sent += 1
            yield _sse(event["event"], event["data"])
            last_heartbeat = time.monotonic()
            if event["event"] in ("ready", "error"):
                return

        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            # Publisher scoring is a single ~15s call. Without these the
            # connection looks dead to every proxy between here and the browser,
            # and to the user.
            yield _sse("heartbeat", {"elapsed_ms": int((now - started) * 1000)})
            last_heartbeat = now

        if now - started > MAX_STREAM_SECONDS:
            yield _sse("error", {"message": "Generation timed out after 10 minutes."})
            return

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
