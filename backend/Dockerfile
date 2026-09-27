# Build context is the repo root, not ./backend, so the image can bake in the
# prompts and catalog it needs. Locally the compose volume mounts shadow these
# copies with your live files; anywhere else (Render, Fly, a plain `docker run`)
# the image is self-contained and needs no mounts at all.
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
