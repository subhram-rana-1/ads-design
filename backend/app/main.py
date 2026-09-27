"""FastAPI entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import config
from app.api import catalog as catalog_routes
from app.api import generate as generate_routes
from app.api import sessions as session_routes
from app.catalog import loader
from app.gemini.client import GeminiError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Both of these raise on failure. A misconfigured container should refuse to
    # start with a readable sentence rather than serve a broken app.
    config.assert_ready()
    counts = loader.warm()
    logger.info("Catalog loaded: %s", counts)
    logger.info("Using model %s", config.GEMINI_MODEL)
    yield


app = FastAPI(title="Ad Placement & Creative Generation", version="1.0.0", lifespan=lifespan)


@app.exception_handler(GeminiError)
async def gemini_error_handler(request: Request, exc: GeminiError) -> JSONResponse:
    """502, with the actual reason.

    A bare 500 during a chat turn tells the user nothing and tells you nothing.
    "API key not valid" on screen is the difference between a ten-second fix and
    ten minutes in the container logs.
    """
    logger.error("Gemini call failed on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=502, content={"detail": f"LLM call failed: {exc}"})


def _health() -> dict:
    return {
        "status": "ok",
        "model": config.GEMINI_MODEL,
        **loader.warm(),
    }


# Registered twice on purpose: :8000/health for direct curl during development,
# /api/health for the same check through the nginx proxy on :8080.
app.add_api_route("/health", _health, methods=["GET"])
app.add_api_route("/api/health", _health, methods=["GET"])

app.include_router(catalog_routes.router, prefix="/api")
app.include_router(session_routes.router, prefix="/api")
app.include_router(generate_routes.router, prefix="/api")
