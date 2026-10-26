"""Account deletion — erase everything the backend holds about one user.

Most user-scoped tables cascade from `app_user`, but several do not have a
foreign key (conversation turns, jobs, blobs, per-user cache entries, rate-limit
buckets), so they are deleted explicitly. The explicit deletes also cover the
cascading tables: the guarantee should not depend on how a particular database
was created.

Files live outside the database and are returned to the caller rather than
removed here, so they are only deleted once the transaction has committed — a
rolled-back deletion must not leave rows pointing at missing files.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from packages.config import get_settings

logger = logging.getLogger(__name__)

# Deleted by `user_id = :uid`, in an order that satisfies foreign keys even if a
# cascade were missing. Counted under the keys the caller reports.
_USER_TABLES: list[tuple[str, str]] = [
    ("reminders", "reminder"),
    ("memories", "user_memory"),
    ("medical", "medical_report"),
    ("activities", "activity_session"),
    ("nutrition", "nutrition_day"),
    ("metrics", "health_metric"),
    ("aggregates", "user_aggregate"),
    ("consents", "consent_scope"),
    ("sessions", "auth_session"),
    ("conversation_summaries", "conversation_summary"),
    ("profile", "user_profile"),
]

# Research jobs are keyed by an ingredient token and shared across users via
# their idempotency key; another user's pending scan may depend on one, so they
# are detached from this user instead of deleted.
_SHARED_JOB_TYPES = ("chemical_research", "etl_chemical_kb", "embedding_backfill")


def delete_user_data(session: Session, user_id: str) -> tuple[dict[str, int], list[Path]]:
    """Delete all rows for `user_id`. Returns (counts, files to remove).

    Safe to call for a user that no longer exists: every delete is scoped by
    `user_id`, so it simply removes nothing.
    """
    params = {"uid": user_id}
    counts: dict[str, int] = {}

    def run(key: str, sql: str, extra: dict[str, Any] | None = None) -> int:
        result = session.execute(text(sql), {**params, **(extra or {})})
        removed = int(result.rowcount or 0)
        if removed:
            counts[key] = counts.get(key, 0) + removed
        return removed

    # Trace spans hang off turn ids, so they go before the turns do.
    run(
        "traces",
        "DELETE FROM trace_span WHERE turn_id IN "
        "(SELECT turn_id FROM conversation_turn WHERE user_id = :uid)",
    )
    run("chat_history", "DELETE FROM conversation_turn WHERE user_id = :uid")

    # Attachments: collect file paths first, then drop the rows. A content-
    # addressed file is shared between rows (and between users), so it is only
    # removed when nothing else still references it.
    blob_rows = session.execute(
        text("SELECT blob_id, sha256, rel_path FROM blob_object WHERE user_id = :uid"), params
    ).mappings().all()
    run("attachments", "DELETE FROM blob_object WHERE user_id = :uid")

    base_dir = Path(get_settings().storage.blob_dir)
    files: list[Path] = []
    seen_paths: set[str] = set()
    for row in blob_rows:
        rel_path = str(row["rel_path"])
        if rel_path in seen_paths:
            continue
        seen_paths.add(rel_path)
        still_used = session.execute(
            text("SELECT 1 FROM blob_object WHERE rel_path = :p LIMIT 1"), {"p": rel_path}
        ).first()
        if still_used is None:
            files.append(base_dir / rel_path)
            # OCR text is cached by image hash and is that user's document text.
            still_hashed = session.execute(
                text("SELECT 1 FROM blob_object WHERE sha256 = :s LIMIT 1"),
                {"s": row["sha256"]},
            ).first()
            if still_hashed is None:
                run("ocr_cache", "DELETE FROM ocr_result WHERE image_sha256 = :s", {"s": row["sha256"]})

    # Jobs: shared research jobs are anonymised, everything else is deleted.
    placeholders = ", ".join(f":t{i}" for i in range(len(_SHARED_JOB_TYPES)))
    shared = {f"t{i}": t for i, t in enumerate(_SHARED_JOB_TYPES)}
    session.execute(
        text(f"UPDATE job SET user_id = NULL WHERE user_id = :uid AND job_type IN ({placeholders})"),
        {**params, **shared},
    )
    run("jobs", "DELETE FROM job WHERE user_id = :uid")

    run("cache", "DELETE FROM cache_entry WHERE scope = :uid")
    run("rate_limits", "DELETE FROM rate_limit_bucket WHERE subject = :subj", {"subj": f"user:{user_id}"[:128]})

    for key, table in _USER_TABLES:
        run(key, f"DELETE FROM {table} WHERE user_id = :uid")

    run("account", "DELETE FROM app_user WHERE user_id = :uid")

    export_dir = Path(get_settings().export.dir) / _safe_segment(user_id)
    if export_dir.is_dir():
        files.extend(p for p in export_dir.iterdir() if p.is_file())
        files.append(export_dir)

    return counts, files


def remove_files(paths: list[Path]) -> int:
    """Best-effort removal after commit. Returns how many were removed."""
    removed = 0
    for path in paths:
        try:
            if path.is_file():
                path.unlink()
                removed += 1
            elif path.is_dir():
                path.rmdir()
        except OSError as exc:
            logger.warning("could not delete %s during account deletion: %s", path, exc)
    return removed


def _safe_segment(value: str) -> str:
    """A user id as a single path segment (ids are UUIDs, but never trust that
    when the value ends up in a filesystem path)."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)[:64]
