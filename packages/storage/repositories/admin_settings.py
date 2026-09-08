"""Admin settings repository with a short-lived process-local cache."""

from __future__ import annotations

import time
from threading import RLock
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


class AdminSettingRepository:
    """Read and write global admin settings.

    The cache reduces a database read for every research-job completion. The
    TTL is intentionally short because multiple API/worker processes may each
    have their own cache; writes update the current process immediately.
    """

    CACHE_TTL_SECONDS = 10.0
    _cache: dict[str, tuple[float, str]] = {}
    _cache_lock = RLock()

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> Optional[str]:
        setting_key = self._normalise_key(key)
        now = time.monotonic()

        with self._cache_lock:
            cached = self._cache.get(setting_key)
            if cached and now - cached[0] < self.CACHE_TTL_SECONDS:
                return cached[1]
            self._cache.pop(setting_key, None)

        row = self.session.execute(
            text("SELECT value FROM admin_setting WHERE setting_key = :key"),
            {"key": setting_key},
        ).mappings().first()
        if not row:
            return None

        value = str(row["value"])
        with self._cache_lock:
            self._cache[setting_key] = (now, value)
        return value

    def set(self, key: str, value: str, updated_by: Optional[str] = None) -> str:
        setting_key = self._normalise_key(key)
        setting_value = str(value).strip()
        if not setting_value:
            raise ValueError("setting value cannot be empty")
        if len(setting_value) > 255:
            raise ValueError("setting value exceeds 255 characters")

        self.session.execute(
            text(
                "INSERT INTO admin_setting (setting_key, value, updated_by) "
                "VALUES (:key, :value, :updated_by) "
                "ON DUPLICATE KEY UPDATE value = VALUES(value), "
                "updated_by = VALUES(updated_by), updated_at = UTC_TIMESTAMP(3)"
            ),
            {
                "key": setting_key,
                "value": setting_value,
                "updated_by": (str(updated_by)[:64] if updated_by else None),
            },
        )

        with self._cache_lock:
            self._cache[setting_key] = (time.monotonic(), setting_value)
        return setting_value

    @staticmethod
    def _normalise_key(key: str) -> str:
        setting_key = str(key).strip()
        if not setting_key:
            raise ValueError("setting key cannot be empty")
        if len(setting_key) > 64:
            raise ValueError("setting key exceeds 64 characters")
        return setting_key

    @classmethod
    def clear_cache(cls) -> None:
        with cls._cache_lock:
            cls._cache.clear()
