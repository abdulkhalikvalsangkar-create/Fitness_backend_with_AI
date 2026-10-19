"""Clinical-range validation for the missing-aware model's 12 features.

Ranges are ported from the source project's `predict_from_terminal.py`
(`NUMERIC_RANGES`) — the bounds it applied at inference time, not invented
here. `ALIASES` maps a few common synonyms (including names the previous
RandomForest-cascade module recognised) onto the exact feature name this
model expects, so a caller using an old or slightly different name still
gets picked up rather than silently ignored.
"""

from __future__ import annotations

import math
from typing import Any, Dict

# Exact order the trained preprocessor/model expect.
FEATURES = [
    "age",
    "gender",
    "height_cm",
    "weight_kg",
    "hrv",
    "resting_heart_rate",
    "calories_burned",
    "activity_type",
    "fitness_level",
    "recovery_score",
    "day_strain",
    "sleep_hours",
]

CATEGORICAL_FEATURES = {"gender", "activity_type", "fitness_level"}

NUMERIC_RANGES: Dict[str, tuple[float, float]] = {
    "age": (1, 120),
    "height_cm": (30, 250),
    "weight_kg": (1, 500),
    "hrv": (0, 1000),
    "resting_heart_rate": (20, 250),
    "calories_burned": (0, 100000),
    "recovery_score": (0, 100),
    "day_strain": (0, 100),
    "sleep_hours": (0, 24),
}

ALIASES: Dict[str, str] = {
    "resting_hr": "resting_heart_rate",
    "rhr": "resting_heart_rate",
    "sleep": "sleep_hours",
    "sleep_mean": "sleep_hours",
    "sleep_duration": "sleep_hours",
    "weight": "weight_kg",
    "height": "height_cm",
    "strain": "day_strain",
    "calories": "calories_burned",
}


class ParameterValidator:
    """Validates a `features` dict against `FEATURES`/`NUMERIC_RANGES`.

    An unrecognised key is a warning, not an error — it's ignored by the
    model but shouldn't fail the whole request (mirrors the source
    project's `predict_from_terminal.py` behaviour for extra/unknown
    fields). A recognised key with a bad value (wrong type, non-finite,
    out of range) is an error.
    """

    def validate(self, features: Dict[str, Any]) -> Dict[str, Any]:
        errors: Dict[str, str] = {}
        warnings: Dict[str, str] = {}
        cleaned: Dict[str, Any] = {}

        if not isinstance(features, dict):
            return {
                "valid": False,
                "errors": {"payload": "features must be an object."},
                "warnings": {},
                "cleaned_features": {},
            }

        for raw_key, value in features.items():
            normalised = str(raw_key).strip().lower()
            key = ALIASES.get(normalised, normalised)

            if key not in FEATURES:
                warnings[str(raw_key)] = (
                    f"'{raw_key}' is not a recognised parameter. "
                    "It will be ignored by the model."
                )
                continue

            if value is None:
                errors[key] = f"'{key}' cannot be null or empty."
                continue

            if key in CATEGORICAL_FEATURES:
                if not isinstance(value, str) or not value.strip():
                    errors[key] = f"'{key}' must be a non-empty string."
                    continue
                cleaned[key] = value.strip()
                continue

            try:
                num_val = float(value)
            except (TypeError, ValueError):
                errors[key] = f"'{key}' must be a numeric value. Got: {value}."
                continue

            if not math.isfinite(num_val):
                errors[key] = f"'{key}' must be a finite number. Got: {value}."
                continue

            lower, upper = NUMERIC_RANGES[key]
            if not lower <= num_val <= upper:
                errors[key] = (
                    f"'{key}' is out of the clinically plausible range "
                    f"[{lower}-{upper}]. Got: {num_val}."
                )
                continue

            cleaned[key] = num_val

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "cleaned_features": cleaned,
        }

    def describe(self) -> list[dict[str, Any]]:
        rows = []
        for feature in FEATURES:
            if feature in CATEGORICAL_FEATURES:
                rows.append({"parameter": feature, "type": "categorical"})
            else:
                lower, upper = NUMERIC_RANGES[feature]
                rows.append({"parameter": feature, "type": "numeric", "min": lower, "max": upper})
        return rows
