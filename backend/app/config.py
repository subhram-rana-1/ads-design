"""Configuration. Resolves paths so the app runs identically in Docker and bare."""

from __future__ import annotations

import os
from pathlib import Path

# backend/app/config.py -> backend/app -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_dir(env_var: str, container_path: str, repo_relative: str) -> Path:
    """Env var wins, then the Docker mount point, then the repo layout.

    This is what lets `uvicorn app.main:app` work from ./backend without Docker,
    which is how you debug when the container is the thing that's broken.
    """
    override = os.getenv(env_var)
    if override:
        return Path(override)
    if Path(container_path).is_dir():
        return Path(container_path)
    return _REPO_ROOT / repo_relative


PROMPTS_DIR = _resolve_dir("PROMPTS_DIR", "/prompts", "prompts")
DATA_DIR = _resolve_dir("DATA_DIR", "/data", "problem_statement/data")

PUBLISHERS_PATH = DATA_DIR / "publishers.json"
PERSONAS_PATH = DATA_DIR / "shopper_personas.json"
SAMPLE_BRIEFS_PATH = DATA_DIR / "example_advertisers.txt"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TIMEOUT_SECONDS = 180.0

# One retry on a failed or unparseable call, then the session fails loudly.
GEMINI_MAX_ATTEMPTS = 2

# Assumptions surfaced in the UI. Not secrets, not tuning knobs — they are
# stated numbers that the campaign math depends on.
DEFAULT_GROSS_MARGIN_PCT = 40.0
MAX_CLARIFICATION_ROUNDS = 4


def assert_ready() -> None:
    """Fail at boot rather than on the first chat message.

    A missing API key that surfaces as a 500 three clicks into a demo is worse
    than a container that refuses to start with a sentence explaining why.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env, put your key "
            "in it, and run `docker compose up` again."
        )
    for path in (PUBLISHERS_PATH, PERSONAS_PATH, SAMPLE_BRIEFS_PATH):
        if not path.is_file():
            raise RuntimeError(f"Catalog file missing: {path}")
    if not PROMPTS_DIR.is_dir():
        raise RuntimeError(f"Prompts directory missing: {PROMPTS_DIR}")
