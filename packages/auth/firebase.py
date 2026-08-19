"""Firebase Admin ID-token verification."""

from __future__ import annotations

import threading
from typing import Any

from packages.config import get_settings
from packages.domain.auth import FirebaseIdentity

_lock = threading.Lock()
_initialized = False


def _ensure_initialized() -> Any:
    global _initialized
    import firebase_admin
    from firebase_admin import credentials

    if _initialized:
        return firebase_admin

    with _lock:
        if _initialized:
            return firebase_admin
        settings = get_settings().security
        if not settings.firebase_service_account_path:
            raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_PATH is not configured")
        options = {"projectId": settings.firebase_project_id} if settings.firebase_project_id else None
        credential = credentials.Certificate(settings.firebase_service_account_path)
        try:
            firebase_admin.initialize_app(credential, options=options)
        except ValueError as exc:
            if "already exists" not in str(exc).lower():
                raise
        _initialized = True
    return firebase_admin


def verify_id_token(token: str) -> FirebaseIdentity:
    firebase_admin = _ensure_initialized()
    from firebase_admin import auth

    decoded: dict[str, Any] = auth.verify_id_token(token, check_revoked=False)
    settings = get_settings().security
    if settings.firebase_project_id and decoded.get("aud") != settings.firebase_project_id:
        raise ValueError("Firebase token audience is invalid")
    if settings.firebase_require_email_verified and not decoded.get("email_verified", False):
        raise ValueError("email verification is required")

    return FirebaseIdentity(
        uid=str(decoded["uid"]),
        email=decoded.get("email"),
        email_verified=bool(decoded.get("email_verified", False)),
        display_name=decoded.get("name"),
    )
