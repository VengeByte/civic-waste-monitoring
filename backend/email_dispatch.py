"""
Server-Side Email Delivery for Compiled Civic Complaints.

The backend sends the complaint directly to the resolved municipal officers with the
sanitized (PII-blurred) JPEG attached when SMTP delivery is configured.

Delivery is intentionally optional. Without SMTP configuration the API keeps working
and reports ``503`` for server-side sends, while the Gmail draft flow and short-lived
evidence URL remain available.

Environment variables:
    SMTP_HOST       SMTP server hostname. Presence enables server-side delivery.
    SMTP_PORT       SMTP port (default 465 when SSL, otherwise 587).
    SMTP_USERNAME   Optional SMTP username for authentication.
    SMTP_PASSWORD   Optional SMTP password.
    SMTP_FROM       Sender address (default: SMTP_USERNAME, else civic-reports@localhost).
    SMTP_USE_SSL    "1"/"true" for implicit TLS (SMTPS). Default: false.
    SMTP_USE_TLS    "1"/"true" for STARTTLS. Default: true unless SSL is enabled.
    SMTP_TIMEOUT    Socket timeout in seconds (default: 15).
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Optional

from backend.env_loader import load_env_file

logger = logging.getLogger("backend.email_dispatch")
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [email_dispatch]: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class EmailDeliveryError(RuntimeError):
    """Raised when SMTP delivery fails or the message is malformed."""


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment variable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, minimum: int) -> int:
    """Parse an integer environment variable with a lower bound."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class SMTPConfig:
    """Resolved SMTP transport configuration."""

    host: str
    port: int
    username: str
    password: str
    sender: str
    use_tls: bool
    use_ssl: bool
    timeout: float

    @classmethod
    def from_env(cls) -> Optional["SMTPConfig"]:
        """
        Build configuration from the process environment.

        Returns:
            SMTPConfig when ``SMTP_HOST`` is set, otherwise None.
        """
        load_env_file()
        host = os.getenv("SMTP_HOST", "").strip()
        if not host:
            return None

        use_ssl = _env_bool("SMTP_USE_SSL", False)
        port = _env_int("SMTP_PORT", 465 if use_ssl else 587, 1)
        use_tls = _env_bool("SMTP_USE_TLS", not use_ssl)
        if use_ssl:
            # Implicit TLS and STARTTLS are mutually exclusive.
            use_tls = False

        username = os.getenv("SMTP_USERNAME", "").strip()
        password = os.getenv("SMTP_PASSWORD", "")
        sender = os.getenv("SMTP_FROM", "").strip() or username or "civic-reports@localhost"

        try:
            timeout = max(1.0, float(os.getenv("SMTP_TIMEOUT", "15")))
        except ValueError:
            timeout = 15.0

        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            sender=sender,
            use_tls=use_tls,
            use_ssl=use_ssl,
            timeout=timeout,
        )


def _assert_single_line(name: str, value: str) -> None:
    """Reject CR/LF in values used as mail headers to prevent header injection."""
    if "\r" in str(value) or "\n" in str(value):
        raise ValueError(f"{name} must not contain newline characters.")


def build_email_message(
    *,
    sender: str,
    to: str,
    cc: list[str],
    subject: str,
    body: str,
    image_bytes: bytes,
    filename: str = "civic-evidence.jpg",
) -> EmailMessage:
    """
    Build a text complaint email with the sanitized photograph attached.

    Args:
        sender: From address.
        to: Primary recipient address.
        cc: CC recipient addresses.
        subject: Subject line (single line).
        body: Plain-text complaint body.
        image_bytes: Sanitized JPEG bytes to attach.
        filename: Attachment filename shown to the officer.

    Returns:
        A fully populated EmailMessage.

    Raises:
        ValueError: If a header value contains CR/LF or the image is empty.
    """
    if not image_bytes:
        raise ValueError("Cannot build a complaint email without evidence bytes.")

    for label, value in (("sender", sender), ("to", to), ("subject", subject)):
        _assert_single_line(label, value)
    for address in cc:
        _assert_single_line("cc", address)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(
        image_bytes,
        maintype="image",
        subtype="jpeg",
        filename=filename,
    )
    return message


def send_complaint_email(
    config: SMTPConfig,
    *,
    to: str,
    cc: list[str],
    subject: str,
    body: str,
    image_bytes: bytes,
    filename: str = "civic-evidence.jpg",
) -> dict[str, Any]:
    """
    Send the complaint email synchronously through SMTP with the photo attached.

    Callers on the event loop should invoke this via ``asyncio.to_thread``.

    Args:
        config: Resolved SMTP transport configuration.
        to: Primary recipient address.
        cc: CC recipient addresses.
        subject: Subject line.
        body: Plain-text complaint body.
        image_bytes: Sanitized JPEG bytes to attach.
        filename: Attachment filename.

    Returns:
        Delivery summary including recipients and attached byte count.

    Raises:
        EmailDeliveryError: If the SMTP conversation fails or a recipient is refused.
    """
    message = build_email_message(
        sender=config.sender,
        to=to,
        cc=cc,
        subject=subject,
        body=body,
        image_bytes=image_bytes,
        filename=filename,
    )

    server: Optional[smtplib.SMTP] = None
    refused: dict[str, tuple[int, bytes]] = {}

    try:
        if config.use_ssl:
            server = smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout)
        else:
            server = smtplib.SMTP(config.host, config.port, timeout=config.timeout)

        with server:
            server.ehlo()
            if config.use_tls:
                server.starttls()
                server.ehlo()
            if config.username:
                server.login(config.username, config.password)
            refused = server.send_message(message)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        logger.error("SMTP delivery to %s failed: %s: %s", to, type(exc).__name__, exc)
        raise EmailDeliveryError(f"{type(exc).__name__}: {exc}") from exc

    if refused:
        recipients = ", ".join(sorted(refused))
        logger.warning("SMTP server refused recipients for %s: %s", to, recipients)
        raise EmailDeliveryError(f"SMTP server refused recipient(s): {recipients}")

    logger.info("Complaint email delivered to %s (cc: %d) with %d attachment bytes.", to, len(cc), len(image_bytes))
    return {
        "recipients": [to, *cc],
        "attachment_bytes": len(image_bytes),
        "message_bytes": len(message.as_bytes()),
        "used_starttls": config.use_tls,
        "used_ssl": config.use_ssl,
    }
