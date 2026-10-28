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
import logging
import os
import re
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.authority_router import resolve_authority
from backend.circuit_breaker import CIRCUIT_BREAKER
from backend.email_dispatch import (
    EmailDeliveryError,
    SMTPConfig,
    send_complaint_email,
)
from backend.env_loader import env_bool
from backend.evidence import EVIDENCE_STORE
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
        else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
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

    subject = f"URGENT: Municipal Waste Clearance Request - Ward {ward_number} ({ward_name})"
    evidence_line = (
        f"• Photo Evidence      : {evidence_url}\n"
        if evidence_url
        else ""
    )

    body = (
        f"To,\n"
        f"The Sanitary Inspector & Medical Officer of Health (MOH)\n"
        f"{municipality} (Ward {ward_number}: {ward_name})\n\n"
        f"Subject: Formal Grievance & Municipal Sanitation Clearance Demand\n\n"
        f"Respected Municipal Officers,\n\n"
        f"{description}\n\n"
        f"Incident Inspection Details:\n"
        f"--------------------------------------------------\n"
        f"• Location / Address  : {formatted_address}\n"
        f"• GPS Coordinates     : {lat:.6f}, {lon:.6f}\n"
        f"• Google Maps Link    : {gmaps_link}\n"
        f"{evidence_line}"
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


def generate_dispatch_urls(
    to_email: str,
    cc_list: list[str],
    subject: str,
    body: str,
    whatsapp_helpline: str,
    formatted_address: str,
    ward_number: str,
    severity: str,
    lat: float,
    lon: float,
    evidence_url: Optional[str] = None,
) -> tuple[str, str, str]:
    """
    Generate pre-filled, URL-encoded dispatch URLs for mailto, Gmail Web, and WhatsApp.

    Returns:
        tuple[mailto_url, gmail_web_url, whatsapp_url]
    """
    cc_str = ",".join(cc_list)

    # 1. Pre-filled standard mailto URL
    mailto_params = []
    if cc_str:
        mailto_params.append(f"cc={urllib.parse.quote(cc_str)}")
    mailto_params.append(f"subject={urllib.parse.quote(subject)}")
    mailto_params.append(f"body={urllib.parse.quote(body)}")
    mailto_url = f"mailto:{to_email}?{'&'.join(mailto_params)}"

    # 2. Gmail Web Compose direct URL
    gmail_params = [
        "view=cm",
        "fs=1",
        f"to={urllib.parse.quote(to_email)}",
    ]
    if cc_str:
        gmail_params.append(f"cc={urllib.parse.quote(cc_str)}")
    gmail_params.append(f"su={urllib.parse.quote(subject)}")
    gmail_params.append(f"body={urllib.parse.quote(body)}")
    gmail_web_url = f"https://mail.google.com/mail/?{'&'.join(gmail_params)}"

    # 3. WhatsApp Direct Helpline URL
    clean_phone = re.sub(r"\D", "", str(whatsapp_helpline)) or "919220592205"
    wa_evidence_line = f"Photo evidence: {evidence_url}\n" if evidence_url else ""
    wa_message = (
        f"CIVIC GRIEVANCE NOTICE\n"
        f"Location: {formatted_address} (Ward {ward_number})\n"
        f"Severity: {severity}\n"
        f"Map: https://www.google.com/maps?q={lat},{lon}\n"
        f"{wa_evidence_line}"
        f"Urgent municipal sanitation clearance required."
    )
    whatsapp_url = f"https://wa.me/{clean_phone}?text={urllib.parse.quote(wa_message)}"

    return mailto_url, gmail_web_url, whatsapp_url


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
    - Step G: Generate pre-filled mailto, Gmail Web, and WhatsApp dispatch URLs.
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

    # Step G: Generate Dispatch URLs (mailto, Gmail Web, WhatsApp)
    formatted_addr = (
        authority.get("formatted_address")
        or authority.get("resolved_address")
        or f"Lat: {lat:.6f}, Lon: {lon:.6f}"
    )
    mailto_url, gmail_web_url, whatsapp_url = generate_dispatch_urls(
        to_email=to_email,
        cc_list=cc_list,
        subject=subject,
        body=body,
        whatsapp_helpline=authority.get("whatsapp_helpline", "919220592205"),
        formatted_address=formatted_addr,
        ward_number=authority.get("ward_number", "Unmapped"),
        severity=str(vlm_data.get("severity", "medium")).capitalize(),
        lat=lat,
        lon=lon,
        evidence_url=evidence_url,
    )

    draft = {
        "to": to_email,
        "cc": cc_list,
        "subject": subject,
        "body": body,
        "mailto_url": mailto_url,
        "gmail_web_url": gmail_web_url,
        "whatsapp_url": whatsapp_url,
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
                "sending, or use the mailto/Gmail/WhatsApp links instead."
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


# Serve Frontend Static Assets & Single Page Interface
from pathlib import Path
from fastapi.staticfiles import StaticFiles

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

