"""MySQL-backed refresh-token sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


class AuthSessionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, session_id: str, user_id: str, refresh_hash: str, expires_at: datetime, device_id: Optional[str] = None, device_name: Optional[str] = None, platform: Optional[str] = None, app_version: Optional[str] = None) -> None:
        self.session.execute(text("""
            INSERT INTO auth_session
                (session_id, user_id, refresh_hash, device_id, device_name,
                 platform, app_version, expires_at)
            VALUES (:sid, :uid, :hash, :did, :dname, :platform, :version, :expires)
        """), {"sid": session_id, "uid": user_id, "hash": refresh_hash, "did": device_id, "dname": device_name, "platform": platform, "version": app_version, "expires": expires_at.replace(tzinfo=None)})

    def get_active_by_hash(self, refresh_hash: str, for_update: bool = False) -> Optional[dict[str, Any]]:
        lock_sql = " FOR UPDATE" if for_update else ""
        query = (
            "SELECT session_id, user_id, refresh_hash, device_id, device_name, "
            "platform, app_version, expires_at, revoked_at, replaced_by "
            "FROM auth_session WHERE refresh_hash = :hash "
            "AND revoked_at IS NULL AND expires_at > UTC_TIMESTAMP(3)" + lock_sql
        )
        row = self.session.execute(text(query), {"hash": refresh_hash}).mappings().first()
        return dict(row) if row else None

    def get_by_hash(self, refresh_hash: str) -> Optional[dict[str, Any]]:
        row = self.session.execute(text("SELECT session_id, user_id, revoked_at, replaced_by, expires_at FROM auth_session WHERE refresh_hash = :hash"), {"hash": refresh_hash}).mappings().first()
        return dict(row) if row else None

    def rotate(self, old_session_id: str, new_session_id: str) -> None:
        self.session.execute(text("UPDATE auth_session SET revoked_at = UTC_TIMESTAMP(3), replaced_by = :new WHERE session_id = :old AND revoked_at IS NULL"), {"old": old_session_id, "new": new_session_id})

    def revoke(self, session_id: str) -> bool:
        result = self.session.execute(text("UPDATE auth_session SET revoked_at = UTC_TIMESTAMP(3) WHERE session_id = :sid AND revoked_at IS NULL"), {"sid": session_id})
        return (result.rowcount or 0) > 0

    def revoke_user(self, user_id: str) -> int:
        result = self.session.execute(text("UPDATE auth_session SET revoked_at = UTC_TIMESTAMP(3) WHERE user_id = :uid AND revoked_at IS NULL"), {"uid": user_id})
        return result.rowcount or 0
