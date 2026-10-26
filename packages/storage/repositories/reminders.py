"""Reminders — user-defined notifications the app schedules locally.

The backend stores and validates them; delivery (local notifications or push)
is the app's job. Schedule semantics:

  * `date` set            -> one-off, on that date at `time`
  * `days` set            -> weekly, on those ISO weekdays (1 = Mon ... 7 = Sun)
  * neither               -> daily at `time`
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

CATEGORIES = ("dosage", "feeding", "medication", "activity", "health", "petcare")
MAX_REMINDERS_PER_USER = 200

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ReminderError(ValueError):
    """A field failed validation. The message is safe to return to the client."""


class ReminderNotFound(LookupError):
    pass


def _string(value: Any, field: str, max_len: int, *, required: bool = False) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ReminderError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ReminderError(f"{field} must be a string")
    value = value.strip()
    if len(value) > max_len:
        raise ReminderError(f"{field} must be at most {max_len} characters")
    return value


def _time(value: Any) -> str:
    if not isinstance(value, str) or not _TIME_RE.match(value.strip()):
        raise ReminderError("time must be 24-hour HH:mm, e.g. 08:30")
    return value.strip()


def _date(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ReminderError("date must be YYYY-MM-DD") from exc


def _days(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReminderError("days must be a list of integers 1 (Mon) to 7 (Sun)")
    days: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 7:
            raise ReminderError("days must be a list of integers 1 (Mon) to 7 (Sun)")
        days.add(item)
    return sorted(days)


def normalise(fields: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    """Validate client fields into column values.

    With `partial` (update), only the keys present are validated and returned.
    """
    out: dict[str, Any] = {}

    def present(key: str) -> bool:
        return key in fields

    if not partial or present("category"):
        category = str(fields.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            raise ReminderError(f"category must be one of: {', '.join(CATEGORIES)}")
        out["category"] = category
    if not partial or present("title"):
        out["title"] = _string(fields.get("title"), "title", 191, required=True)
    if not partial or present("time"):
        out["remind_time"] = _time(fields.get("time"))
    if present("subtitle"):
        out["subtitle"] = _string(fields.get("subtitle"), "subtitle", 255)
    if present("date"):
        out["remind_date"] = _date(fields.get("date"))
    if present("days"):
        out["days"] = _days(fields.get("days"))
    if present("enabled"):
        if not isinstance(fields["enabled"], bool):
            raise ReminderError("enabled must be true or false")
        out["enabled"] = fields["enabled"]
    for key, limit in (("supplement_id", 64), ("pet_id", 64), ("item", 191)):
        if present(key):
            out[key] = _string(fields.get(key), key, limit)
    return out


def _check_schedule(date_value: Optional[str], days: list[int]) -> None:
    if date_value and days:
        raise ReminderError("provide either date (one-off) or days (weekly), not both")


def _loads_days(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        return [int(d) for d in value]
    try:
        return [int(d) for d in json.loads(value)]
    except (TypeError, ValueError):
        return []


def to_api(row: dict[str, Any]) -> dict[str, Any]:
    remind_date = row.get("remind_date")
    return {
        "id": row["reminder_id"],
        "category": row["category"],
        "enabled": bool(row["enabled"]),
        "title": row["title"],
        "subtitle": row.get("subtitle"),
        "time": row["remind_time"],
        "date": remind_date.isoformat() if isinstance(remind_date, (date, datetime)) else remind_date,
        "days": _loads_days(row.get("days")),
        "supplement_id": row.get("supplement_id"),
        "pet_id": row.get("pet_id"),
        "item": row.get("item"),
    }


_COLUMNS = (
    "reminder_id, category, enabled, title, subtitle, remind_time, remind_date, "
    "days, supplement_id, pet_id, item"
)


class ReminderRepository:
    def __init__(self, session: Session, user_id: str) -> None:
        self.session = session
        self.user_id = user_id

    def list(self, category: Optional[str] = None) -> list[dict[str, Any]]:
        sql = f"SELECT {_COLUMNS} FROM reminder WHERE user_id = :uid"
        params: dict[str, Any] = {"uid": self.user_id}
        if category:
            sql += " AND category = :cat"
            params["cat"] = category
        sql += " ORDER BY remind_time, created_at"
        rows = self.session.execute(text(sql), params).mappings().all()
        return [to_api(dict(r)) for r in rows]

    def get(self, reminder_id: str) -> Optional[dict[str, Any]]:
        row = self.session.execute(
            text(f"SELECT {_COLUMNS} FROM reminder WHERE reminder_id = :rid AND user_id = :uid"),
            {"rid": reminder_id, "uid": self.user_id},
        ).mappings().first()
        return to_api(dict(row)) if row else None

    def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        values = normalise(fields, partial=False)
        days = values.get("days", [])
        _check_schedule(values.get("remind_date"), days)

        count = self.session.execute(
            text("SELECT COUNT(*) FROM reminder WHERE user_id = :uid"), {"uid": self.user_id}
        ).scalar()
        if int(count or 0) >= MAX_REMINDERS_PER_USER:
            raise ReminderError(f"reminder limit reached ({MAX_REMINDERS_PER_USER})")

        reminder_id = uuid.uuid4().hex
        self.session.execute(
            text(
                """
                INSERT INTO reminder
                    (reminder_id, user_id, category, enabled, title, subtitle, remind_time,
                     remind_date, days, supplement_id, pet_id, item)
                VALUES
                    (:rid, :uid, :category, :enabled, :title, :subtitle, :remind_time,
                     :remind_date, :days, :supplement_id, :pet_id, :item)
                """
            ),
            {
                "rid": reminder_id,
                "uid": self.user_id,
                "category": values["category"],
                "enabled": 1 if values.get("enabled", True) else 0,
                "title": values["title"],
                "subtitle": values.get("subtitle"),
                "remind_time": values["remind_time"],
                "remind_date": values.get("remind_date"),
                "days": json.dumps(days),
                "supplement_id": values.get("supplement_id"),
                "pet_id": values.get("pet_id"),
                "item": values.get("item"),
            },
        )
        created = self.get(reminder_id)
        assert created is not None
        return created

    def update(self, reminder_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        current = self.get(reminder_id)
        if current is None:
            raise ReminderNotFound(reminder_id)

        values = normalise(fields, partial=True)
        if not values:
            raise ReminderError("no updatable fields supplied")

        # A schedule change replaces the whole schedule: setting `date` clears
        # `days` and vice versa, so a one-off can be turned weekly (and back)
        # with a single call.
        if "remind_date" in values and values["remind_date"] and "days" not in values:
            values["days"] = []
        if "days" in values and values["days"] and "remind_date" not in values:
            values["remind_date"] = None
        _check_schedule(
            values.get("remind_date", current["date"]),
            values.get("days", current["days"]),
        )

        sets: list[str] = []
        params: dict[str, Any] = {"rid": reminder_id, "uid": self.user_id}
        for column, value in values.items():
            sets.append(f"{column} = :{column}")
            if column == "days":
                params[column] = json.dumps(value)
            elif column == "enabled":
                params[column] = 1 if value else 0
            else:
                params[column] = value
        self.session.execute(
            text(f"UPDATE reminder SET {', '.join(sets)} WHERE reminder_id = :rid AND user_id = :uid"),
            params,
        )
        updated = self.get(reminder_id)
        assert updated is not None
        return updated

    def delete(self, reminder_id: str) -> bool:
        result = self.session.execute(
            text("DELETE FROM reminder WHERE reminder_id = :rid AND user_id = :uid"),
            {"rid": reminder_id, "uid": self.user_id},
        )
        return (result.rowcount or 0) > 0
