"""
FastAPI Application and Civic Complaint Handoff Compiler.

Connects:
- backend.privacy (Image & Frame PII Anonymization)
- backend.vlm_router (Multi-Provider VLM Waste Analysis)
- backend.authority_router (Municipal Ward Resolution & Haversine Deduplication)
- backend.evidence (Short-Lived Evidence Links & Report Retention)
- backend.email_dispatch (Photo-Attached SMTP Delivery)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from backend.authority_router import resolve_authority
from backend.circuit_breaker import CIRCUIT_BREAKER
from backend.email_dispatch import (
    EmailDeliveryError,
    SMTPConfig,
    build_email_message,
    send_complaint_email,
)
from backend.env_loader import env_bool
from backend.evidence import EVIDENCE_STORE
from backend.gmail_draft import (
    GMAIL_SESSIONS,
    GMAIL_STATES,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    GmailOAuthError,
    GoogleOAuthConfig,
    build_draft_url,
    build_raw_message,
    create_gmail_draft,
    exchange_code,
    fetch_userinfo,
    refresh_access_token,
)
from backend.privacy import blur_pii_in_image
from backend.provider_check import check_all
from backend.vlm_config import DEFAULT_PROVIDERS, configured_api_key_count
from backend.vlm_metrics import VLM_METRICS
from backend.vlm_router import analyze_with_vlm

# Configure application logging
logger = logging.getLogger("backend.main")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [api]: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


async def _validate_providers_on_startup() -> None:
    """
    Optionally validate configured models at boot.

    Disabled by default so offline development and tests do not depend on provider
    reachability. Enable with VLM_STARTUP_CHECK=1; VLM_STRICT_STARTUP_CHECK=1 turns a
    failed check into a startup error.
    """
    if not env_bool("VLM_STARTUP_CHECK", False):
        return

    results = await check_all(DEFAULT_PROVIDERS, force=True)
    for result in results:
        if result.ok:
            logger.info("VLM startup check: %s OK (%s).", result.name, result.detail)
        else:
            logger.warning("VLM startup check: %s %s — %s", result.name, result.status, result.detail)

    if env_bool("VLM_STRICT_STARTUP_CHECK", False) and not all(r.ok for r in results):
        raise RuntimeError("One or more configured VLM providers failed startup validation.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        from backend.privacy import load_cascades
        from backend.authority_router import load_ward_directory
        load_cascades()
        load_ward_directory()
    except Exception as exc:
        logger.warning("Could not pre-warm local models on startup: %s", exc)
    await _validate_providers_on_startup()
    yield


# Initialize FastAPI application
app = FastAPI(
    title="Civic Garbage Disposal & Sanitation Monitor API",
    description="Automated Civic Waste Detection, PII Anonymization, and Municipal Handoff Engine",
    version="2.0.0",
    lifespan=lifespan,
)

# Configure Cross-Origin Resource Sharing (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Server-side delivery guardrails (the endpoint is unauthenticated in this prototype).
MAX_SENDS_PER_REPORT = 5
MAX_EMAIL_BODY_CHARS = 20_000
MAX_EMAIL_SUBJECT_CHARS = 300


class EmailDispatchRequest(BaseModel):
    """Optional overrides when sending a stored complaint through the server."""

    body: Optional[str] = Field(
        default=None,
        max_length=MAX_EMAIL_BODY_CHARS,
        description="Edited complaint text; defaults to the compiled draft.",
    )
    subject: Optional[str] = Field(
        default=None,
        max_length=MAX_EMAIL_SUBJECT_CHARS,
        description="Edited subject line; defaults to the compiled draft.",
    )


def build_evidence_url(request: Request, token: str) -> str:
    """
    Build the absolute, shareable evidence URL for a retained report photograph.

    ``PUBLIC_BASE_URL`` overrides the request-derived origin for deployments behind
    a proxy or on a custom domain.
    """
    override = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if override:
        return f"{override}/api/evidence/{token}"
    return str(request.url_for("get_evidence", token=token))


@app.get("/api/health", summary="Service Health Check")
async def health_check() -> dict[str, Any]:
    """Health check endpoint indicating operational status."""
    return {
        "status": "healthy",
        "service": "civic-garbage-api",
        "vlm_providers": len(DEFAULT_PROVIDERS),
        "vlm_keys_configured": configured_api_key_count(),
    }


@app.get("/api/health/providers", summary="VLM Provider & Model Health")
async def provider_health(force: bool = False) -> dict[str, Any]:
    """
    Validate configured models against each provider's live catalog.

    Results are cached briefly; pass ``?force=true`` for a fresh check. The response
    includes breaker state and router counters, and never contains API keys.
    """
    results = await check_all(DEFAULT_PROVIDERS, force=force)
    return {
        "status": "ok" if all(result.ok for result in results) else "degraded",
        "providers": [result.to_dict() for result in results],
        "circuits": CIRCUIT_BREAKER.snapshot(),
        "metrics": VLM_METRICS.snapshot(),
    }



def compile_complaint_letter(
    authority: dict[str, Any],
    vlm_result: dict[str, Any],
    lat: float,
    lon: float,
    captured_at: Optional[str] = None,
    evidence_url: Optional[str] = None,
) -> tuple[str, str, str, list[str]]:
    """
    Compile the formal complaint letter, subject line, and recipient lists.

    Args:
        authority: Municipal authority routing dictionary.
        vlm_result: Structured VLM analysis dictionary.
        lat: Latitude of incident.
        lon: Longitude of incident.
        captured_at: Optional timestamp string.
        evidence_url: Optional short-lived link to the sanitized evidence photograph.

    Returns:
        tuple[subject, body, to_email, cc_emails]
    """
    municipality = authority.get("municipality_name", "Municipal Corporation")
    ward_number = authority.get("ward_number", "Unmapped")
    ward_name = authority.get("ward_name", "Central Grievance Zone")
    si_email = authority.get("sanitary_inspector_email", "").strip()
    moh_email = authority.get("moh_email", "").strip()
    comm_email = authority.get("commissioner_email", "").strip()

    to_email = si_email or moh_email or "sanitation-grievance@municipal.gov.in"
    cc_list: list[str] = [e for e in [moh_email, comm_email] if e and e != to_email]

    time_str = (
        captured_at.strip()
        if captured_at and captured_at.strip()
        else datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    )

    formatted_address = (
        authority.get("formatted_address")
        or authority.get("resolved_address")
        or f"Lat: {lat:.6f}, Lon: {lon:.6f}"
    )
    gmaps_link = f"https://www.google.com/maps?q={lat},{lon}"

    waste_types = vlm_result.get("waste_types", [])
    waste_str = ", ".join(waste_types) if waste_types else "Unsegregated municipal waste"
    severity_str = str(vlm_result.get("severity", "medium")).capitalize()
    description = vlm_result.get("official_complaint_description", "").strip()

    match_type = str(authority.get("match_type", "ward"))
    is_ward = match_type == "ward"
    is_central = match_type == "central_fallback"
    website = str(authority.get("website", "")).strip()
    helpline = str(authority.get("helpline", "")).strip()
    boundary_quality = str(authority.get("boundary_quality", "")).strip()

    if is_ward:
        subject = (
            f"URGENT: Municipal Waste Clearance Request - "
            f"Ward {ward_number} ({ward_name})"
        )
        recipient_block = (
            f"The Sanitary Inspector & Medical Officer of Health (MOH)\n"
            f"{municipality} (Ward {ward_number}: {ward_name})"
        )
        salutation = "Respected Municipal Officers,"
        authority_line = f"• Authority Basis    : Ward {ward_number}"
    elif is_central:
        subject = (
            "URGENT: Municipal Waste Clearance Request - Central Grievance Cell"
        )
        recipient_block = "The Central Municipal Grievance Cell"
        salutation = "Respected Grievance Officer,"
        authority_line = "• Authority Basis    : Central municipal fallback"
    else:
        subject = f"URGENT: Municipal Waste Clearance Request - {municipality}"
        recipient_block = f"The Municipal Commissioner\n{municipality}"
        salutation = "Respected Municipal Commissioner,"
        authority_line = (
            f"• Authority Basis    : {boundary_quality or 'municipal'} boundary"
        )

    evidence_line = "• Photo Evidence      : Attached to this email (PII anonymized)\n"
    portal_line = f"• Municipal Portal    : {website}\n" if website else ""
    helpline_line = f"• Helpline            : {helpline}\n" if helpline else ""
    routing_note = (
        "This report could not be mapped to a specific municipal corporation. "
        "Please forward it to the urban local body with jurisdiction over the "
        "coordinates above.\n\n"
        if is_central
        else ""
    )

    body = (
        f"To,\n"
        f"{recipient_block}\n\n"
        f"Subject: Formal Grievance & Municipal Sanitation Clearance Demand\n\n"
        f"{salutation}\n\n"
        f"{description}\n\n"
        f"{routing_note}"
        f"Incident Inspection Details:\n"
        f"--------------------------------------------------\n"
        f"• Location / Address  : {formatted_address}\n"
        f"• GPS Coordinates     : {lat:.6f}, {lon:.6f}\n"
        f"• Google Maps Link    : {gmaps_link}\n"
        f"{evidence_line}"
        f"{authority_line}\n"
        f"{portal_line}"
        f"{helpline_line}"
        f"• Identified Waste    : {waste_str}\n"
        f"• Severity Level      : {severity_str}\n"
        f"• Timestamp of Report : {time_str}\n"
        f"--------------------------------------------------\n\n"
        f"You are requested to immediately dispatch the sanitation collection team for complete waste removal, "
        f"surface sweeping, and disinfection of this civic site.\n\n"
        f"Sincerely,\n"
        f"Civic Sanitation & Garbage Reporting Monitoring Cell"
    )

    return subject, body, to_email, cc_list


@app.post("/api/report", summary="Submit Civic Garbage Report")
async def report_civic_garbage(
    request: Request,
    file: UploadFile = File(..., description="Uploaded photo of reported waste site"),
    lat: float = Form(..., description="Latitude coordinate"),
    lon: float = Form(..., description="Longitude coordinate"),
    captured_at: Optional[str] = Form(None, description="Optional incident capture timestamp"),
) -> dict[str, Any]:
    """
    Comprehensive civic garbage analysis and municipal handoff endpoint:
    - Step A: Read and validate raw image payload.
    - Step B: Run PII blur (Haar Cascades + 51x51 Gaussian blur).
    - Step C: Perform VLM analysis; early return if no garbage detected.
    - Step D: Resolve municipal authority (ward boundary & Nominatim address).
    - Step E: Retain the sanitized photo behind a short-lived evidence link.
    - Step F: Compile formal complaint letter including the evidence URL.
    - Step G: Build the draft used by the Gmail API draft endpoint.
    """
    # Step A: Read raw bytes & validate non-empty payload
    raw_bytes = await file.read()
    if not raw_bytes or len(raw_bytes) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    # Step B: PII Anonymization via OpenCV
    try:
        sanitized_bytes, pii_blurred = blur_pii_in_image(raw_bytes)
    except Exception as exc:
        logger.error("PII anonymization failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Image processing failed: {exc}",
        )

    sanitized_image_b64 = base64.b64encode(sanitized_bytes).decode("ascii")

    # Step C: VLM Multi-Provider Analysis
    try:
        vlm_res = await analyze_with_vlm(sanitized_bytes)
        vlm_data = vlm_res.data if hasattr(vlm_res, "data") else dict(vlm_res)
    except Exception:
        logger.exception("VLM multi-provider analysis failed for coordinates (%.5f, %.5f).", lat, lon)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "VLM analysis failed because no configured vision provider produced a "
                "valid result. Please retry shortly."
            ),
        )

    # Early exit if no garbage is detected
    if not vlm_data.get("garbage_detected", False):
        logger.info("VLM reported zero waste detected at (%.5f, %.5f). Early return.", lat, lon)
        return {
            "status": "success",
            "garbage_detected": False,
            "pii_blurred": pii_blurred,
            "message": "No uncollected civic waste detected.",
            "vlm_result": vlm_data,
            "sanitized_image_b64": sanitized_image_b64,
        }

    # Step D: Point-in-Polygon Ward Resolution & Nominatim Geocoding
    authority = await resolve_authority(lat=lat, lon=lon)

    # Step E: Retain the sanitized photograph behind an unguessable, short-lived link.
    evidence_record = EVIDENCE_STORE.create(sanitized_bytes)
    evidence_url = build_evidence_url(request, evidence_record.token)
    logger.info(
        "Retained evidence for report %s... (expires %s).",
        evidence_record.token[:8],
        evidence_record.expires_at_iso,
    )

    # Step F: Compile Complaint Letter Body (includes the evidence link)
    subject, body, to_email, cc_list = compile_complaint_letter(
        authority=authority,
        vlm_result=vlm_data,
        lat=lat,
        lon=lon,
        captured_at=captured_at,
        evidence_url=evidence_url,
    )

    # Step G: Compile the draft used by the Gmail draft endpoint.
    draft = {
        "to": to_email,
        "cc": cc_list,
        "subject": subject,
        "body": body,
        "evidence_url": evidence_url,
        "evidence_expires_at": evidence_record.expires_at_iso,
    }
    EVIDENCE_STORE.set_draft(evidence_record.token, draft=draft, authority=authority)

    # Final Response Payload
    return {
        "status": "success",
        "garbage_detected": True,
        "pii_blurred": pii_blurred,
        "vlm_result": vlm_data,
        "authority": authority,
        "sanitized_image_b64": sanitized_image_b64,
        "report_id": evidence_record.token,
        "draft": draft,
    }


@app.get(
    "/api/evidence/{token}",
    name="get_evidence",
    summary="Retrieve Sanitized Report Photograph",
)
async def get_evidence(token: str) -> Response:
    """
    Serve the sanitized (PII-blurred) photograph for a retained report.

    The token is an unguessable bearer capability with a bounded lifetime. Expired or
    unknown tokens return 404 so the endpoint does not reveal whether a report existed.
    """
    record = EVIDENCE_STORE.get(token)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evidence link is invalid or has expired.",
        )

    return Response(
        content=record.image_bytes,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="civic-evidence-{token[:8]}.jpg"',
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.post(
    "/api/reports/{report_id}/email",
    summary="Email Complaint with Photo Attachment",
)
async def email_report(
    report_id: str,
    payload: Optional[EmailDispatchRequest] = None,
) -> dict[str, Any]:
    """
    Send the compiled complaint to the resolved municipal officers with the sanitized
    JPEG attached.

    Recipients come exclusively from the stored ward draft, never from the request, so
    this endpoint cannot be used as an open email relay. The request may override the
    editable subject/body (for example after the citizen edits the letter in the UI).
    """
    record = EVIDENCE_STORE.get(report_id)
    if record is None or not record.draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found or the evidence link has expired.",
        )

    if record.sent_count >= MAX_SENDS_PER_REPORT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Send limit reached for this report ({MAX_SENDS_PER_REPORT} attempts).",
        )

    config = SMTPConfig.from_env()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Server-side email delivery is not configured. Set SMTP_HOST (and optional "
                "SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_FROM) to enable photo-attached "
                "sending. The website uses the Gmail draft endpoint instead."
            ),
        )

    draft = record.draft
    subject = (payload.subject if payload and payload.subject else draft.get("subject", "")).strip()
    body = (payload.body if payload and payload.body else draft.get("body", "")).strip()
    to_email = str(draft.get("to", "")).strip()
    cc_list = [str(address).strip() for address in draft.get("cc", []) if str(address).strip()]

    if not subject or not body or not to_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Complaint draft is missing a recipient, subject, or body.",
        )

    filename = f"civic-evidence-{report_id[:8]}.jpg"
    try:
        delivery = await asyncio.to_thread(
            send_complaint_email,
            config,
            to=to_email,
            cc=cc_list,
            subject=subject,
            body=body,
            image_bytes=record.image_bytes,
            filename=filename,
        )
    except EmailDeliveryError:
        logger.exception("Server-side email delivery failed for report %s...", report_id[:8])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Email delivery failed. Check the server SMTP configuration and logs.",
        )
    except Exception:
        logger.exception("Unexpected error sending report %s...", report_id[:8])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected error while sending the complaint email.",
        )

    send_count = EVIDENCE_STORE.record_delivery(report_id) or (record.sent_count + 1)
    sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    logger.info(
        "Report %s... emailed to %s with %d attachment bytes (send %d/%d).",
        report_id[:8],
        to_email,
        delivery["attachment_bytes"],
        send_count,
        MAX_SENDS_PER_REPORT,
    )

    return {
        "status": "sent",
        "report_id": report_id,
        "to": to_email,
        "cc": cc_list,
        "subject": subject,
        "attachment_bytes": delivery["attachment_bytes"],
        "message_bytes": delivery["message_bytes"],
        "sent_at": sent_at,
        "send_count": send_count,
        "message": f"Complaint emailed to {to_email} with the sanitized photo attached.",
    }


def _google_redirect_uri(request: Request, config: GoogleOAuthConfig) -> str:
    """Resolve the OAuth callback URL, preferring explicit configuration."""
    if config.redirect_uri:
        return config.redirect_uri
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/auth/google/callback"
    return str(request.url_for("google_callback"))


@app.get("/auth/google/status", summary="Gmail Connection Status")
async def google_status(request: Request) -> dict[str, Any]:
    """Report whether Gmail OAuth is configured and whether this browser has an active session."""
    config = GoogleOAuthConfig.from_env()
    session = GMAIL_SESSIONS.get(request.cookies.get(SESSION_COOKIE_NAME))
    return {
        "configured": config is not None,
        "connected": session is not None,
        "email": session.email if session is not None else None,
    }


@app.get("/auth/google/start", summary="Initiate Gmail OAuth Authorization")
async def google_start(
    request: Request,
    report_id: Optional[str] = None,
    popup: bool = False,
) -> RedirectResponse:
    """
    Redirect the user to Google's OAuth consent and account chooser.

    Validates that Gmail OAuth is configured and the report is active before
    generating a short-lived state and redirecting to accounts.google.com.
    """
    config = GoogleOAuthConfig.from_env()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Gmail draft integration is not configured. Set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET, and GOOGLE_REDIRECT_URI."
            ),
        )

    if not report_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="report_id is required.",
        )

    record = EVIDENCE_STORE.get(report_id)
    if record is None or not record.draft:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="report expired — please submit again",
        )

    redirect_uri = _google_redirect_uri(request, config)
    state = GMAIL_STATES.create(report_id=report_id, redirect_uri=redirect_uri, popup=popup)
    auth_url = config.authorization_url(state, redirect_uri)
    return RedirectResponse(auth_url, status_code=status.HTTP_302_FOUND)


@app.get("/auth/google/callback", name="google_callback", summary="Gmail OAuth Callback & Draft Creation")
async def google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    """
    Exchange the authorization code, create the Gmail draft with the photo attached,
    and redirect the browser directly to Gmail on the created draft.
    """
    pending = GMAIL_STATES.consume(state)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OAuth state.",
        )

    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google returned error: {error}",
        )

    config = GoogleOAuthConfig.from_env()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Gmail draft integration is not configured. Set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET, and GOOGLE_REDIRECT_URI."
            ),
        )

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return an authorization code.",
        )

    report_id = pending.report_id
    record = EVIDENCE_STORE.get(report_id)
    if record is None or not record.draft:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="report expired — please submit again",
        )

    try:
        token_data = await exchange_code(config, code, pending.redirect_uri)
        userinfo = await fetch_userinfo(token_data["access_token"])
        session = GMAIL_SESSIONS.create(token_data, str(userinfo["email"]))
    except GmailOAuthError as exc:
        logger.warning("Gmail OAuth token exchange failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google authentication failed: {exc}",
        )

    draft = record.draft
    from backend.authority_router import central_grievance_email
    to_email = str(draft.get("to", "")).strip() or central_grievance_email()
    cc_list = [str(addr).strip() for addr in draft.get("cc", []) if str(addr).strip()]
    subject = str(draft.get("subject", "Civic Waste Complaint")).strip()
    body = str(draft.get("body", "")).strip()

    filename = f"civic-evidence-{report_id[:8]}.jpg"
    try:
        message = build_email_message(
            sender=session.email,
            to=to_email,
            cc=cc_list,
            subject=subject,
            body=body,
            image_bytes=record.image_bytes,
            filename=filename,
        )
        raw_message = build_raw_message(message)
        gmail_draft = await create_gmail_draft(session.access_token, raw_message)
    except Exception as exc:
        logger.warning("Gmail draft creation failed for report %s...: %s", report_id[:8], exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Gmail draft creation failed: {exc}",
        )

    draft_id = str(gmail_draft.get("id", ""))
    msg_id = ""
    if isinstance(gmail_draft.get("message"), dict):
        msg_id = str(gmail_draft["message"].get("id", ""))

    draft_url = build_draft_url(session.email, draft_id=draft_id, message_id=msg_id)
    logger.info(
        "Created Gmail draft %s for report %s... in %s. Issuing 302 to Gmail.",
        draft_id,
        report_id[:8],
        session.email,
    )

    response = RedirectResponse(url=draft_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session.session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


class UpdateDraftRequest(BaseModel):
    """Payload for editing draft subject or body before dispatch."""

    body: Optional[str] = Field(
        default=None,
        max_length=MAX_EMAIL_BODY_CHARS,
        description="Updated complaint body.",
    )
    subject: Optional[str] = Field(
        default=None,
        max_length=MAX_EMAIL_SUBJECT_CHARS,
        description="Updated complaint subject.",
    )


@app.patch(
    "/api/reports/{report_id}/draft",
    summary="Update Complaint Draft Text",
)
async def update_report_draft(
    report_id: str,
    payload: UpdateDraftRequest,
) -> dict[str, Any]:
    """Sync user edits from the complaint textarea into the retained draft."""
    record = EVIDENCE_STORE.get(report_id)
    if record is None or not record.draft:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="report expired — please submit again",
        )

    if payload.body is not None:
        record.draft["body"] = payload.body.strip()
    if payload.subject is not None:
        record.draft["subject"] = payload.subject.strip()

    return {
        "status": "updated",
        "report_id": report_id,
        "draft": record.draft,
    }


# Serve Frontend Static Assets, Privacy Policy & Single Page Interface
from pathlib import Path
from fastapi.staticfiles import StaticFiles

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"

@app.get("/privacy", response_class=HTMLResponse, summary="Application Privacy Policy")
async def get_privacy_policy() -> HTMLResponse:
    """Serve the public privacy policy page for Google OAuth verification compliance."""
    privacy_file = frontend_dir / "privacy.html"
    if privacy_file.is_file():
        return HTMLResponse(content=privacy_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Privacy Policy</h1><p>Civic Waste Reporter Privacy Policy</p>")


if frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

