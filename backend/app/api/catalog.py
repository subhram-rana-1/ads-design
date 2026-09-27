"""Read-only catalog endpoints. Used by the sample-brief picker and the drawer."""

from __future__ import annotations

from fastapi import APIRouter

from app.catalog import loader
from app.core import economics
from app.core.rate_card import RATE_CARD_NOTE
from app.models.domain import Persona, Publisher

router = APIRouter(tags=["catalog"])


@router.get("/sample-briefs")
def sample_briefs() -> list[dict]:
    return loader.sample_briefs()


@router.get("/publishers", response_model=list[Publisher])
def publishers() -> list[Publisher]:
    return loader.publishers()


@router.get("/personas", response_model=list[Persona])
def personas() -> list[Persona]:
    return loader.personas()


@router.get("/assumptions")
def assumptions() -> dict:
    """Every synthesised number in one place, so the UI can state them."""
    return {
        "rate_card_note": RATE_CARD_NOTE,
        "assumed_ctr": economics.ASSUMED_CTR,
        "assumed_cvr": economics.ASSUMED_CVR,
    }
