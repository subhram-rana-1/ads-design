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


def persona_catalog_for_prompt() -> list[dict]:
    return [p.model_dump() for p in personas()]


def warm() -> dict[str, int]:
    """Force-load at startup so failures happen at boot."""
    return {"publishers": len(publishers()), "personas": len(personas()), "sample_briefs": len(sample_briefs())}
