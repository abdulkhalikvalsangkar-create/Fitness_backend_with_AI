"""App-facing health scores.

Health Connect supplies measurements, not recovery/sleep/strain scores.  These
functions deliberately remain deterministic and explain which inputs were
used, so a client never has to mistake an estimated value for a native field.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Iterable, Optional


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def sleep_score(hours: Any) -> Optional[float]:
    """Score sleep duration on a 0-100 scale.

    Seven to nine hours receives full duration credit. Short sleep is penalised
    more heavily than long sleep; this is a duration score, not a diagnosis.
    """
    value = _number(hours)
    if value is None or not 0 <= value <= 24:
        return None
    if value < 7:
        score = 100 - (7 - value) * 20
    elif value <= 9:
        score = 100
    else:
        score = 100 - (value - 9) * 10
    return round(_clamp(score, 0, 100), 1)


def strain_score(*, steps: Any = None, active_minutes: Any = None, calories: Any = None) -> Optional[float]:
    """Derive a 0-21 daily strain estimate from movement and exercise."""
    step_value = _number(steps)
    minute_value = _number(active_minutes)
    calorie_value = _number(calories)
    if step_value is None and minute_value is None and calorie_value is None:
        return None
    score = 0.0
    if step_value is not None:
        score += _clamp(step_value, 0, 12_000) / 12_000 * 10.5
    if minute_value is not None:
        score += _clamp(minute_value, 0, 90) / 90 * 7.0
    if calorie_value is not None:
        score += _clamp(calorie_value, 0, 700) / 700 * 3.5
    return round(_clamp(score, 0, 21), 1)


def recovery_score(
    *,
    sleep: Any = None,
    strain: Any = None,
    hrv: Any = None,
    hrv_baseline: Any = None,
    rhr: Any = None,
    rhr_baseline: Any = None,
) -> tuple[Optional[float], list[str]]:
    """Return a weighted 0-100 recovery estimate and the components used.

    Missing HRV is expected on some Health Connect runtimes. Components are
    reweighted instead of converting an absent sensor reading into zero.
    """
    components: list[tuple[str, float, float]] = []
    sleep_value = _number(sleep)
    strain_value = _number(strain)
    hrv_value, hrv_base = _number(hrv), _number(hrv_baseline)
    rhr_value, rhr_base = _number(rhr), _number(rhr_baseline)

    if sleep_value is not None:
        components.append(("sleep", _clamp(sleep_value, 0, 100), 0.45))
    if hrv_value is not None and hrv_base and hrv_base > 0:
        components.append(("hrv_vs_baseline", _clamp(50 + 100 * (hrv_value / hrv_base - 1), 0, 100), 0.25))
    if rhr_value is not None and rhr_base and rhr_base > 0:
        components.append(("rhr_vs_baseline", _clamp(50 - 5 * (rhr_value - rhr_base), 0, 100), 0.20))
    if strain_value is not None:
        components.append(("previous_day_strain", _clamp(100 - strain_value / 21 * 70, 0, 100), 0.10))

    if not components:
        return None, []
    weight = sum(item[2] for item in components)
    result = sum(value * item_weight for _, value, item_weight in components) / weight
    return round(_clamp(result, 0, 100), 1), [name for name, _, _ in components]


def _preferred(points: Iterable[dict[str, Any]], metric: str, on: date) -> Optional[dict[str, Any]]:
    matches = [p for p in points if p.get("metric") == metric and p.get("measured_on") == on]
    if not matches:
        return None
    source_rank = {"health_connect": 4, "healthconnect": 4, "wearable": 3, "manual": 2, "chat": 1}
    return max(matches, key=lambda p: source_rank.get(str(p.get("source") or "").lower(), 0))


def _value(points: Iterable[dict[str, Any]], metric: str, on: date) -> Optional[float]:
    point = _preferred(points, metric, on)
    return _number(point.get("value")) if point else None


def _baseline(points: list[dict[str, Any]], metric: str, before: date, days: int = 28) -> Optional[float]:
    since = before - timedelta(days=days)
    values = [
        _number(p.get("value"))
        for p in points
        if p.get("metric") == metric and since <= p.get("measured_on", date.min) < before
    ]
    usable = [value for value in values if value is not None]
    return sum(usable) / len(usable) if usable else None


def build_dashboard(
    *,
    on: date,
    metric_points: list[dict[str, Any]],
    activity_days: dict[date, dict[str, float]],
) -> dict[str, Any]:
    """Build current dashboard scores plus an inclusive seven-day history."""
    history: list[dict[str, Any]] = []
    daily: dict[date, dict[str, Any]] = {}

    # Calculate oldest-first because today's recovery uses yesterday's strain.
    start = on - timedelta(days=6)
    for offset in range(7):
        day = start + timedelta(days=offset)
        sleep_point = _preferred(metric_points, "sleep", day)
        direct_sleep = _number(sleep_point.get("value")) if sleep_point else None
        sleep_unit = str(sleep_point.get("unit") or "").strip().lower() if sleep_point else ""
        hours = _value(metric_points, "sleep_hours", day)
        sleep_is_duration = sleep_unit in {"h", "hr", "hrs", "hour", "hours"} or (
            not sleep_unit and direct_sleep is not None and direct_sleep <= 24
        )
        if direct_sleep is not None and not sleep_is_duration and 0 <= direct_sleep <= 100:
            calculated_sleep, sleep_source = direct_sleep, "synced"
        else:
            calculated_sleep = sleep_score(hours if hours is not None else direct_sleep)
            sleep_source = "derived" if calculated_sleep is not None else "unavailable"

        direct_strain = _value(metric_points, "strain", day)
        activity = activity_days.get(day, {})
        if direct_strain is not None and 0 <= direct_strain <= 21:
            calculated_strain, strain_source = direct_strain, "synced"
        else:
            calculated_strain = strain_score(
                steps=_value(metric_points, "steps", day),
                active_minutes=activity.get("active_minutes"),
                calories=activity.get("calories"),
            )
            strain_source = "derived" if calculated_strain is not None else "unavailable"

        direct_recovery = _value(metric_points, "recovery", day)
        if direct_recovery is not None and 0 <= direct_recovery <= 100:
            calculated_recovery, recovery_inputs, recovery_source = direct_recovery, ["synced_recovery"], "synced"
        else:
            previous = daily.get(day - timedelta(days=1), {})
            calculated_recovery, recovery_inputs = recovery_score(
                sleep=calculated_sleep,
                strain=previous.get("strain_score"),
                hrv=_value(metric_points, "hrv", day),
                hrv_baseline=_baseline(metric_points, "hrv", day),
                rhr=_value(metric_points, "rhr", day),
                rhr_baseline=_baseline(metric_points, "rhr", day),
            )
            recovery_source = "derived" if calculated_recovery is not None else "unavailable"

        daily[day] = {
            "recovery_score": calculated_recovery,
            "strain_score": calculated_strain,
            "sleep_score": calculated_sleep,
            "recovery_source": recovery_source,
            "strain_source": strain_source,
            "sleep_source": sleep_source,
            "recovery_inputs": recovery_inputs,
        }
        history.append({"date": day.isoformat(), "score": calculated_recovery, "source": recovery_source})

    current = daily[on]
    body_fat_points = [p for p in metric_points if p.get("metric") == "body_fat" and p.get("measured_on") <= on]
    body_fat_point = max(body_fat_points, key=lambda p: p.get("measured_on"), default=None)

    def score_payload(name: str, scale: int) -> dict[str, Any]:
        value = current[f"{name}_score"]
        source = current[f"{name}_source"]
        payload: dict[str, Any] = {"value": value, "scale_max": scale, "source": source}
        if name == "recovery":
            payload["inputs_used"] = current["recovery_inputs"]
        return payload

    return {
        "date": on.isoformat(),
        "recovery": score_payload("recovery", 100),
        "strain": score_payload("strain", 21),
        "sleep": score_payload("sleep", 100),
        "recovery_history": history,
        "body_fat": {
            "value": _number(body_fat_point.get("value")) if body_fat_point else None,
            "unit": (body_fat_point.get("unit") or "%") if body_fat_point else "%",
            "measured_on": body_fat_point.get("measured_on").isoformat() if body_fat_point else None,
            "source": body_fat_point.get("source") if body_fat_point else None,
        },
    }
