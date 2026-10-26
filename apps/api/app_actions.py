"""Actions behind the app's account, reminder and restaurant-search screens.

Registered on import by `apps.api.actions` (which owns the registry). Kept in
their own module so that file does not keep growing; the handlers follow the
same contract: parsed body, principal, session in; JSON-serialisable dict out.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from apps.api.actions import action
from apps.api.security import Principal
from packages.account.deletion import delete_user_data, remove_files
from packages.config import get_settings
from packages.domain.enums import JobType
from packages.restaurant import places
from packages.storage.repositories.jobs import JobRepository
from packages.storage.repositories.reminders import (
    CATEGORIES,
    ReminderError,
    ReminderNotFound,
    ReminderRepository,
)
from packages.storage.repositories.users import UserRepository

logger = logging.getLogger(__name__)

# How recently the user must have typed their password for account deletion.
# The app re-authenticates immediately before calling, so this is generous.
REAUTH_MAX_AGE_SECONDS = 300


# --------------------------------------------------------------------------
# account deletion
# --------------------------------------------------------------------------


@action("auth.delete_account")
def handle_delete_account(body: dict, principal: Principal, session: Session) -> dict[str, Any]:
    """Permanently delete the caller's account and all of its data.

    The app re-authenticates with Firebase first and sends the resulting fresh
    `firebase_id_token`. Idempotent: repeating the call after success (or after
    a partial failure) completes whatever is left and returns `deleted: true`.
    """
    token = body.get("firebase_id_token")
    if not isinstance(token, str) or not token.strip():
        raise HTTPException(status_code=400, detail="firebase_id_token is required")

    from packages.auth.firebase import delete_user, verify_id_token

    try:
        identity = verify_id_token(token.strip())
    except Exception as exc:
        logger.info("account deletion: token rejected: %s", exc)
        raise HTTPException(status_code=401, detail="re-authentication failed") from exc

    if identity.auth_time is None or time.time() - identity.auth_time > REAUTH_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="recent sign-in required: re-authenticate and retry")

    # The token must belong to the account being deleted. If the local account
    # is already gone (a retry), there is nothing to compare and nothing left to
    # protect locally; the token alone authorises removing its own Firebase user.
    user = UserRepository(session).get(principal.user_id)
    if user is not None and user.get("external_id") != identity.uid:
        raise HTTPException(status_code=403, detail="this sign-in does not belong to the signed-in account")

    removed, files = delete_user_data(session, principal.user_id)
    # Commit before touching Firebase: if the Firebase call fails, the local
    # erasure must stand and the retry must be able to finish the job.
    session.commit()
    remove_files(files)

    try:
        firebase_deleted = delete_user(identity.uid)
    except Exception as exc:
        logger.exception("firebase user deletion failed for uid %s", identity.uid)
        raise HTTPException(
            status_code=502,
            detail="your data was deleted but removing the sign-in account failed; retry to finish",
        ) from exc

    return {
        "action": "auth.delete_account",
        "deleted": True,
        "firebase_deleted": firebase_deleted,
        "removed": removed,
    }


# --------------------------------------------------------------------------
# data export
# --------------------------------------------------------------------------


@action("export.request")
def handle_export_request(body: dict, principal: Principal, session: Session) -> dict[str, Any]:
    """Start (or rejoin) a health-data export.

    Poll `job_status` with the returned `job_id`. While running, `job.result`
    carries `{stage, progress}`; on success it carries `download_url`,
    `expires_at`, `size_bytes` and `emailed`.
    """
    UserRepository(session).upsert(principal.user_id, locale=body.get("locale") or "en")

    ttl_hours = get_settings().export.link_ttl_hours
    existing = session.execute(
        sql_text(
            "SELECT job_id, status FROM job "
            "WHERE user_id = :uid AND job_type = :jt "
            "  AND status IN ('queued', 'running', 'succeeded') "
            "  AND created_at > UTC_TIMESTAMP(3) - INTERVAL :hrs HOUR "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"uid": principal.user_id, "jt": JobType.DATA_EXPORT.value, "hrs": ttl_hours},
    ).first()
    if existing:
        job_id, status = existing[0], existing[1]
    else:
        job_id = JobRepository(session).enqueue(
            job_type=JobType.DATA_EXPORT,
            payload={},
            user_id=principal.user_id,
            priority=150,
        )
        status = "queued"

    return {
        "action": "export.request",
        "job_id": job_id,
        "status": status,
        "poll_action": "job_status",
    }


# --------------------------------------------------------------------------
# restaurant place search
# --------------------------------------------------------------------------


@action("places.search")
def handle_places_search(body: dict, principal: Principal, session: Session) -> dict[str, Any]:
    """Type-ahead restaurant search. The chosen `place_id` goes into `scan`."""
    query = str(body.get("query") or "").strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="query must be at least 2 characters")

    near = None
    if body.get("lat") is not None and body.get("lng") is not None:
        try:
            lat, lng = float(body["lat"]), float(body["lng"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="lat and lng must be numbers") from exc
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise HTTPException(status_code=400, detail="lat/lng out of range")
        near = (lat, lng)

    try:
        limit = max(1, min(10, int(body.get("limit") or 5)))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="limit must be an integer") from exc

    try:
        found = places.search(
            query,
            session_token=str(body["session_token"])[:64] if body.get("session_token") else None,
            near=near,
            limit=limit,
        )
    except places.PlacesUnavailable as exc:
        logger.warning("place search unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="place search is currently unavailable") from exc

    return {"action": "places.search", "query": query, "places": [p.to_dict() for p in found]}


# --------------------------------------------------------------------------
# reminders
# --------------------------------------------------------------------------


@action("reminders")
def handle_reminders(body: dict, principal: Principal, session: Session) -> dict[str, Any]:
    """CRUD for the six reminder categories. See ReminderRepository for the
    schedule semantics (`date` one-off, `days` weekly, neither daily)."""
    op = str(body.get("op") or "list").lower()
    UserRepository(session).upsert(principal.user_id, locale=body.get("locale") or "en")
    repo = ReminderRepository(session, principal.user_id)

    try:
        if op == "list":
            category = body.get("category")
            if category is not None and str(category).lower() not in CATEGORIES:
                raise ReminderError(f"category must be one of: {', '.join(CATEGORIES)}")
            return {
                "action": "reminders",
                "reminders": repo.list(str(category).lower() if category else None),
            }

        if op == "create":
            return {"action": "reminders", "reminder": repo.create(body)}

        reminder_id = body.get("id")
        if not isinstance(reminder_id, str) or not reminder_id:
            raise ReminderError("id is required")

        if op == "update":
            return {"action": "reminders", "reminder": repo.update(reminder_id, body)}

        if op == "delete":
            # Deleting something already gone is not an error: the app's goal
            # (the reminder not existing) is met either way.
            return {"action": "reminders", "deleted": repo.delete(reminder_id), "id": reminder_id}
    except ReminderNotFound as exc:
        raise HTTPException(status_code=404, detail="reminder not found") from exc
    except ReminderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    raise HTTPException(status_code=400, detail="op must be list, create, update or delete")
