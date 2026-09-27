# Builds the whole app into one image: API, prompts, catalog and UI.
#
# It lives at the repo root, and the build context is the repo root, because it
# copies from backend/, prompts/, problem_statement/ and frontend/. Keeping it
# here also means every platform that defaults to ./Dockerfile — Render, Fly,
# Railway, plain `docker build .` — works with no extra configuration.
#
# Locally, compose volume mounts shadow the baked-in prompts and UI with your
# live files. Anywhere else the image is self-contained and needs no mounts.
FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
COPY prompts/ /prompts/
COPY problem_statement/data/ /data/

# The UI, for single-service hosting. Ignored locally, where nginx serves it.
COPY frontend/public/ /static/

EXPOSE 8000

# Hosts inject their own port; compose overrides this command with --reload.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
