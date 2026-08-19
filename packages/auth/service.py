"""Application service for Firebase exchange and backend sessions."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from packages.auth.firebase import verify_id_token
from packages.auth.tokens import (
    create_access_token,
    create_refresh_token,
    hash_refresh_token,
    refresh_expiry,
)
from packages.config import get_settings
from packages.domain.auth import AuthenticatedUser, FirebaseIdentity, TokenResponse
from packages.storage.repositories.auth_sessions import AuthSessionRepository
from packages.storage.repositories.users import UserRepository


class AuthServiceError(Exception):
    """Safe-to-return authentication failure."""


class AuthService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.sessions = AuthSessionRepository(session)

    def exchange_firebase_token(
        self,
        firebase_id_token: str,
        device_id: Optional[str] = None,
        device_name: Optional[str] = None,
        platform: Optional[str] = None,
        app_version: Optional[str] = None,
    ) -> TokenResponse:
        try:
            identity = verify_id_token(firebase_id_token)
        except Exception as exc:
            raise AuthServiceError("Firebase authentication failed") from exc

        user = self.users.get_or_create_external_user(
            external_id=identity.uid,
            email=identity.email,
            display_name=identity.display_name,
        )
        self._require_active(user)
        result, _ = self._issue_pair(
            user,
            identity,
            device_id=device_id,
            device_name=device_name,
            platform=platform,
            app_version=app_version,
        )
        return result

    def refresh(self, refresh_token: str, device_id: Optional[str] = None) -> TokenResponse:
        token_hash = hash_refresh_token(refresh_token)
        current = self.sessions.get_active_by_hash(token_hash, for_update=True)
        if current is None:
            historical = self.sessions.get_by_hash(token_hash)
            if historical and (historical.get("revoked_at") or historical.get("replaced_by")):
                self.sessions.revoke_user(str(historical["user_id"]))
            raise AuthServiceError("invalid or expired refresh token")

        user = self.users.get(str(current["user_id"]))
        if not user:
            raise AuthServiceError("user account not found")
        self._require_active(user)
        if device_id and current.get("device_id") and device_id != current["device_id"]:
            raise AuthServiceError("refresh token does not belong to this device")

        identity = FirebaseIdentity(
            uid=str(user.get("external_id") or user["user_id"]),
            email=user.get("email"),
        )
        replacement, replacement_session_id = self._issue_pair(
            user,
            identity,
            device_id=current.get("device_id"),
            device_name=current.get("device_name"),
            platform=current.get("platform"),
            app_version=current.get("app_version"),
        )
        self.sessions.rotate(str(current["session_id"]), replacement_session_id)
        return replacement

    def logout(self, principal_user_id: str, refresh_token: Optional[str]) -> bool:
        if not refresh_token:
            return False
        row = self.sessions.get_by_hash(hash_refresh_token(refresh_token))
        if not row or str(row["user_id"]) != principal_user_id:
            return False
        return self.sessions.revoke(str(row["session_id"]))

    def logout_all(self, principal_user_id: str) -> int:
        return self.sessions.revoke_user(principal_user_id)

    def _issue_pair(
        self,
        user: dict[str, Any],
        identity: FirebaseIdentity,
        *,
        device_id: Optional[str],
        device_name: Optional[str],
        platform: Optional[str],
        app_version: Optional[str],
    ) -> tuple[TokenResponse, str]:
        scopes = ("user",)
        is_admin = False
        access_token, access_seconds = create_access_token(str(user["user_id"]), scopes, is_admin)
        refresh_token = create_refresh_token()
        session_id = str(uuid.uuid4())
        self.sessions.create(
            session_id=session_id,
            user_id=str(user["user_id"]),
            refresh_hash=hash_refresh_token(refresh_token),
            expires_at=refresh_expiry(),
            device_id=device_id,
            device_name=device_name,
            platform=platform,
            app_version=app_version,
        )
        settings = get_settings().security
        return (
            TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=access_seconds,
                refresh_expires_in=settings.refresh_token_days * 86400,
                user=AuthenticatedUser(
                    user_id=str(user["user_id"]),
                    firebase_uid=identity.uid,
                    email=identity.email,
                ),
            ),
            session_id,
        )

    @staticmethod
    def _require_active(user: dict[str, Any]) -> None:
        if str(user.get("status") or "active") != "active":
            raise AuthServiceError("user account is not active")
