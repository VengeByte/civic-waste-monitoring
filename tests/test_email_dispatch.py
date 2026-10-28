"""
Unit tests for backend/email_dispatch.py.

Covers SMTP configuration parsing, MIME construction with the JPEG attachment,
STARTTLS/SSL transports, authentication, refused recipients, and failure wrapping.
No real network connection is made.
"""

from __future__ import annotations

import smtplib
from unittest.mock import patch

import pytest

from backend.email_dispatch import (
    EmailDeliveryError,
    SMTPConfig,
    build_email_message,
    send_complaint_email,
)

JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-payload\xff\xd9"


def _config(**overrides) -> SMTPConfig:
    values = {
        "host": "smtp.test",
        "port": 587,
        "username": "mailer",
        "password": "secret",
        "sender": "civic@test",
        "use_tls": True,
        "use_ssl": False,
        "timeout": 12.0,
    }
    values.update(overrides)
    return SMTPConfig(**values)


def _clear_smtp_env(monkeypatch) -> None:
    for name in (
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "SMTP_USE_SSL",
        "SMTP_USE_TLS",
        "SMTP_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)


# ============================================================================
# Configuration
# ============================================================================

def test_config_absent_without_host(monkeypatch):
    _clear_smtp_env(monkeypatch)
    assert SMTPConfig.from_env() is None


def test_config_defaults_to_starttls_on_587(monkeypatch):
    _clear_smtp_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_USERNAME", "bot@example.org")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")

    config = SMTPConfig.from_env()

    assert config is not None
    assert config.host == "smtp.example.org"
    assert config.port == 587
    assert config.use_tls is True
    assert config.use_ssl is False
    assert config.sender == "bot@example.org"
    assert config.timeout == 15.0


def test_config_ssl_defaults_to_465_and_disables_starttls(monkeypatch):
    _clear_smtp_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_USE_SSL", "true")

    config = SMTPConfig.from_env()

    assert config is not None
    assert config.port == 465
    assert config.use_ssl is True
    assert config.use_tls is False


def test_config_explicit_values_win(monkeypatch):
    _clear_smtp_env(monkeypatch)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USE_TLS", "0")
    monkeypatch.setenv("SMTP_FROM", "reports@example.org")
    monkeypatch.setenv("SMTP_TIMEOUT", "3.5")

    config = SMTPConfig.from_env()

    assert config is not None
    assert config.port == 2525
    assert config.use_tls is False
    assert config.sender == "reports@example.org"
    assert config.timeout == 3.5


# ============================================================================
# Message construction
# ============================================================================

def test_message_has_recipients_and_jpeg_attachment():
    message = build_email_message(
        sender="civic@test",
        to="inspector@city.gov",
        cc=["moh@city.gov", "commissioner@city.gov"],
        subject="Waste clearance",
        body="Please clear the refuse.",
        image_bytes=JPEG_BYTES,
        filename="evidence.jpg",
    )

    assert message["To"] == "inspector@city.gov"
    assert message["Cc"] == "moh@city.gov, commissioner@city.gov"
    assert message["Subject"] == "Waste clearance"

    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.get_content_type() == "image/jpeg"
    assert attachment.get_filename() == "evidence.jpg"
    assert attachment.get_payload(decode=True) == JPEG_BYTES

    plain_body = message.get_body(preferencelist=("plain",))
    assert plain_body is not None
    assert "Please clear the refuse." in plain_body.get_content()


def test_message_rejects_header_injection():
    with pytest.raises(ValueError):
        build_email_message(
            sender="civic@test",
            to="inspector@city.gov",
            cc=[],
            subject="Legit\nBcc: attacker@evil.test",
            body="Body",
            image_bytes=JPEG_BYTES,
        )


def test_message_requires_evidence_bytes():
    with pytest.raises(ValueError):
        build_email_message(
            sender="civic@test",
            to="inspector@city.gov",
            cc=[],
            subject="Subject",
            body="Body",
            image_bytes=b"",
        )


# ============================================================================
# SMTP transmission
# ============================================================================

def test_send_uses_starttls_login_and_returns_summary():
    with patch("backend.email_dispatch.smtplib.SMTP") as smtp_cls:
        server = smtp_cls.return_value
        server.send_message.return_value = {}

        result = send_complaint_email(
            _config(),
            to="inspector@city.gov",
            cc=["moh@city.gov"],
            subject="Subject",
            body="Body",
            image_bytes=JPEG_BYTES,
        )

    smtp_cls.assert_called_once_with("smtp.test", 587, timeout=12.0)
    server.ehlo.assert_called()
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("mailer", "secret")
    server.send_message.assert_called_once()

    assert result["recipients"] == ["inspector@city.gov", "moh@city.gov"]
    assert result["attachment_bytes"] == len(JPEG_BYTES)
    assert result["used_starttls"] is True
    assert result["used_ssl"] is False


def test_send_uses_ssl_and_skips_starttls():
    with patch("backend.email_dispatch.smtplib.SMTP_SSL") as ssl_cls:
        server = ssl_cls.return_value
        server.send_message.return_value = {}

        send_complaint_email(
            _config(use_ssl=True, use_tls=False, port=465),
            to="inspector@city.gov",
            cc=[],
            subject="Subject",
            body="Body",
            image_bytes=JPEG_BYTES,
        )

    ssl_cls.assert_called_once_with("smtp.test", 465, timeout=12.0)
    server.starttls.assert_not_called()
    server.login.assert_called_once()


def test_send_without_username_skips_login():
    with patch("backend.email_dispatch.smtplib.SMTP") as smtp_cls:
        server = smtp_cls.return_value
        server.send_message.return_value = {}

        send_complaint_email(
            _config(username="", password=""),
            to="inspector@city.gov",
            cc=[],
            subject="Subject",
            body="Body",
            image_bytes=JPEG_BYTES,
        )

    server.login.assert_not_called()


def test_send_raises_when_recipient_refused():
    with patch("backend.email_dispatch.smtplib.SMTP") as smtp_cls:
        server = smtp_cls.return_value
        server.send_message.return_value = {"bad@city.gov": (550, b"unknown user")}

        with pytest.raises(EmailDeliveryError, match="refused"):
            send_complaint_email(
                _config(),
                to="bad@city.gov",
                cc=[],
                subject="Subject",
                body="Body",
                image_bytes=JPEG_BYTES,
            )


def test_send_wraps_smtp_exceptions():
    with patch("backend.email_dispatch.smtplib.SMTP") as smtp_cls:
        server = smtp_cls.return_value
        server.send_message.side_effect = smtplib.SMTPServerDisconnected("connection dropped")

        with pytest.raises(EmailDeliveryError, match="SMTPServerDisconnected"):
            send_complaint_email(
                _config(),
                to="inspector@city.gov",
                cc=[],
                subject="Subject",
                body="Body",
                image_bytes=JPEG_BYTES,
            )


def test_send_wraps_connection_errors():
    with patch("backend.email_dispatch.smtplib.SMTP", side_effect=OSError("no route to host")):
        with pytest.raises(EmailDeliveryError, match="OSError"):
            send_complaint_email(
                _config(),
                to="inspector@city.gov",
                cc=[],
                subject="Subject",
                body="Body",
                image_bytes=JPEG_BYTES,
            )
