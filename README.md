# Civic Garbage & Waste Monitor

A FastAPI + single-page application that turns a photo of uncollected civic waste into a
privacy-sanitized municipal complaint with a short-lived evidence link and optional
photo-attached email delivery.

```
photo + lat/lon
   → OpenCV PII redaction (faces, plates, 51×51 blur)
   → multi-provider VLM inspection (Gemini → OpenRouter)
   → strict JSON schema validation
   → Shapely ward lookup + Nominatim address
   → complaint letter + evidence link
   → mailto / Gmail / WhatsApp / SMTP-with-attachment
```

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# fill in GEMINI_API_KEY and OPENROUTER_API_KEY
uvicorn backend.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000/>. API docs are at `/docs`.

> **Keys:** `.env` is gitignored and the old hardcoded credentials have been removed
> from source. If those keys were ever shared, rotate them in the Gemini/OpenRouter
> consoles.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Google Gemini key (primary provider) |
| `OPENROUTER_API_KEY` | — | OpenRouter key (fallback provider) |
| `VLM_CONFIG_PATH` | `backend/vlm_providers.json` | Alternative provider/model registry |
| `VLM_TOTAL_TIMEOUT_SECONDS` | `30` | Whole-chain deadline |
| `VLM_HEALTH_CACHE_SECONDS` | `60` | Cache TTL for `/api/health/providers` |
| `VLM_STARTUP_CHECK` | `0` | Validate model IDs at boot |
| `VLM_STRICT_STARTUP_CHECK` | `0` | Fail startup if validation fails |
| `SMTP_HOST` | — | Enables server-side email delivery |
| `SMTP_PORT` | `587` (`465` with SSL) | SMTP port |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | — | Optional SMTP auth |
| `SMTP_FROM` | username | Sender address |
| `SMTP_USE_SSL` / `SMTP_USE_TLS` | auto | Transport security |
| `PUBLIC_BASE_URL` | request origin | Public origin for evidence links behind a proxy |
| `EVIDENCE_TTL_SECONDS` | `86400` | Evidence-link lifetime |
| `EVIDENCE_MAX_ENTRIES` | `100` | In-memory photo cap |

Provider and model selection lives in [`backend/vlm_providers.json`](backend/vlm_providers.json).
Each provider names an environment variable, never a key. Add, reorder, or disable
models there without touching Python.

## VLM fallback semantics

The router ([`backend/vlm_router.py`](backend/vlm_router.py)) distinguishes failure
types instead of treating every non-200 the same:

| Signal | Class | Action |
|---|---|---|
| 401 / 403 | auth | Open provider circuit for 10 min; do not retry |
| 404 / 410 / "decommissioned" | dead model | Open model circuit for 15 min; next model |
| 429 | rate limit | Honour `Retry-After`, retry once, then next model |
| 5xx | upstream | Backoff + retry once, then next model |
| Timeout / connection | transport | Retry once, then next model |
| 400 | bad request | Retry once without `response_format`; then next model |
| 200 + invalid JSON/schema | bad output | Retry once; then next model |

Additional protections:

- **Overall deadline** (`VLM_TOTAL_TIMEOUT_SECONDS`, default 30 s) checked before every
  attempt, so a chain cannot run unbounded.
- **Per-attempt timeout** from the provider config (Gemini 20 s, OpenRouter 15 s).
- **Circuit breakers** per provider and per model, with 60 s cooldown for transient
  failures, 15 min for dead models, and 10 min for auth failures.
- **Capability-aware payloads**: `response_format` is only sent for models with
  `json_mode: true`.

Groq is intentionally absent from the registry: its current catalog has no
vision-capable model, so including it only adds a guaranteed HTTP 400.

## Health and observability

```http
GET /api/health
GET /api/health/providers          # validates live model catalogs, cached 60 s
GET /api/health/providers?force=true
```

`/api/health/providers` returns, per provider: configured vs available models, missing
models, vision support where the catalog exposes modalities, plus breaker state and
router counters (attempts, success rate, retries, fallbacks, per-model latency). It
never returns API keys.

Optional boot-time validation:

```powershell
$env:VLM_STARTUP_CHECK = "1"
$env:VLM_STRICT_STARTUP_CHECK = "0"   # set to 1 to fail startup on a dead model
```

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/report` | Multipart photo + `lat`/`lon` → analysis, ward, letter, evidence link |
| `GET /api/evidence/{token}` | Sanitized JPEG behind a 24 h bearer link |
| `POST /api/reports/{id}/email` | Optional SMTP send with the JPEG attached |
| `GET /api/health` | Liveness + provider counts |
| `GET /api/health/providers` | Live model and circuit health |

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The suite covers privacy redaction, ward routing, API integration, evidence links,
SMTP delivery, provider configuration, error classification, retries, circuit breakers,
health checks, and metrics. Provider HTTP calls are mocked; no test spends API quota.

## Project layout

| Path | Role |
|---|---|
| [`backend/main.py`](backend/main.py) | FastAPI composition, complaint compiler, evidence/email endpoints |
| [`backend/privacy.py`](backend/privacy.py) | Haar-cascade PII redaction |
| [`backend/vlm_router.py`](backend/vlm_router.py) | Config-driven fallback, retries, deadlines |
| [`backend/vlm_config.py`](backend/vlm_config.py) | Provider/model registry loader |
| [`backend/vlm_providers.json`](backend/vlm_providers.json) | Ordered providers and models |
| [`backend/circuit_breaker.py`](backend/circuit_breaker.py) | Per-provider/model breakers |
| [`backend/vlm_metrics.py`](backend/vlm_metrics.py) | In-process attempt metrics |
| [`backend/provider_check.py`](backend/provider_check.py) | `/models` catalog validation |
| [`backend/authority_router.py`](backend/authority_router.py) | Ward polygons, Nominatim, Haversine |
| [`backend/evidence.py`](backend/evidence.py) | TTL-bounded evidence store |
| [`backend/email_dispatch.py`](backend/email_dispatch.py) | SMTP delivery with JPEG attachment |
| [`frontend/index.html`](frontend/index.html) | Map, upload, results, dispatch UI |

## Known limitations

- Evidence and breaker/metric state are **process-local**. Multi-worker deployments
  need Redis or a database; a report created in one worker is invisible to another.
- The API has no authentication or rate limiting; the email endpoint caps sends at five
  per report but report creation still costs a VLM call.
- Ward polygons are placeholder rectangles and municipal addresses are unverified.
- The evidence link is a bearer capability: anyone with the URL can view the blurred
  photo until it expires.
- PII detection uses frontal-face and Russian-plate Haar cascades; undetected PII is
  still sent to the VLM.
