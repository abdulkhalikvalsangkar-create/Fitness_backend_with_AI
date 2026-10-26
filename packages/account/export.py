"""Health-data export.

The export is built by a background job (`data_export`), written to a private
directory as one JSON file, and delivered through a signed, expiring download
link. The same link is emailed when SMTP is configured; it is always present in
the job result so the app can show it regardless.

Links are JWTs signed with the server secret and a dedicated audience, so an
access token can never be replayed as a download link or the reverse.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import tempfile
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

import jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from packages.account.deletion import _safe_segment
from packages.config import get_settings

logger = logging.getLogger(__name__)

EXPORT_AUDIENCE = "fitness-export"
EXPORT_VERSION = 1

ProgressFn = Callable[[str, int], None]


# -- link signing -----------------------------------------------------------


def sign_link(user_id: str, job_id: str, expires_at: datetime) -> str:
    secret = get_settings().security.jwt_secret
    if not secret:
        raise RuntimeError("JWT_SECRET is not configured")
    return jwt.encode(
        {"sub": user_id, "jid": job_id, "aud": EXPORT_AUDIENCE, "exp": expires_at},
        secret,
        algorithm=get_settings().security.jwt_algorithm,
    )


def verify_link(token: str) -> tuple[str, str]:
    """Returns (user_id, job_id) or raises jwt.InvalidTokenError (incl. expiry)."""
    settings = get_settings().security
    if not settings.jwt_secret:
        raise jwt.InvalidTokenError("export links are not configured")
    claims = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        audience=EXPORT_AUDIENCE,
        options={"require": ["exp", "sub", "jid"]},
    )
    return str(claims["sub"]), str(claims["jid"])


def export_path(user_id: str, job_id: str) -> Path:
    return Path(get_settings().export.dir) / _safe_segment(user_id) / f"{_safe_segment(job_id)}.json"


def download_url(token: str) -> str:
    base = get_settings().export.public_base_url.rstrip("/")
    return f"{base}/export/download?token={quote(token)}"


# -- data collection --------------------------------------------------------


def _rows(session: Session, sql: str, user_id: str) -> list[dict[str, Any]]:
    result = session.execute(text(sql), {"uid": user_id}).mappings().all()
    return [dict(row) for row in result]


def collect(session: Session, user_id: str, progress: Optional[ProgressFn] = None) -> dict[str, Any]:
    """Everything stored about the user, in tables the user can recognise."""
    report = progress or (lambda stage, pct: None)

    sections: list[tuple[str, int, str]] = [
        ("account", 10, "SELECT email, locale, jurisdiction, created_at FROM app_user WHERE user_id = :uid"),
        (
            "profile",
            20,
            "SELECT display_name, date_of_birth, age_band, sex, height_cm, weight_kg, "
            "pregnancy_status, goals, preferences, updated_at FROM user_profile WHERE user_id = :uid",
        ),
        ("consent", 25, "SELECT scope, granted, granted_at, revoked_at FROM consent_scope WHERE user_id = :uid"),
        (
            "metrics",
            40,
            "SELECT metric, measured_on, value, unit, source FROM health_metric "
            "WHERE user_id = :uid ORDER BY measured_on, metric",
        ),
        (
            "nutrition",
            50,
            "SELECT * FROM nutrition_day WHERE user_id = :uid ORDER BY consumed_on",
        ),
        (
            "activities",
            60,
            "SELECT * FROM activity_session WHERE user_id = :uid ORDER BY started_at",
        ),
        (
            "medical",
            70,
            "SELECT report_date, bmi, systolic, diastolic, hba1c, lipids, labs, flags, "
            "conditions, allergies, medications FROM medical_report "
            "WHERE user_id = :uid ORDER BY report_date",
        ),
        (
            "memories",
            78,
            "SELECT kind, value, confidence, created_at FROM user_memory "
            "WHERE user_id = :uid AND active = 1 ORDER BY created_at",
        ),
        (
            "reminders",
            84,
            "SELECT reminder_id, category, enabled, title, subtitle, remind_time, remind_date, "
            "days, supplement_id, pet_id, item FROM reminder WHERE user_id = :uid",
        ),
        (
            "chat_history",
            92,
            "SELECT session_id, role, content, created_at FROM conversation_turn "
            "WHERE user_id = :uid ORDER BY created_at",
        ),
        (
            "attachments",
            96,
            "SELECT blob_id, mime_type, size_bytes, created_at FROM blob_object "
            "WHERE user_id = :uid AND kind = 'upload' ORDER BY created_at",
        ),
    ]

    data: dict[str, Any] = {}
    for key, pct, sql in sections:
        report(f"reading {key}", pct)
        data[key] = _rows(session, sql, user_id)

    # Single-row sections read better as objects than one-element lists.
    for key in ("account", "profile"):
        data[key] = data[key][0] if data[key] else None

    for section in ("profile", "medical", "reminders"):
        _decode_json_columns(data[section])

    return data


def _decode_json_columns(value: Any) -> None:
    rows = value if isinstance(value, list) else [value]
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, item in list(row.items()):
            if isinstance(item, (str, bytes)) and key in {
                "goals", "preferences", "lipids", "labs", "flags",
                "conditions", "allergies", "medications", "days",
            }:
                try:
                    row[key] = json.loads(item)
                except (TypeError, ValueError):
                    pass


def write_export(user_id: str, job_id: str, data: dict[str, Any]) -> Path:
    """Write atomically so a download never serves a half-written file."""
    document = {
        "export_version": EXPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "data": data,
    }
    path = export_path(user_id, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, default=str, ensure_ascii=False)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def link_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=get_settings().export.link_ttl_hours)


# -- email ------------------------------------------------------------------


def send_link_email(to_address: str, url: str, expires_at: datetime) -> bool:
    """Email the link. Returns False (never raises) when SMTP is unconfigured
    or sending fails — the link in the job result is the source of truth."""
    cfg = get_settings().export
    if not (cfg.smtp_host and cfg.smtp_from and to_address):
        return False

    message = EmailMessage()
    message["Subject"] = "Your health data export is ready"
    message["From"] = cfg.smtp_from
    message["To"] = to_address
    message.set_content(
        "Your health data export is ready.\n\n"
        f"Download it here: {url}\n\n"
        f"This link expires on {expires_at:%Y-%m-%d %H:%M} UTC. "
        "If you did not request this export, you can ignore this email."
    )
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as client:
            client.starttls()
            if cfg.smtp_user:
                client.login(cfg.smtp_user, cfg.smtp_password)
            client.send_message(message)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("export email to %s failed: %s", to_address, exc)
        return False


def purge_expired_files() -> int:
    """Delete export files older than the link lifetime (a file nobody can
    reach any more should not sit on disk holding health data)."""
    root = Path(get_settings().export.dir)
    if not root.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - get_settings().export.link_ttl_hours * 3600
    removed = 0
    for path in root.glob("*/*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
