"""Firebase Admin ID-token verification."""

from __future__ import annotations

import threading
from typing import Any, Optional

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
        auth_time=int(decoded["auth_time"]) if decoded.get("auth_time") is not None else None,
    )


def find_password_user(email: str) -> Optional[str]:
    """Firebase uid of the account that signs in with this email and a password,
    or None. Accounts that only use Google/Apple sign-in have no password to
    reset, so they are treated as not found."""
    _ensure_initialized()
    from firebase_admin import auth

    try:
        record = auth.get_user_by_email(email)
    except auth.UserNotFoundError:
        return None
    if record.disabled:
        return None
    if not any(p.provider_id == "password" for p in record.provider_data):
        return None
    return str(record.uid)


def set_password(uid: str, new_password: str) -> None:
    """Set a new password and invalidate every existing Firebase session."""
    _ensure_initialized()
    from firebase_admin import auth

    auth.update_user(uid, password=new_password)
    auth.revoke_refresh_tokens(uid)


def delete_user(uid: str) -> bool:
    """Delete the Firebase account. Returns False if it was already gone, so a
    retried account deletion is idempotent."""
    _ensure_initialized()
    from firebase_admin import auth

    try:
        auth.delete_user(uid)
    except auth.UserNotFoundError:
        return False
    return True
