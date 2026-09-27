# Running and deploying

## The short version

GitHub **cannot host this app running**. GitHub Pages serves static files only,
and this needs a Python process to hold the session state and to call Gemini
with a key that must never reach the browser. An Actions secret is readable by
CI jobs, never by a live web service.

So there are three ways to see it work, in the order I'd recommend them.

---

## 1. Locally — the primary path

```bash
cp .env.example .env     # then put your key in GEMINI_API_KEY
docker compose up --build
```

Open <http://localhost:8080>.

This is what the exercise asks for and it is the best demo: no cold start, no
free-tier rate limits, and editing a prompt in `prompts/` or a file in
`frontend/public/` takes effect on the next request without a rebuild.

Ports are overridable in `.env` if 8000 or 8080 are taken:

```
BACKEND_PORT=8001
FRONTEND_PORT=8081
```

---

## 2. From the published image — one command, no clone

Every push to `main` publishes the image to GitHub's container registry, so the
app can be run anywhere Docker runs:

```bash
docker run --rm -p 8000:8000 \
  -e GEMINI_API_KEY=your-key-here \
  ghcr.io/subhram-rana-1/ads-design:latest
```

Open <http://localhost:8000>.

The image is self-contained — prompts, the publisher/persona catalog and the UI
are all baked in, and FastAPI serves the UI from the same process. CI asserts
this on every build, because an image that only works with compose mounts is
not deployable.

---

## 3. Hosted on Render — optional public link

[`render.yaml`](render.yaml) is a blueprint. Use **Blueprint**, not **Web Service** — only Blueprint reads `render.yaml`:

1. <https://render.com> → **New** → **Blueprint** → connect this repo
2. Render reads the blueprint and **prompts for `GEMINI_API_KEY`**. It is
   declared `sync: false`, which means the value is entered in Render's
   dashboard and stored encrypted. It is never committed.
3. Deploy.

If you create the service by hand instead (**New → Web Service**), Render
ignores `render.yaml` and you must set the env var yourself. The Dockerfile is
at the repo root so the build works either way.

One service, not two. A static-site CDN in front of the API would buffer the
SSE stream, and the generation progress would sit silent for a minute and then
jump to done.

**Caveat worth knowing before you share the link:** Render's free tier spins
down after inactivity and cold-starts take roughly 50 seconds. A reviewer's
first click would look broken. Use it as a bonus link, not the main path.

---

## Where the key lives

| Context | Where the key goes | Committed? |
|---|---|---|
| Local | `.env` | No — gitignored |
| GitHub repo | Nowhere | Never |
| GitHub Actions | Repo secret `GEMINI_API_KEY` | No — encrypted by GitHub |
| GHCR image | Passed at `docker run` time | No — not in the image |
| Render | Dashboard env var (`sync: false`) | No — encrypted by Render |

CI enforces two of these on every push: it fails the build if a `AIza…` string
appears in any tracked file or if `.env` is ever committed, and it fails the
smoke test if the key shows up in the container logs.

---

## What CI does

| Job | Runs on | Uses the key? |
|---|---|---|
| `verify` | every push and PR | No — LLM calls are stubbed |
| `smoke` | `main` and manual | Yes — two real calls |
| `publish` | `main` | No |

`verify` runs `backend/verify_pipeline.py`, which replaces every Gemini call
with a schema-shaped stub and asserts the invariants that matter: allocations
sum to the budget exactly, every publisher gets a verdict, ad sets stay within
3–5 personas, headlines fit 60 characters. It needs no key and no network.

`smoke` is the counterpart: it starts the real container and streams one actual
chat turn, which is the only way to catch a broken key, a schema Gemini has
stopped accepting, or a regression in the streaming path.
