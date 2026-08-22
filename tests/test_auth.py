"""Local authentication checks that do not require Firebase or MySQL."""

from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("JWT_SECRET", "test-secret-for-auth-tests-32-bytes-minimum")
os.environ.setdefault("JWT_AUDIENCE", "fitness-api")
os.environ.setdefault("JWT_ISSUER", "movenetics-api")
os.environ.setdefault("ACCESS_TOKEN_MINUTES", "15")
os.environ.setdefault("REFRESH_TOKEN_DAYS", "30")

import jwt
from pydantic import ValidationError

from apps.api.main import app
from apps.api.security import _decode_jwt
from apps.api.actions import _answer_source
from packages.auth.tokens import create_access_token, create_refresh_token, hash_refresh_token
from packages.domain.auth import FirebaseExchangeRequest
from packages.domain.enums import BlockType
from packages.domain.models import AnswerBlock, AnswerPayload
from packages.storage import migrate


class AuthTokenTests(unittest.TestCase):
    def test_access_token_contains_backend_claims(self) -> None:
        token, expires_in = create_access_token("local-user", ("user",))
        claims = jwt.decode(
            token,
            os.environ["JWT_SECRET"],
            algorithms=["HS256"],
            audience="fitness-api",
            issuer="movenetics-api",
        )
        self.assertEqual(claims["sub"], "local-user")
        self.assertEqual(claims["scopes"], ["user"])
        self.assertEqual(expires_in, 900)
        self.assertIn("jti", claims)

    def test_expired_access_token_is_rejected(self) -> None:
        token = jwt.encode(
            {
                "sub": "local-user",
                "aud": "fitness-api",
                "iss": "movenetics-api",
                "iat": int(time.time()) - 120,
                "exp": int(time.time()) - 60,
            },
            os.environ["JWT_SECRET"],
            algorithm="HS256",
        )
        with self.assertRaises(Exception):
            _decode_jwt(token)

    def test_refresh_token_is_opaque_and_hash_is_stable(self) -> None:
        token = create_refresh_token()
        self.assertGreaterEqual(len(token), 64)
        self.assertNotEqual(token, create_refresh_token())
        self.assertEqual(len(hash_refresh_token(token)), 64)
        self.assertEqual(hash_refresh_token(token), hash_refresh_token(token))

    def test_exchange_request_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            FirebaseExchangeRequest(firebase_id_token="token", user_id="forged-user")


class AuthIntegrationShapeTests(unittest.TestCase):
    def test_auth_routes_are_registered(self) -> None:
        routes = {(route.path, method) for route in app.routes for method in (route.methods or ())}
        self.assertIn(("/", "POST"), routes)
        self.assertNotIn(("/auth/exchange", "POST"), routes)

    def test_auth_session_migration_is_discovered(self) -> None:
        versions = {migration.version for migration in migrate.discover()}
        self.assertIn("003_auth_sessions", versions)

    def test_answer_source_identifies_faq_and_llm_blocks(self) -> None:
        class State:
            payload = AnswerPayload(
                blocks=[AnswerBlock(block_id="faq_1", type=BlockType.FAQ_ANSWER)]
            )

        self.assertEqual(_answer_source(State()), "faq")
        State.payload = AnswerPayload(
            blocks=[AnswerBlock(block_id="personal_1", type=BlockType.TEXT)]
        )
        self.assertEqual(_answer_source(State()), "llm")


if __name__ == "__main__":
    unittest.main()
