"""
parameter_validator.py
Validates all input parameter values against clinically plausible ranges
before they are passed to the BiologicalAgeModel.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import numpy as np


@dataclass
class ParamRule:
    name: str
    unit: str
    min_val: float
    max_val: float
    description: str
    allowed_values: Optional[list] = None   # for discrete/categorical params


# ------------------------------------------------------------------
# MASTER VALIDATION REGISTRY — all supported parameters
# ------------------------------------------------------------------
PARAM_RULES: Dict[str, ParamRule] = {

    # ── Activity ─────────────────────────────────────────────────
    "activity_mean": ParamRule(
        "activity_mean", "steps/day", 0, 80000,
        "Average daily step count."
    ),
    "activity": ParamRule(
        "activity", "steps/day", 0, 80000,
        "Daily physical step count."
    ),
    "steps": ParamRule(
        "steps", "steps/day", 0, 80000,
        "Daily physical step count."
    ),
    "step_count": ParamRule(
        "step_count", "steps/day", 0, 80000,
        "Daily physical step count."
    ),
    "day_strain": ParamRule(
        "day_strain", "score (0–21)", 0, 21,
        "WHOOP day strain score."
    ),
    "activity_strain": ParamRule(
        "activity_strain", "score (0–21)", 0, 21,
        "WHOOP per-workout strain score."
    ),
    "activity_duration_min": ParamRule(
        "activity_duration_min", "minutes", 0, 600,
        "Workout duration in minutes."
    ),
    "activity_calories": ParamRule(
        "activity_calories", "kcal", 0, 5000,
        "Calories burned during workout."
    ),
    "calories_burned": ParamRule(
        "calories_burned", "kcal/day", 500, 10000,
        "Total daily caloric burn."
    ),
    "workout_completed": ParamRule(
        "workout_completed", "0 or 1", 0, 1,
        "Whether a workout was completed today.",
        allowed_values=[0, 1]
    ),
    "vo2_max": ParamRule(
        "vo2_max", "mL/kg/min", 10, 90,
        "VO₂ Max — maximal oxygen uptake."
    ),

    # ── Sleep ─────────────────────────────────────────────────────
    "sleep_hours": ParamRule(
        "sleep_hours", "hours", 0, 24,
        "Total sleep duration per night."
    ),
    "sleep_mean": ParamRule(
        "sleep_mean", "hours", 0, 24,
        "Average sleep hours."
    ),
    "sleep": ParamRule(
        "sleep", "hours", 0, 24,
        "Sleep duration in hours."
    ),
    "sleep_efficiency": ParamRule(
        "sleep_efficiency", "%", 0, 100,
        "Percentage of time in bed actually spent sleeping."
    ),
    "sleep_performance": ParamRule(
        "sleep_performance", "%", 0, 100,
        "WHOOP sleep performance score."
    ),
    "light_sleep_hours": ParamRule(
        "light_sleep_hours", "hours", 0, 12,
        "Hours of light sleep per night."
    ),
    "rem_sleep_hours": ParamRule(
        "rem_sleep_hours", "hours", 0, 6,
        "Hours of REM sleep per night."
    ),
    "deep_sleep_hours": ParamRule(
        "deep_sleep_hours", "hours", 0, 5,
        "Hours of deep/slow-wave sleep per night."
    ),
    "wake_ups": ParamRule(
        "wake_ups", "count", 0, 30,
        "Number of wake-up events per night."
    ),
    "time_to_fall_asleep_min": ParamRule(
        "time_to_fall_asleep_min", "minutes", 0, 180,
        "Time (in minutes) to fall asleep."
    ),

    # ── Heart & Recovery ─────────────────────────────────────────
    "hrv": ParamRule(
        "hrv", "ms", 1, 300,
        "Heart Rate Variability in milliseconds."
    ),
    "hrv_baseline": ParamRule(
        "hrv_baseline", "ms", 1, 300,
        "HRV baseline reference value in milliseconds."
    ),
    "resting_heart_rate": ParamRule(
        "resting_heart_rate", "bpm", 25, 130,
        "Resting heart rate in beats per minute."
    ),
    "resting_hr": ParamRule(
        "resting_hr", "bpm", 25, 130,
        "Resting heart rate in beats per minute."
    ),
    "rhr_baseline": ParamRule(
        "rhr_baseline", "bpm", 25, 130,
        "Resting HR baseline reference value."
    ),
    "recovery_score": ParamRule(
        "recovery_score", "score (0–100)", 0, 100,
        "WHOOP recovery score."
    ),
    "avg_heart_rate": ParamRule(
        "avg_heart_rate", "bpm", 40, 220,
        "Average heart rate during workout."
    ),
    "max_heart_rate": ParamRule(
        "max_heart_rate", "bpm", 60, 230,
        "Maximum heart rate reached during workout."
    ),
    "respiratory_rate": ParamRule(
        "respiratory_rate", "breaths/min", 4, 40,
        "Breathing rate at rest."
    ),
    "skin_temp_deviation": ParamRule(
        "skin_temp_deviation", "°C", -5, 5,
        "Deviation from baseline skin temperature."
    ),

    # ── HR Zones ─────────────────────────────────────────────────
    "hr_zone_1_min": ParamRule(
        "hr_zone_1_min", "minutes", 0, 600,
        "Minutes spent in HR Zone 1 (very light)."
    ),
    "hr_zone_2_min": ParamRule(
        "hr_zone_2_min", "minutes", 0, 600,
        "Minutes spent in HR Zone 2 (light)."
    ),
    "hr_zone_3_min": ParamRule(
        "hr_zone_3_min", "minutes", 0, 600,
        "Minutes spent in HR Zone 3 (moderate)."
    ),
    "hr_zone_4_min": ParamRule(
        "hr_zone_4_min", "minutes", 0, 600,
        "Minutes spent in HR Zone 4 (hard)."
    ),
    "hr_zone_5_min": ParamRule(
        "hr_zone_5_min", "minutes", 0, 600,
        "Minutes spent in HR Zone 5 (maximum)."
    ),

    # ── Body Metrics ─────────────────────────────────────────────
    "weight_kg": ParamRule(
        "weight_kg", "kg", 20, 300,
        "Body weight in kilograms."
    ),
    "height_cm": ParamRule(
        "height_cm", "cm", 50, 250,
        "Height in centimetres."
    ),

    # ── Blood Pressure ────────────────────────────────────────────
    "systolic_bp": ParamRule(
        "systolic_bp", "mmHg", 60, 250,
        "Systolic blood pressure in mmHg."
    ),
    "diastolic_bp": ParamRule(
        "diastolic_bp", "mmHg", 30, 150,
        "Diastolic blood pressure in mmHg."
    ),

    # ── Fitness Profile ───────────────────────────────────────────
    "fitness_level": ParamRule(
        "fitness_level", "1–4", 1, 4,
        "Fitness level: 1=Beginner, 2=Intermediate, 3=Advanced, 4=Elite.",
        allowed_values=[1, 2, 3, 4]
    ),
    "workout_time_of_day": ParamRule(
        "workout_time_of_day", "1–3", 1, 3,
        "Workout time: 1=Morning, 2=Afternoon, 3=Evening.",
        allowed_values=[1, 2, 3]
    ),
}


class ParameterValidator:
    """
    Validates a features dictionary against the PARAM_RULES registry.
    Strictly checks for non-numeric, null, NaN, and infinite values,
    as well as clinically plausible physiological boundaries.
    """

    def validate(self, features: dict) -> dict:
        errors = {}
        warnings = {}
        cleaned = {}

        if not isinstance(features, dict):
            return {
                "valid": False,
                "errors": {"payload": "Features payload must be an object."},
                "warnings": {},
                "cleaned_features": {}
            }

        for key, value in features.items():
            rule = PARAM_RULES.get(key)

            if rule is None:
                # Unknown parameter — pass through with a warning
                warnings[key] = f"'{key}' is not a recognised parameter. It will be ignored by the model."
                continue

            # Check for null, non-numeric, NaN, Infinity
            if value is None:
                errors[key] = f"'{key}' cannot be null or empty."
                continue

            try:
                num_val = float(value)
            except (ValueError, TypeError):
                errors[key] = f"'{key}' must be a numeric value. Got: {value}."
                continue

            if not np.isfinite(num_val):
                errors[key] = f"'{key}' must be a finite number. Got: {value}."
                continue

            # Allowed discrete values check
            if rule.allowed_values is not None:
                if int(num_val) not in rule.allowed_values:
                    errors[key] = (
                        f"'{key}' must be one of {rule.allowed_values}. "
                        f"Got: {num_val}."
                    )
                    continue

            # Range check
            if num_val < rule.min_val or num_val > rule.max_val:
                errors[key] = (
                    f"'{key}' is out of the clinically plausible range "
                    f"[{rule.min_val}–{rule.max_val} {rule.unit}]. "
                    f"Got: {num_val}."
                )
                continue

            cleaned[key] = num_val

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "cleaned_features": cleaned
        }

    def describe(self) -> list:
        """Return a list of all known parameters with their valid ranges."""
        return [
            {
                "parameter": rule.name,
                "unit": rule.unit,
                "min": rule.min_val,
                "max": rule.max_val,
                "description": rule.description,
                "allowed_values": rule.allowed_values
            }
            for rule in PARAM_RULES.values()
        ]
