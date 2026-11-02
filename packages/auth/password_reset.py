"""Forgot-password flow: email a 5-digit code, verify it, set a new password.

    request_code  -> emails a code (also used for "resend")
    verify_code   -> exchanges the code for a short-lived reset token
    reset_password-> exchanges the token + new password for a Firebase update

Design rules:

* **No account enumeration.** `request_code` answers identically whether or not
  the email belongs to an account (and whether or not it can reset a password),
  and `verify_code` fails with one generic message for every kind of mismatch.
  Throttling is keyed on the email hash, not on the account.
* **Codes are never stored.** Only an HMAC of the code, bound to the email, is
  kept; comparison is constant-time.
* **Online guessing is capped.** A 5-digit code has 100,000 values, so a code
  dies after `max_attempts` wrong guesses and after `otp_ttl_seconds`.
* **The reset token is single-use.** It is a signed JWT with its own audience,
  and it is consumed atomically before Firebase is touched.

SQL is deliberately portable (times are passed in from Python rather than read
from the database) so the whole flow is tested against SQLite.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from packages.common import mailer
from packages.config import get_settings

logger = logging.getLogger(__name__)

OTP_LENGTH = 5
RESET_AUDIENCE = "fitness-password-reset"
PASSWORD_MIN = 8
PASSWORD_MAX = 128

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GENERIC_CODE_ERROR = "invalid or expired code"


class ResetError(Exception):
    """A failure that is safe to return to the client as-is."""

    def __init__(self, status: int, message: str, retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


def normalise_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise ResetError(400, "a valid email address is required")
    return email


def email_hash(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def validate_password(password: Any) -> str:
    if not isinstance(password, str):
        raise ResetError(400, "new_password is required")
    if len(password) < PASSWORD_MIN:
        raise ResetError(400, f"password must be at least {PASSWORD_MIN} characters")
    if len(password) > PASSWORD_MAX:
        raise ResetError(400, f"password must be at most {PASSWORD_MAX} characters")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ResetError(400, "password must contain at least one letter and one number")
    return password


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def _as_datetime(value: Any) -> Optional[datetime]:
    """Rows come back as datetimes from MySQL but as strings from SQLite."""
    if value is None or isinstance(value, datetime):
        return _naive_utc(value)
    return _naive_utc(datetime.fromisoformat(str(value)))


class PasswordResetService:
    def __init__(self, session: Session, now: Optional[datetime] = None) -> None:
        self.session = session
        self._fixed_now = now
        self.cfg = get_settings().password_reset

    # -- helpers ----------------------------------------------------------

    def _now(self) -> datetime:
        return _naive_utc(self._fixed_now or datetime.now(timezone.utc))  # type: ignore[return-value]

    def _otp_hash(self, hashed_email: str, otp: str) -> str:
        secret = get_settings().security.jwt_secret
        if not secret:
            raise RuntimeError("JWT_SECRET is not configured")
        return hmac.new(
            secret.encode("utf-8"), f"{hashed_email}:{otp}".encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _get(self, hashed_email: str) -> Optional[dict[str, Any]]:
        row = self.session.execute(
            text("SELECT * FROM password_reset_otp WHERE email_hash = :h"), {"h": hashed_email}
        ).mappings().first()
        if not row:
            return None
        data = dict(row)
        for key in ("expires_at", "window_started_at", "last_sent_at", "verified_at", "reset_used_at"):
            data[key] = _as_datetime(data.get(key))
        return data

    # -- 1. request / resend ----------------------------------------------

    def request_code(self, raw_email: Any) -> dict[str, Any]:
        """Create and email a fresh code. Calling it again is "resend": the old
        code stops working. Returns the same shape for every email."""
        email = normalise_email(raw_email)

        if not mailer.configured() and get_settings().env != "development":
            raise ResetError(503, "email delivery is not configured")

        hashed = email_hash(email)
        now = self._now()
        row = self._get(hashed)

        sends = 0
        window_started = now
        if row:
            since_last = (now - row["last_sent_at"]).total_seconds()
            if since_last < self.cfg.resend_cooldown_seconds:
                wait = int(self.cfg.resend_cooldown_seconds - since_last) + 1
                raise ResetError(429, "please wait before requesting another code", retry_after=wait)

            if (now - row["window_started_at"]) < timedelta(hours=1):
                sends = int(row["sends_in_window"])
                window_started = row["window_started_at"]
                if sends >= self.cfg.max_sends_per_hour:
                    wait = int((window_started + timedelta(hours=1) - now).total_seconds()) + 1
                    raise ResetError(429, "too many code requests; try again later", retry_after=wait)

        otp = f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"
        values = {
            "h": hashed,
            "otp": self._otp_hash(hashed, otp),
            "exp": now + timedelta(seconds=self.cfg.otp_ttl_seconds),
            "sends": sends + 1,
            "win": window_started,
            "last": now,
        }
        updated = self.session.execute(
            text(
                "UPDATE password_reset_otp SET otp_hash = :otp, expires_at = :exp, attempts = 0, "
                "sends_in_window = :sends, window_started_at = :win, last_sent_at = :last, "
                "verified_at = NULL, reset_jti = NULL, reset_used_at = NULL WHERE email_hash = :h"
            ),
            values,
        ).rowcount
        if not updated:
            self.session.execute(
                text(
                    "INSERT INTO password_reset_otp (email_hash, otp_hash, expires_at, attempts, "
                    "sends_in_window, window_started_at, last_sent_at) "
                    "VALUES (:h, :otp, :exp, 0, :sends, :win, :last)"
                ),
                values,
            )

        self._deliver(email, otp)
        return {
            "sent": True,
            "otp_length": OTP_LENGTH,
            "expires_in": self.cfg.otp_ttl_seconds,
            "resend_after": self.cfg.resend_cooldown_seconds,
        }

    def _deliver(self, email: str, otp: str) -> None:
        """Email the code if — and only if — a password account exists.

        Lookup and send happen after the code is stored and failures are
        swallowed, so the response is identical for registered, unregistered
        and social-login-only addresses.
        """
        from packages.auth import firebase

        try:
            uid = firebase.find_password_user(email)
        except Exception:
            logger.exception("password reset: Firebase lookup failed")
            return
        if uid is None:
            return

        minutes = max(1, self.cfg.otp_ttl_seconds // 60)
        sent = mailer.send_email(
            email,
            "Your password reset code",
            f"Your password reset code is {otp}.\n\n"
            f"It expires in {minutes} minutes. If you did not ask to reset your password, "
            "you can ignore this email — your password has not changed.",
        )
        if not sent:
            if get_settings().env == "development":
                # Local development without SMTP: surface the code in the log.
                logger.warning("password reset code for %s (dev only, email not sent): %s", email, otp)
            else:
                logger.error("password reset email could not be sent")

    # -- 2. verify --------------------------------------------------------

    def verify_code(self, raw_email: Any, raw_otp: Any) -> dict[str, Any]:
        email = normalise_email(raw_email)
        otp = str(raw_otp or "").strip()
        if not re.fullmatch(rf"\d{{{OTP_LENGTH}}}", otp):
            raise ResetError(400, _GENERIC_CODE_ERROR)

        hashed = email_hash(email)
        now = self._now()
        row = self._get(hashed)

        if row is None or row["verified_at"] is not None or row["expires_at"] <= now:
            raise ResetError(400, _GENERIC_CODE_ERROR)
        if int(row["attempts"]) >= self.cfg.max_attempts:
            raise ResetError(429, "too many incorrect attempts; request a new code")

        if not hmac.compare_digest(row["otp_hash"], self._otp_hash(hashed, otp)):
            attempts = int(row["attempts"]) + 1
            # The last allowed guess also retires the code outright.
            self.session.execute(
                text(
                    "UPDATE password_reset_otp SET attempts = :a, "
                    "expires_at = CASE WHEN :a >= :max THEN :now ELSE expires_at END "
                    "WHERE email_hash = :h"
                ),
                {"a": attempts, "max": self.cfg.max_attempts, "now": now, "h": hashed},
            )
            if attempts >= self.cfg.max_attempts:
                raise ResetError(429, "too many incorrect attempts; request a new code")
            raise ResetError(400, _GENERIC_CODE_ERROR)

        jti = uuid.uuid4().hex
        claimed = self.session.execute(
            text(
                "UPDATE password_reset_otp SET verified_at = :now, reset_jti = :jti "
                "WHERE email_hash = :h AND verified_at IS NULL AND expires_at > :now"
            ),
            {"now": now, "jti": jti, "h": hashed},
        ).rowcount
        if not claimed:  # lost a race with a concurrent verify of the same code
            raise ResetError(400, _GENERIC_CODE_ERROR)

        secret = get_settings().security.jwt_secret
        token = jwt.encode(
            {
                "email": email,
                "jti": jti,
                "aud": RESET_AUDIENCE,
                "exp": datetime.now(timezone.utc) + timedelta(seconds=self.cfg.token_ttl_seconds),
            },
            secret,
            algorithm=get_settings().security.jwt_algorithm,
        )
        return {"verified": True, "reset_token": token, "expires_in": self.cfg.token_ttl_seconds}

    # -- 3. set the new password -------------------------------------------

    def reset_password(self, reset_token: Any, new_password: Any) -> dict[str, Any]:
        claims = self._decode_token(reset_token)
        password = validate_password(new_password)  # before consuming: the user may retry

        email, jti = str(claims["email"]), str(claims["jti"])
        hashed = email_hash(email)
        now = self._now()

        consumed = self.session.execute(
            text(
                "UPDATE password_reset_otp SET reset_used_at = :now "
                "WHERE email_hash = :h AND reset_jti = :jti "
                "AND verified_at IS NOT NULL AND reset_used_at IS NULL"
            ),
            {"now": now, "h": hashed, "jti": jti},
        ).rowcount
        if not consumed:
            raise ResetError(400, "this reset link has already been used or is no longer valid")

        from packages.auth import firebase

        try:
            uid = firebase.find_password_user(email)
        except Exception as exc:
            self._release(hashed)
            logger.exception("password reset: Firebase lookup failed")
            raise ResetError(502, "could not reach the sign-in service; please try again") from exc
        if uid is None:
            raise ResetError(400, "this reset link has already been used or is no longer valid")

        try:
            firebase.set_password(uid, password)
        except Exception as exc:
            # Give the user their one use back so they can retry.
            self._release(hashed)
            logger.exception("password reset: Firebase update failed")
            raise ResetError(502, "could not update the password; please try again") from exc

        self._revoke_backend_sessions(uid)
        self.session.execute(text("DELETE FROM password_reset_otp WHERE email_hash = :h"), {"h": hashed})
        return {"reset": True}

    def _decode_token(self, token: Any) -> dict[str, Any]:
        if not isinstance(token, str) or not token:
            raise ResetError(400, "reset_token is required")
        settings = get_settings().security
        try:
            return jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
                audience=RESET_AUDIENCE,
                options={"require": ["exp", "email", "jti"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise ResetError(400, "the reset session has expired; start again") from exc
        except jwt.InvalidTokenError as exc:
            raise ResetError(400, "invalid reset token") from exc

    def _release(self, hashed_email: str) -> None:
        self.session.execute(
            text("UPDATE password_reset_otp SET reset_used_at = NULL WHERE email_hash = :h"),
            {"h": hashed_email},
        )

    def _revoke_backend_sessions(self, firebase_uid: str) -> None:
        """Sign the user out of this API on every device."""
        from packages.storage.repositories.auth_sessions import AuthSessionRepository
        from packages.storage.repositories.users import UserRepository

        user = UserRepository(self.session).get_by_external_id(firebase_uid)
        if user:
            AuthSessionRepository(self.session).revoke_user(str(user["user_id"]))
