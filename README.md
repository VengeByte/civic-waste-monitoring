# Civic Waste Reporter

A FastAPI + modern citizen-friendly web application that turns a photo of uncollected civic waste into an official municipal complaint with automatic local privacy anonymization (faces & license plates blurred) and a photo-attached Gmail draft preloaded for 1-click dispatch.

* **Live Cloud Service**: [https://civic-waste-monitoring.onrender.com](https://civic-waste-monitoring.onrender.com)
* **Privacy Policy**: [https://civic-waste-monitoring.onrender.com/privacy](https://civic-waste-monitoring.onrender.com/privacy)
* **Interactive API Docs**: [https://civic-waste-monitoring.onrender.com/docs](https://civic-waste-monitoring.onrender.com/docs)
* **Provider & Circuit Diagnostics**: [https://civic-waste-monitoring.onrender.com/api/health/providers](https://civic-waste-monitoring.onrender.com/api/health/providers)

```
photo + map location pin
   → OpenCV PII redaction (faces, vehicle plates, 51×51 Gaussian blur via bundled cascades)
   → multi-provider VLM inspection (Gemini → OpenRouter fallback)
   → strict JSON schema validation
   → 4-tier municipal authority routing (Ward → Corporation → State ULB Directorate → Central)
   → official complaint letter with JPEG evidence attached
   → Gmail Compose opens directly with prefilled recipient, complaint body & photo attached
```

---

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Fill in GEMINI_API_KEY, OPENROUTER_API_KEY, and Google OAuth credentials in .env
uvicorn backend.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000/> in your browser. API docs are available at `/docs`.

> **Security Note:** `.env` is gitignored and credentials must never be committed to source control. Set API keys in your local `.env` or in your cloud provider's dashboard.

---

## Production Deployment (Render)

The application is configured for deployment on [Render](https://render.com) as a native Python web service without Docker overhead:

### Service Settings
* **Environment**: `Python 3`
* **Build Command**: `pip install -r requirements.txt`
* **Start Command**: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT --workers 1`

> **Why `--workers 1`?** Temporary evidence photo tokens, circuit breakers, and Google OAuth state tokens are maintained in an in-memory session store (`GoogleSessionStore`). A single worker process guarantees that OAuth callbacks and evidence downloads route to the exact process that initiated them.

### Required Environment Variables on Render

| Variable | Description / Source |
|---|---|
| `GEMINI_API_KEY` | Primary VLM key from [Google AI Studio](https://aistudio.google.com/app/apikey) |
| `OPENROUTER_API_KEY` | Secondary fallback VLM key from [OpenRouter](https://openrouter.ai/) |
| `GOOGLE_CLIENT_ID` | OAuth 2.0 Web Client ID from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | OAuth 2.0 Client Secret from Google Cloud Console |
| `GOOGLE_REDIRECT_URI` | `https://civic-waste-monitoring.onrender.com/auth/google/callback` |
| `PUBLIC_BASE_URL` | `https://civic-waste-monitoring.onrender.com` |

---

## Configuration Reference

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Google Gemini key (primary VLM provider) |
| `OPENROUTER_API_KEY` | — | OpenRouter key (fallback VLM provider) |
| `VLM_CONFIG_PATH` | `backend/vlm_providers.json` | Path to provider/model routing registry |
| `VLM_TOTAL_TIMEOUT_SECONDS` | `30` | End-to-end VLM fallback deadline |
| `VLM_HEALTH_CACHE_SECONDS` | `60` | Cache TTL for `/api/health/providers` |
| `VLM_STARTUP_CHECK` | `0` | Validate model catalogs at boot (`1` to enable) |
| `VLM_STRICT_STARTUP_CHECK` | `0` | Fail startup if any configured model is missing |
| `GOOGLE_CLIENT_ID` | — | Gmail OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | — | Gmail OAuth client secret |
| `GOOGLE_REDIRECT_URI` | derived | Redirect URI matching Google Cloud Console exactly |
| `PUBLIC_BASE_URL` | request origin | Public base URL used for OAuth and evidence links |
| `EVIDENCE_TTL_SECONDS` | `86400` | Evidence-link lifetime (24 hours) |
| `EVIDENCE_MAX_ENTRIES` | `100` | Maximum in-memory evidence photo buffer |
| `CENTRAL_GRIEVANCE_EMAIL` | `grievance.central@chd.nic.in` | Fallback recipient for unmapped coordinates |
| `SMTP_HOST` / `SMTP_PORT` | — | Optional legacy server-side SMTP delivery |

Provider and model selection lives in [`backend/vlm_providers.json`](backend/vlm_providers.json). Each provider names an environment variable, never a raw secret.

---

## Local Privacy & PII Anonymization

Before any image is sent to an external AI provider or saved as evidence, the backend runs local computer vision anonymization:

1. **Facial Detection**: Identifies human faces using frontal face Haar cascades.
2. **License Plate Detection**: Identifies vehicle registration plates using Russian/international plate Haar cascades.
3. **Heavy Gaussian Blurring**: Applies a `51×51` Gaussian blur kernel over all detected bounding boxes.
4. **Bundled Offline Models**: Haar XML classifiers are bundled inside [`backend/data/cascades/`](backend/data/cascades/) (`haarcascade_frontalface_default.xml` and `haarcascade_russian_plate_number.xml`), ensuring reliable offline execution across Windows and Linux.
5. **Headless Linux Compatibility**: Pinned `opencv-python-headless>=4.8.0,<5.0.0` in [`requirements.txt`](requirements.txt) with dual-path class resolution (`cv2.CascadeClassifier` / `cv2.objdetect.CascadeClassifier`) to ensure compatibility in headless cloud runtimes.

---

## Gmail Draft Setup & Google Cloud Configuration

The application provides a single-click dispatch action: **Send via Gmail**. The backend uses the Google Gmail API to compose a full draft with the formal municipal letter, recipient authority, and PII-blurred JPEG evidence attached. It then opens Gmail directly with the compose window popped up in front of the user (`#inbox?compose=<messageId>`).

### Google Cloud Console Configuration:
1. **Google Cloud Project**:
   - Go to [Google Cloud Console](https://console.cloud.google.com/) and create or select your project.
2. **Enable Gmail API**:
   - In **APIs & Services** &rarr; **Library**, search for **Gmail API** and click **Enable**.
3. **Configure OAuth Consent Screen**:
   - Go to **Google Auth Platform** &rarr; **Branding**:
     - **App name**: `Civic Waste Monitor`
     - **User support email**: Your developer email.
     - **Application home page**: `https://civic-waste-monitoring.onrender.com`
     - **Application privacy policy link**: `https://civic-waste-monitoring.onrender.com/privacy`
   - In **Data Access** / **Scopes**:
     - Add `https://www.googleapis.com/auth/gmail.compose`.
   - In **Audience**:
     - To allow citizens worldwide to log in, click **`Publish app`** &rarr; **Confirm** (transitions app from *Testing* to *In production*).
4. **Create OAuth 2.0 Web Credentials**:
   - In **Clients** / **Credentials** &rarr; **Create Credentials** &rarr; **OAuth client ID**.
   - Application type: **Web application**.
   - **Authorized redirect URIs**:
     - Local development: `http://localhost:8000/auth/google/callback`
     - Production: `https://civic-waste-monitoring.onrender.com/auth/google/callback`

### OAuth Scope & Privacy Compliance:
* **Scope**: `https://www.googleapis.com/auth/gmail.compose`
* **Limited Use Disclosure**: This scope only creates a draft in the user's Gmail account. The app **cannot** send emails autonomously, cannot read the user's inbox, and cannot delete emails. The citizen retains full control to review the draft and click **Send** inside Gmail.
* **Public Privacy Policy**: Served at [`/privacy`](https://civic-waste-monitoring.onrender.com/privacy) and defined in [`frontend/privacy.html`](frontend/privacy.html).

---

## VLM Fallback Semantics

The router ([`backend/vlm_router.py`](backend/vlm_router.py)) distinguishes failure types to ensure uninterrupted processing:

| Signal | Class | Action |
|---|---|---|
| 401 / 403 | auth | Open provider circuit for 10 min; do not retry |
| 404 / 410 / "decommissioned" | dead model | Open model circuit for 15 min; advance to next model |
| 429 | rate limit | Honour `Retry-After`, retry once, then advance |
| 5xx | upstream | Exponential backoff + retry once, then advance |
| Timeout / connection | transport | Retry once, then advance |
| 400 | bad request | Retry once without `response_format`; then advance |
| 200 + invalid JSON/schema | bad output | Retry once; then advance |

* **Total Timeout**: Bounded by `VLM_TOTAL_TIMEOUT_SECONDS` (default: 30 s).
* **Circuit Breakers**: Independent per-provider and per-model breakers with automated recovery.
* **Diagnostics Endpoint**: `GET /api/health/providers` reports live catalog availability, model health, and breaker status.

---

## Countrywide Municipal Authority Resolution

The system employs a 4-tier hierarchical authority routing engine to resolve any GPS coordinate across India:

```
Coordinates (lat, lon)
   │
   ├─► Tier 1: Granular Ward Polygons (e.g. Chandigarh Ward Directory)
   │     └─► Matches specific ward sanitary inspector & councillor
   │
   ├─► Tier 2: Municipal Corporation Boundaries (GIS Polygons)
   │     └─► Point-in-polygon matching against covered municipal GIS polygons (30 covered cities)
   │
   ├─► Tier 3: Pan-India State & City Authority Router (backend/state_authority_router.py)
   │     ├─► City-Level Directory: Matches recognized corporations (Mysuru, BBMP Bengaluru, Pune, Indore, Surat, etc.)
   │     ├─► State Nodal Directorates: Covers all 28 States & 8 Union Territories routing to the respective Directorate of Urban Local Bodies
   │     └─► Offline Bounding Boxes: Geofence fallback if reverse geocoding is unreachable
   │
   └─► Tier 4: Central Nodal Grievance Fallback
         └─► Fallback contact (grievance.central@chd.nic.in, configurable via CENTRAL_GRIEVANCE_EMAIL)
```

Spatial data preparation tools are located in [`scripts/gis/`](scripts/gis/). See [`scripts/gis/README.md`](scripts/gis/README.md) for details.

---

## API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | `GET` | Citizen web interface |
| `/privacy` | `GET` | Google-compliant public Privacy Policy |
| `/api/report` | `POST` | Multipart photo + coordinates &rarr; PII blur, VLM analysis, authority routing, complaint letter, evidence token |
| `/api/evidence/{token}` | `GET` | View sanitized evidence JPEG behind a 24-hour bearer link |
| `/auth/google/status` | `GET` | Check Google OAuth status and login availability |
| `/auth/google/start` | `GET` | Initiate Google OAuth consent flow for report draft dispatch |
| `/auth/google/callback` | `GET` | OAuth callback &rarr; creates photo-attached draft & 302 redirects to Gmail compose |
| `/api/reports/{id}/draft` | `PATCH` | Sync citizen edits to complaint body before draft creation |
| `/api/reports/{id}/email` | `POST` | Optional legacy SMTP delivery with attached JPEG |
| `/api/health` | `GET` | System liveness and basic provider status |
| `/api/health/providers` | `GET` | Real-time VLM provider, model catalog, and circuit breaker health |

---

## Tests

The project includes an extensive automated test suite covering privacy redaction, VLM routing, authority resolution, Gmail MIME drafting, evidence expiration, and health endpoints:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

All 133 tests mock external HTTP requests, requiring zero network bandwidth and consuming zero API quota.

---

## Project Layout

| Path | Role |
|---|---|
| [`backend/main.py`](backend/main.py) | FastAPI application entry point, complaint compiler, evidence & OAuth routes |
| [`backend/gmail_draft.py`](backend/gmail_draft.py) | Google OAuth 2.0 flow, session management, MIME draft construction |
| [`backend/privacy.py`](backend/privacy.py) | OpenCV Haar cascade face & plate detection and Gaussian blur |
| [`backend/data/cascades/`](backend/data/cascades/) | Bundled Haar cascade XML models for offline face & plate detection |
| [`backend/vlm_router.py`](backend/vlm_router.py) | Multi-provider VLM routing, fallbacks, retries, and timeouts |
| [`backend/vlm_config.py`](backend/vlm_config.py) | Provider registry loader and configuration parser |
| [`backend/vlm_providers.json`](backend/vlm_providers.json) | Priority-ordered VLM provider and model definitions |
| [`backend/circuit_breaker.py`](backend/circuit_breaker.py) | Provider and model circuit breakers |
| [`backend/vlm_metrics.py`](backend/vlm_metrics.py) | In-memory VLM attempt latency and error tracking |
| [`backend/provider_check.py`](backend/provider_check.py) | Live `/models` catalog inspection |
| [`backend/authority_router.py`](backend/authority_router.py) | Ward polygons, Nominatim reverse geocoding, spatial indexing |
| [`backend/state_authority_router.py`](backend/state_authority_router.py) | Pan-India 28 State & 8 UT nodal grievance routing |
| [`backend/evidence.py`](backend/evidence.py) | In-memory TTL-bounded photo evidence repository |
| [`backend/email_dispatch.py`](backend/email_dispatch.py) | Legacy SMTP transport with JPEG attachment |
| [`backend/data/municipal/`](backend/data/municipal/) | Municipal contacts and spatial boundary GeoJSON datasets |
| [`frontend/index.html`](frontend/index.html) | Modern citizen interface (map location, photo capture, 1-click Gmail dispatch) |
| [`frontend/privacy.html`](frontend/privacy.html) | Public privacy policy complying with Google API User Data Policy |
| [`frontend/app.js`](frontend/app.js) | Frontend application logic, geolocation, API integration |
| [`scripts/gis/`](scripts/gis/) | Offline spatial dataset acquisition scripts (OSM & DataMeet) |
| [`tests/`](tests/) | Pytest automated test suite (133 tests) |

---

## Known Limitations & Architecture Notes

- **In-Memory State**: Temporary evidence images, circuit breakers, and OAuth CSRF tokens are process-local. For horizontal multi-worker scaling, back the session and evidence stores with Redis or an object store.
- **Gmail Draft vs Auto-Send**: The Gmail API creates a preloaded draft in the citizen's account; the user reviews and clicks **Send** inside Gmail. The application never sends emails autonomously on behalf of the user.
- **Geographic Precision**: Points outside the 30 granular boundary polygons seamlessly route to the respective State Directorate of Urban Local Bodies (Tier 3) or Central Grievance cell (Tier 4).
