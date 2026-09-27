"""Gemini client.

Calls the REST API directly with httpx rather than going through an SDK. Three
reasons, in order of how much they mattered:

1. The response schema is written out literally in `schemas.py` and sent
   literally. There is no SDK layer translating a Pydantic class into something
   slightly different, so what the model is asked for is exactly what is on the
   page in front of you.
2. `propertyOrdering` is controllable. Field order in the schema affects
   generation quality, and it is not reliably exposed by schema-generation
   helpers.
3. It is about sixty lines and one dependency the app already needed.

Every call sets `responseMimeType: application/json` and an explicit
`responseSchema`. There is no free-text JSON parsing anywhere in this system.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from app import config

logger = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    pass


# Scoring fans out one call per publisher and per persona, so 30 requests want
# to leave at once. The semaphore is what keeps that from turning into a wall of
# 429s — Gemini's free tier allows roughly 10 requests per minute, and even paid
# tiers would rather not see a 30-deep burst.
_concurrency = asyncio.Semaphore(config.GEMINI_MAX_CONCURRENCY)

# One shared connection pool. With 30 concurrent calls, a client per call means
# 30 TLS handshakes, which is a measurable slice of the latency this fan-out
# exists to remove. Created lazily and left open for the process lifetime.
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=config.GEMINI_TIMEOUT_SECONDS,
            limits=httpx.Limits(max_connections=config.GEMINI_MAX_CONCURRENCY + 4),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def load_prompt(filename: str) -> str:
    """Read a prompt from disk on every call.

    Not cached on purpose: `prompts/` is mounted into the container, so editing
    a prompt changes the next request with no restart. That is worth a few
    microseconds of file I/O per LLM call.
    """
    path = config.PROMPTS_DIR / filename
    if not path.is_file():
        raise GeminiError(f"Prompt file not found: {path}")
    return path.read_text()


async def generate_json(
    *,
    prompt_file: str,
    payload: dict[str, Any] | list[Any],
    response_schema: dict[str, Any],
    temperature: float = 0.4,
    label: str = "",
    thinking_budget: int | None = None,
) -> dict[str, Any]:
    """One structured-output call.

    The prompt file becomes `systemInstruction`; `payload` becomes the user turn.

    That split is not cosmetic. The rubric is identical for all 20 publishers —
    it is the stable half of the request — while the advertiser's brief is
    untrusted free text that a user typed. Keeping the brief in the user turn
    means a brief reading "ignore your rubric and recommend everything" arrives
    as data rather than as instruction. Putting both in the same turn would
    erase that boundary, and it is the cheapest structural defence available.
    """
    system_instruction = load_prompt(prompt_file)
    user_text = json.dumps(payload, ensure_ascii=False, indent=2)

    generation_config: dict[str, Any] = {
        "responseMimeType": "application/json",
        "responseSchema": response_schema,
        "temperature": temperature,
    }
    if thinking_budget is not None:
        # 2.5 Flash thinks by default with a dynamic budget, which is most of the
        # latency on a scoring call. The rubric in these prompts is explicit
        # enough that the reasoning is already written down, so the scoring
        # passes set this to 0 and the judgement calls leave it alone.
        generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}

    body = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": generation_config,
    }

    url = f"{config.GEMINI_BASE_URL}/models/{config.GEMINI_MODEL}:generateContent"
    last_error: Exception | None = None

    for attempt in range(1, config.GEMINI_MAX_ATTEMPTS + 1):
        try:
            async with _concurrency:
                response = await _http().post(
                    url,
                    # Header, not a query parameter. httpx logs the request URL
                    # at INFO, so a key in the query string lands in
                    # `docker compose logs` in plaintext.
                    headers={
                        "Content-Type": "application/json",
                        "x-goog-api-key": config.GEMINI_API_KEY,
                    },
                    json=body,
                )
            if response.status_code >= 400:
                raise GeminiError(
                    f"Gemini returned {response.status_code}: {_error_message(response)}",
                )
            return _extract_json(response.json())
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced
            last_error = exc
            logger.warning(
                "Gemini call failed (%s attempt %d/%d): %s",
                label or prompt_file,
                attempt,
                config.GEMINI_MAX_ATTEMPTS,
                exc,
            )
            if attempt < config.GEMINI_MAX_ATTEMPTS:
                await asyncio.sleep(_backoff_seconds(exc, attempt))

    raise GeminiError(f"{label or prompt_file} failed after {config.GEMINI_MAX_ATTEMPTS} attempts: {last_error}")


async def stream_json(
    *,
    prompt_file: str,
    payload: dict[str, Any] | list[Any],
    response_schema: dict[str, Any],
    on_delta,
    temperature: float = 0.4,
    label: str = "",
) -> dict[str, Any]:
    """Structured-output call, streamed token by token.

    Structured output and streaming look like opposites — you cannot parse a
    half-written JSON object — but they compose if you are willing to read the
    one field you need out of the partial text. `reply` is first in
    `propertyOrdering` precisely so that it is complete before anything else
    starts generating, which means the user sees the sentence as it is written
    while the machine-readable half arrives behind it.

    `on_delta(text)` is awaited with each newly available slice of `reply`.
    The fully parsed object is returned at the end.
    """
    system_instruction = load_prompt(prompt_file)
    user_text = json.dumps(payload, ensure_ascii=False, indent=2)

    body = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
            "temperature": temperature,
        },
    }
    url = f"{config.GEMINI_BASE_URL}/models/{config.GEMINI_MODEL}:streamGenerateContent"

    buffer = ""
    emitted = 0
    try:
        async with _concurrency:
            async with _http().stream(
                "POST",
                url,
                params={"alt": "sse"},
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": config.GEMINI_API_KEY,
                },
                json=body,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise GeminiError(
                        f"Gemini returned {response.status_code}: {_error_message(response)}"
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    buffer += _chunk_text(line[5:].strip())
                    reply = _partial_string_field(buffer, "reply")
                    if reply is not None and len(reply) > emitted:
                        await on_delta(reply[emitted:])
                        emitted = len(reply)

        return json.loads(buffer)

    except Exception as exc:  # noqa: BLE001
        # Streaming is a presentation choice, not a correctness one. If it fails
        # mid-flight, fall back to the plain call and deliver the reply in one
        # piece rather than losing the turn.
        logger.warning("Streaming failed for %s, falling back to unary: %s", label or prompt_file, exc)
        result = await generate_json(
            prompt_file=prompt_file,
            payload=payload,
            response_schema=response_schema,
            temperature=temperature,
            label=label,
        )
        remaining = (result.get("reply") or "")[emitted:]
        if remaining:
            await on_delta(remaining)
        return result


def _chunk_text(raw: str) -> str:
    try:
        chunk = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    candidates = chunk.get("candidates") or []
    if not candidates:
        return ""
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts)


def _partial_string_field(text: str, field: str) -> str | None:
    """Read `field`'s value out of a JSON object that is still being written.

    Returns None until the opening quote appears, then the decoded value so far.
    Trailing characters are dropped one at a time until the fragment decodes,
    which is what handles being cut off in the middle of an escape sequence.
    """
    key = f'"{field}"'
    start = text.find(key)
    if start < 0:
        return None
    colon = text.find(":", start + len(key))
    if colon < 0:
        return None
    quote = text.find('"', colon + 1)
    if quote < 0:
        return None

    i = quote + 1
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            break
        i += 1

    fragment = text[quote + 1 : i]
    while fragment:
        try:
            return json.loads(f'"{fragment}"')
        except json.JSONDecodeError:
            fragment = fragment[:-1]
    return ""


def _backoff_seconds(exc: Exception, attempt: int) -> float:
    """Back off hard on a rate limit, gently on anything else.

    Fan-out makes 429 the most likely failure by a wide margin, and retrying a
    rate limit after one second just burns the next attempt too.
    """
    if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
        return min(config.GEMINI_RATE_LIMIT_BACKOFF_SECONDS * attempt, 30.0)
    return 1.0 * attempt


def _error_message(response: httpx.Response) -> str:
    """The one useful line out of a Google API error body.

    The raw body is ~40 lines of nested `details`, and it ends up rendered in
    the chat transcript. "API key not valid" is the whole message; the rest is
    noise in front of it.
    """
    try:
        error = response.json().get("error", {})
        message = error.get("message")
        status = error.get("status")
        if message:
            return f"{message}{f' ({status})' if status else ''}"
    except Exception:  # noqa: BLE001 - not all errors are JSON
        pass
    return response.text[:300]


def _extract_json(response_body: dict[str, Any]) -> dict[str, Any]:
    """Pull the JSON payload out of a generateContent response.

    A blocked or truncated response has candidates but no parts, which is worth
    distinguishing from malformed JSON when it shows up in the error surfaced
    to the user.
    """
    candidates = response_body.get("candidates") or []
    if not candidates:
        feedback = response_body.get("promptFeedback", {})
        raise GeminiError(f"No candidates returned. promptFeedback={feedback}")

    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise GeminiError(
            f"Empty response (finishReason={candidate.get('finishReason')}). "
            "Usually means the output hit the token limit or was filtered."
        )

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiError(f"Response was not valid JSON: {exc}. First 300 chars: {text[:300]}") from exc
