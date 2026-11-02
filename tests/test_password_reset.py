"""Forgot-password OTP flow, run against an in-memory SQLite database.

Firebase and email are replaced with fakes; everything else — hashing, expiry,
attempt limits, resend cooldown, single-use tokens — is the real code.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("JWT_SECRET", "test-secret-for-password-reset-32-bytes!")

import jwt
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.api import app_actions
from apps.api.actions import action_names
from apps.api.main import PASSWORD_RESET_ACTIONS, PUBLIC_ACTIONS
from apps.api.security import Principal
from packages.auth import password_reset as pr
from packages.auth.password_reset import PasswordResetService, ResetError

DDL = """
CREATE TABLE password_reset_otp (
  email_hash TEXT PRIMARY KEY, otp_hash TEXT NOT NULL, expires_at TIMESTAMP NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, sends_in_window INTEGER NOT NULL DEFAULT 1,
  window_started_at TIMESTAMP NOT NULL, last_sent_at TIMESTAMP NOT NULL,
  verified_at TIMESTAMP NULL, reset_jti TEXT NULL, reset_used_at TIMESTAMP NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

EMAIL = "Jane.Doe@Example.com"


class FakeFirebase:
    """Stands in for packages.auth.firebase."""

    def __init__(self) -> None:
        self.accounts = {"jane.doe@example.com": "uid-1"}
        self.passwords: dict[str, str] = {}
        self.fail_update = False

    def find_password_user(self, email: str):
        return self.accounts.get(email)

    def set_password(self, uid: str, password: str) -> None:
        if self.fail_update:
            raise RuntimeError("firebase down")
        self.passwords[uid] = password


class ResetFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text(DDL))
        self.session = sessionmaker(bind=engine)()
        self.addCleanup(self.session.close)

        self.firebase = FakeFirebase()
        self.sent: list[tuple[str, str, str]] = []

        def fake_send(to: str, subject: str, body: str) -> bool:
            self.sent.append((to, subject, body))
            return True

        self.revoked: list[str] = []
        patches = [
            mock.patch("packages.auth.firebase.find_password_user", self.firebase.find_password_user),
            mock.patch("packages.auth.firebase.set_password", self.firebase.set_password),
            mock.patch.object(pr.mailer, "send_email", fake_send),
            mock.patch.object(pr.mailer, "configured", return_value=True),
            mock.patch.object(
                PasswordResetService,
                "_revoke_backend_sessions",
                lambda _service, uid: self.revoked.append(uid),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    # -- helpers ----------------------------------------------------------

    def svc(self, minutes: float = 0) -> PasswordResetService:
        return PasswordResetService(
            self.session, now=datetime.now(timezone.utc) + timedelta(minutes=minutes)
        )

    def last_code(self) -> str:
        import re

        return re.search(r"code is (\d{5})", self.sent[-1][2]).group(1)

    def issue(self, minutes: float = 0) -> str:
        self.svc(minutes).request_code(EMAIL)
        return self.last_code()

    def expire_cooldown(self) -> None:
        self.session.execute(
            text("UPDATE password_reset_otp SET last_sent_at = :t"),
            {"t": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)},
        )

    # -- request ----------------------------------------------------------

    def test_request_emails_a_five_digit_code(self) -> None:
        result = self.svc().request_code(EMAIL)
        self.assertEqual(result["otp_length"], 5)
        self.assertEqual(result["expires_in"], 600)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "jane.doe@example.com")
        self.assertRegex(self.last_code(), r"^\d{5}$")

    def test_unknown_email_gets_the_same_response_and_no_email(self) -> None:
        known = self.svc().request_code("jane.doe@example.com")
        unknown = self.svc().request_code("nobody@example.com")
        self.assertEqual(known, unknown)
        self.assertEqual(len(self.sent), 1)  # only the real account was emailed

    def test_code_is_not_stored_in_plain_text(self) -> None:
        code = self.issue()
        stored = self.session.execute(text("SELECT otp_hash FROM password_reset_otp")).scalar()
        self.assertNotIn(code, stored)
        self.assertEqual(len(stored), 64)

    def test_malformed_email_is_rejected(self) -> None:
        for bad in ("", "nope", "a@b", None, "x" * 300 + "@a.com"):
            with self.assertRaises(ResetError, msg=repr(bad)) as ctx:
                self.svc().request_code(bad)
            self.assertEqual(ctx.exception.status, 400)

    def test_resend_is_throttled_by_cooldown(self) -> None:
        self.issue()
        with self.assertRaises(ResetError) as ctx:
            self.svc().request_code(EMAIL)
        self.assertEqual(ctx.exception.status, 429)
        self.assertGreater(ctx.exception.retry_after, 0)

    def test_cooldown_applies_equally_to_unknown_emails(self) -> None:
        self.svc().request_code("nobody@example.com")
        with self.assertRaises(ResetError) as ctx:
            self.svc().request_code("nobody@example.com")
        self.assertEqual(ctx.exception.status, 429)

    def test_resend_invalidates_the_previous_code(self) -> None:
        first = self.issue()
        second = self.issue(minutes=2)
        self.assertNotEqual(first, second)
        with self.assertRaises(ResetError):
            self.svc(minutes=2).verify_code(EMAIL, first)
        self.assertTrue(self.svc(minutes=2).verify_code(EMAIL, second)["verified"])

    def test_hourly_send_cap(self) -> None:
        for i in range(5):
            self.svc(minutes=i * 2).request_code(EMAIL)
        with self.assertRaises(ResetError) as ctx:
            self.svc(minutes=12).request_code(EMAIL)
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("too many", ctx.exception.message)
        # ...and the window rolls over after an hour.
        self.svc(minutes=75).request_code(EMAIL)

    def test_unconfigured_email_in_production_is_503(self) -> None:
        with mock.patch.object(pr.mailer, "configured", return_value=False), mock.patch.object(
            pr, "get_settings"
        ) as settings:
            settings.return_value.env = "production"
            settings.return_value.password_reset = pr.get_settings().password_reset
            with self.assertRaises(ResetError) as ctx:
                self.svc().request_code(EMAIL)
        self.assertEqual(ctx.exception.status, 503)

    # -- verify -----------------------------------------------------------

    def test_correct_code_returns_a_reset_token(self) -> None:
        code = self.issue()
        result = self.svc().verify_code(EMAIL, code)
        claims = jwt.decode(
            result["reset_token"],
            os.environ["JWT_SECRET"],
            algorithms=["HS256"],
            audience=pr.RESET_AUDIENCE,
        )
        self.assertEqual(claims["email"], "jane.doe@example.com")
        self.assertEqual(result["expires_in"], 600)

    def test_wrong_code_is_generic_400(self) -> None:
        code = self.issue()
        wrong = f"{(int(code) + 1) % 100000:05d}"
        with self.assertRaises(ResetError) as ctx:
            self.svc().verify_code(EMAIL, wrong)
        self.assertEqual((ctx.exception.status, ctx.exception.message), (400, "invalid or expired code"))

    def test_unknown_email_fails_exactly_like_a_wrong_code(self) -> None:
        with self.assertRaises(ResetError) as ctx:
            self.svc().verify_code("nobody@example.com", "12345")
        self.assertEqual((ctx.exception.status, ctx.exception.message), (400, "invalid or expired code"))

    def test_non_numeric_or_wrong_length_codes_do_not_burn_attempts(self) -> None:
        code = self.issue()
        for bad in ("abcde", "1234", "123456", "", None):
            with self.assertRaises(ResetError):
                self.svc().verify_code(EMAIL, bad)
        self.assertTrue(self.svc().verify_code(EMAIL, code)["verified"])

    def test_code_expires(self) -> None:
        code = self.issue()
        with self.assertRaises(ResetError) as ctx:
            self.svc(minutes=11).verify_code(EMAIL, code)
        self.assertEqual(ctx.exception.status, 400)

    def test_five_wrong_guesses_lock_the_code_even_for_the_right_one(self) -> None:
        code = self.issue()
        wrong = f"{(int(code) + 1) % 100000:05d}"
        statuses = []
        for _ in range(5):
            with self.assertRaises(ResetError) as ctx:
                self.svc().verify_code(EMAIL, wrong)
            statuses.append(ctx.exception.status)
        self.assertEqual(statuses, [400, 400, 400, 400, 429])
        with self.assertRaises(ResetError):
            self.svc().verify_code(EMAIL, code)

    def test_a_new_code_resets_the_attempt_counter(self) -> None:
        code = self.issue()
        wrong = f"{(int(code) + 1) % 100000:05d}"
        for _ in range(5):
            with self.assertRaises(ResetError):
                self.svc().verify_code(EMAIL, wrong)
        new_code = self.issue(minutes=2)
        self.assertTrue(self.svc(minutes=2).verify_code(EMAIL, new_code)["verified"])

    def test_code_cannot_be_verified_twice(self) -> None:
        code = self.issue()
        self.svc().verify_code(EMAIL, code)
        with self.assertRaises(ResetError):
            self.svc().verify_code(EMAIL, code)

    # -- confirm ----------------------------------------------------------

    def _token(self) -> str:
        return self.svc().verify_code(EMAIL, self.issue())["reset_token"]

    def test_full_flow_updates_firebase_and_clears_state(self) -> None:
        token = self._token()
        result = self.svc().reset_password(token, "NewPassw0rd!")
        self.assertEqual(result, {"reset": True})
        self.assertEqual(self.firebase.passwords, {"uid-1": "NewPassw0rd!"})
        self.assertEqual(self.revoked, ["uid-1"])
        self.assertEqual(self.session.execute(text("SELECT COUNT(*) FROM password_reset_otp")).scalar(), 0)

    def test_token_is_single_use(self) -> None:
        token = self._token()
        self.svc().reset_password(token, "NewPassw0rd!")
        with self.assertRaises(ResetError) as ctx:
            self.svc().reset_password(token, "AnotherPassw0rd")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.firebase.passwords["uid-1"], "NewPassw0rd!")

    def test_weak_password_is_rejected_without_burning_the_token(self) -> None:
        token = self._token()
        for weak in ("short1", "allletters", "12345678", "x" * 200 + "1", None):
            with self.assertRaises(ResetError, msg=repr(weak)) as ctx:
                self.svc().reset_password(token, weak)
            self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.svc().reset_password(token, "GoodPassw0rd"), {"reset": True})

    def test_expired_token_is_rejected(self) -> None:
        settings = pr.get_settings().security
        token = jwt.encode(
            {"email": "jane.doe@example.com", "jti": "x" * 32, "aud": pr.RESET_AUDIENCE,
             "exp": datetime.now(timezone.utc) - timedelta(seconds=5)},
            settings.jwt_secret, algorithm="HS256",
        )
        with self.assertRaises(ResetError) as ctx:
            self.svc().reset_password(token, "GoodPassw0rd")
        self.assertIn("expired", ctx.exception.message)

    def test_forged_and_wrong_audience_tokens_are_rejected(self) -> None:
        claims = {"email": "jane.doe@example.com", "jti": "x" * 32,
                  "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
        forged = jwt.encode({**claims, "aud": pr.RESET_AUDIENCE}, "wrong-secret-wrong-secret-wrong-1", algorithm="HS256")
        wrong_aud = jwt.encode({**claims, "aud": "fitness-api"}, os.environ["JWT_SECRET"], algorithm="HS256")
        for token in (forged, wrong_aud, "garbage", None, ""):
            with self.assertRaises(ResetError, msg=repr(token)):
                self.svc().reset_password(token, "GoodPassw0rd")

    def test_access_token_cannot_reset_a_password(self) -> None:
        from packages.auth.tokens import create_access_token

        access, _ = create_access_token("u1", ("user",))
        with self.assertRaises(ResetError):
            self.svc().reset_password(access, "GoodPassw0rd")

    def test_token_from_a_superseded_code_is_rejected(self) -> None:
        old_token = self._token()
        self.issue(minutes=2)  # resend replaces the whole reset state
        with self.assertRaises(ResetError):
            self.svc(minutes=2).reset_password(old_token, "GoodPassw0rd")

    def test_firebase_failure_returns_502_and_allows_retry(self) -> None:
        token = self._token()
        self.firebase.fail_update = True
        with self.assertRaises(ResetError) as ctx:
            self.svc().reset_password(token, "GoodPassw0rd")
        self.assertEqual(ctx.exception.status, 502)
        self.firebase.fail_update = False
        self.assertEqual(self.svc().reset_password(token, "GoodPassw0rd"), {"reset": True})


class ResetActionTests(unittest.TestCase):
    def test_actions_are_registered_and_public(self) -> None:
        for name in PASSWORD_RESET_ACTIONS:
            self.assertIn(name, action_names())
            self.assertIn(name, PUBLIC_ACTIONS)

    def test_failure_is_committed_before_the_http_error(self) -> None:
        """Otherwise the request rollback would erase the wrong-attempt counter."""
        session = mock.Mock()
        with mock.patch.object(
            app_actions.PasswordResetService, "verify_code", side_effect=ResetError(400, "invalid or expired code")
        ):
            with self.assertRaises(HTTPException) as ctx:
                app_actions.handle_password_reset_verify(
                    {"email": EMAIL, "otp": "00000"}, Principal(user_id=""), session
                )
        session.commit.assert_called_once()
        self.assertEqual(ctx.exception.status_code, 400)

    def test_retry_after_header_on_429(self) -> None:
        with mock.patch.object(
            app_actions.PasswordResetService, "request_code",
            side_effect=ResetError(429, "please wait", retry_after=42),
        ):
            with self.assertRaises(HTTPException) as ctx:
                app_actions.handle_password_reset_request(
                    {"email": EMAIL}, Principal(user_id=""), mock.Mock()
                )
        self.assertEqual(ctx.exception.headers, {"Retry-After": "42"})

    def test_resend_reports_its_own_action_name(self) -> None:
        with mock.patch.object(
            app_actions.PasswordResetService, "request_code", return_value={"sent": True}
        ):
            result = app_actions.handle_password_reset_resend(
                {"email": EMAIL}, Principal(user_id=""), mock.Mock()
            )
        self.assertEqual(result["action"], "auth.password_reset.resend")


if __name__ == "__main__":
    unittest.main()
