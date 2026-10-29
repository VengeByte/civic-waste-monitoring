# Civic Garbage & Waste Monitor

A FastAPI + single-page application that turns a photo of uncollected civic waste into a
privacy-sanitized municipal complaint with a short-lived evidence link and a
photo-attached Gmail draft.

```
photo + lat/lon
   → OpenCV PII redaction (faces, plates, 51×51 blur)
   → multi-provider VLM inspection (Gemini → OpenRouter)
   → strict JSON schema validation
   → ward / municipal-corporation lookup + Nominatim address
   → complaint letter + evidence link
   → Gmail draft with the JPEG attached; user selects sender and presses Send
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
| `GOOGLE_CLIENT_ID` | — | Gmail OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | — | Gmail OAuth client secret |
| `GOOGLE_REDIRECT_URI` | derived | Must exactly match the Google Cloud redirect URI |
| `SMTP_HOST` | — | Optional legacy server-side email endpoint |
| `SMTP_PORT` | `587` (`465` with SSL) | SMTP port |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | — | Optional SMTP auth |
| `SMTP_FROM` | username | Sender address |
| `SMTP_USE_SSL` / `SMTP_USE_TLS` | auto | Transport security |
| `PUBLIC_BASE_URL` | request origin | Public origin for evidence and OAuth links |
| `EVIDENCE_TTL_SECONDS` | `86400` | Evidence-link lifetime |
| `EVIDENCE_MAX_ENTRIES` | `100` | In-memory photo cap |

Provider and model selection lives in [`backend/vlm_providers.json`](backend/vlm_providers.json).
Each provider names an environment variable, never a key. Add, reorder, or disable
models there without touching Python.

## Gmail draft setup (Google Cloud Console)

The application provides a seamless, single-click dispatch action: **Send via Gmail**.
Instead of opening an empty email without attachments, the backend uses the Google Gmail API to compose a full draft with the complaint text, municipal recipient, and PII-blurred evidence JPEG already attached. It then opens Gmail directly on that draft for the selected account so the user only reviews and presses **Send** — they never attach anything manually.

### Step-by-Step Google Cloud Configuration:
1. **Create a Google Cloud Project**:
   - Go to the [Google Cloud Console](https://console.cloud.google.com/).
   - Create a new project (e.g., `civic-waste-monitor`).
2. **Enable the Gmail API**:
   - In APIs & Services → Library, search for **Gmail API** and click **Enable**.
3. **Configure the OAuth Consent Screen**:
   - Go to APIs & Services → OAuth consent screen.
   - Choose **External** user type (Testing mode is ideal for local evaluation / college projects).
   - Fill in the App name and developer contact email.
   - Under **Scopes**, add `https://www.googleapis.com/auth/gmail.compose`.
   - Under **Test users**, add your personal Gmail address(es) that you will use to test sending.
4. **Create OAuth 2.0 Credentials**:
   - Go to APIs & Services → Credentials → **Create Credentials** → **OAuth client ID**.
   - Application type: **Web application**.
   - Name: `Civic Waste Monitor Web App`.
   - Under **Authorized redirect URIs**, add:
     - Local: `http://localhost:8000/auth/google/callback`
     - Local alternative: `http://127.0.0.1:8000/auth/google/callback`
     - Production/Deployed: `https://your-domain.example/auth/google/callback`
5. **Add Credentials to `.env`**:
   - Copy Client ID and Client Secret into your `.env` file:
   ```ini
   GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your-client-secret
   GOOGLE_REDIRECT_URI=http://localhost:8000/auth/google/callback
   PUBLIC_BASE_URL=http://localhost:8000
   ```

### OAuth Scope & Security:
- **Scope used**: `https://www.googleapis.com/auth/gmail.compose`
- **Why this scope?**: This scope grants permission to create and save email drafts in the user's Gmail account, but does **NOT** grant permission to send emails on their behalf or read their inbox. The user maintains full autonomy and must review the draft and manually press Send inside the Gmail interface.
- **Single-Worker Session Store Caveat**: OAuth tokens and CSRF state are stored in an in-memory session store (`GoogleSessionStore`). For a local demo or single-process deployment (`uvicorn` with 1 worker), this is completely fine. For horizontal multi-worker or multi-container deployments, backing this store with Redis or a persistent database is recommended.

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

## Countrywide municipal data — pipeline only, not wired yet

The offline acquisition pipeline is in [`scripts/gis/`](scripts/gis/) and writes:

- `backend/data/municipal/municipal_corporations.json` — 54 contacts
- `backend/data/municipal/municipal_boundaries.geojson` — boundary polygons where available
- `backend/data/municipal/review_report.csv` — QA table
- `backend/data/municipal/manifest.json` — query audit log

The app now ships a **30-record covered dataset**; the 24 corporations without a usable
boundary were removed from the output and are listed in
`manifest.json → excluded_missing`. Of the 30, 11 use DataMeet community
ward/boundary maps (CC BY 4.0) and the rest use OpenStreetMap or an approximate city
boundary. Routing order is:

1. Chandigarh ward polygon, if matched.
2. One of the 30 covered municipal corporation polygons.
3. Central municipal grievance fallback for every pin outside those boundaries; the
   complaint is emailed to `grievance.central@chd.nic.in` (override with
   `CENTRAL_GRIEVANCE_EMAIL`).

See [`scripts/gis/README.md`](scripts/gis/README.md) to rerun the pipeline or add more
covered cities.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/report` | Multipart photo + `lat`/`lon` → analysis, ward/municipal routing, letter, evidence link |
| `GET /api/evidence/{token}` | Sanitized JPEG behind a 24 h bearer link |
| `GET /auth/google/status` | Whether Gmail OAuth is configured and connected |
| `GET /auth/google/start?report_id={id}` | Google account chooser / OAuth consent redirect for report |
| `GET /auth/google/callback` | OAuth callback; creates JPEG-attached draft and 302 redirects directly to Gmail |
| `PATCH /api/reports/{id}/draft` | Sync user-edited complaint text before dispatch |
| `POST /api/reports/{id}/email` | Optional legacy SMTP send with the JPEG attached |
| `GET /api/health` | Liveness + provider counts |
| `GET /api/health/providers` | Live model and circuit health |

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The suite covers privacy redaction, ward/municipal routing, API integration, evidence
links, Gmail OAuth/draft MIME, provider configuration, error classification, retries,
circuit breakers, health checks, and metrics. Provider and Google HTTP calls are mocked;
no test spends API quota.

## Project layout

| Path | Role |
|---|---|
| [`backend/main.py`](backend/main.py) | FastAPI composition, complaint compiler, evidence/Gmail endpoints |
| [`backend/gmail_draft.py`](backend/gmail_draft.py) | Gmail OAuth, session storage, draft creation |
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
| [`backend/data/municipal/`](backend/data/municipal/) | Countrywide contacts + boundary dataset (30/54 polygons) |
| [`scripts/gis/`](scripts/gis/) | Offline OSM + DataMeet acquisition pipeline |
| [`frontend/index.html`](frontend/index.html) | Map, upload, results, dispatch UI |

## Known limitations

- Evidence, breaker/metric state, and Gmail OAuth sessions are **process-local**.
  Multi-worker deployments need Redis or a database; a report or Gmail session created
  in one worker is invisible to another. A server restart requires reconnecting Gmail.
- Gmail draft creation requires a Google Cloud OAuth client and (in Testing mode) test
  users. The draft is created but **not sent**; the user presses Send in Gmail.
- The API has no authentication or rate limiting; the legacy SMTP endpoint caps sends
  at five per report, but report creation still costs a VLM call.
- Ward polygons are placeholder rectangles and municipal addresses are unverified.
- The evidence link is a bearer capability: anyone with the URL can view the blurred
  photo until it expires.
- PII detection uses frontal-face and Russian-plate Haar cascades; undetected PII is
  still sent to the VLM.
