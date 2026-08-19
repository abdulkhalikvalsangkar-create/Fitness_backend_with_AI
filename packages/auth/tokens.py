"""Backend access JWTs and opaque refresh-token utilities."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from packages.config import get_settings


def create_access_token(user_id: str, scopes: tuple[str, ...] = (), is_admin: bool = False) -> tuple[str, int]:
    settings = get_settings().security
    if not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET is not configured")

    now = datetime.now(timezone.utc)
    lifetime = timedelta(minutes=settings.access_token_minutes)
    claims: dict[str, Any] = {
        "sub": user_id,
        "aud": settings.jwt_audience,
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + lifetime,
        "jti": str(uuid.uuid4()),
        "scopes": list(scopes),
    }
    if is_admin:
        claims["admin"] = True

    return (
        jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm),
        int(lifetime.total_seconds()),
    )


def create_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(refresh_token: str) -> str:
    return hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()


def refresh_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=get_settings().security.refresh_token_days)
