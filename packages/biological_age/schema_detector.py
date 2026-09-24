import pandas as pd
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set

# -------------------------------------------------------------------------
# Validated biometric columns known by the engine
# -------------------------------------------------------------------------
REGISTERED_BIOMETRIC_COLUMNS: Set[str] = {
    # Activity
    "activity", "activity_mean", "steps", "step_count", "daily_steps", "total_steps", "day_strain", "activity_strain",
    "activity_duration_min", "activity_calories", "calories_burned", "calories", "active_calories",
    "workout_completed", "vo2_max", "vo2max", "vo2", "enmo", "enmo_g",
    # Sleep
    "sleep", "sleep_hours", "sleep_mean", "sleep_duration", "total_sleep", "sleep_time", "hours_slept",
    "sleep_efficiency", "sleep_performance", "light_sleep_hours", "rem_sleep_hours", "deep_sleep_hours", "wake_ups",
    "time_to_fall_asleep_min",
    # Heart & Recovery
    "hrv", "hrv_baseline", "hrv_ms", "rmssd", "sdnn", "resting_heart_rate", "resting_hr", "rhr", "rhr_baseline", "heart_rate", "pulse", "resting_pulse",
    "recovery_score", "recovery", "whoop_recovery", "recovery_index", "avg_heart_rate", "max_heart_rate",
    "respiratory_rate", "rr", "breathing_rate", "respiration_rate",
    # HR Zones
    "hr_zone_1_min", "hr_zone_2_min", "hr_zone_3_min", "hr_zone_4_min", "hr_zone_5_min",
    # Anthropometric / Vitals / Temp
    "weight", "weight_kg", "body_weight", "height", "height_cm", "body_height",
    "systolic", "systolic_bp", "bp_systolic", "diastolic", "diastolic_bp", "bp_diastolic",
    "temperature", "skin_temperature", "body_temperature", "temp", "temp_skin",
    "fitness_level", "workout_time_of_day"
}

TIMESTAMP_CANDIDATES: List[str] = [
    "timestamp", "datetime", "date", "time", "created_at", "recorded_at", "measurement_time"
]

METADATA_COLUMNS: Set[str] = {
    "id", "user_id", "subject_id", "participant_id", "index", "record_id"
}

AGE_COLUMNS: Set[str] = {
    "age", "chronological_age", "chrono_age", "participant_age", "subject_age", "user_age"
}

GENDER_COLUMNS: Set[str] = {
    "gender", "sex", "participant_gender", "subject_gender"
}


@dataclass
class SchemaReport:
    timestamp_col: Optional[str] = None
    age_col: Optional[str] = None
    registered_columns: List[str] = field(default_factory=list)
    time_series_candidates: List[str] = field(default_factory=list)
    unknown_columns: List[str] = field(default_factory=list)
    ignored_metadata_columns: List[str] = field(default_factory=list)
    data_period_days: Optional[float] = None
    row_count: int = 0


def normalize_col_name(col: str) -> str:
    """
    Normalizes column header string:
    - Splits camelCase / PascalCase (e.g. StepCount -> step_count, restingHeartRate -> resting_heart_rate)
    - Removes unit parens like (bpm), (ms), (hours), (kg), (seconds)
    - Replaces spaces and dashes with underscores
    - Lowercases everything
    """
    import re
    s = str(col).strip()
    # Insert underscore between camelCase/PascalCase words
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = s.lower()
    # Strip unit parentheses
    s = re.sub(r"\([^)]*\)", "", s).strip()
    s = s.replace(" ", "_").replace("-", "_")
    return s


class SchemaDetector:
    """
    Auto-detects columns present in an uploaded DataFrame:
    - Identifies timestamp column and estimates time span.
    - Matches registered biometric columns (case-insensitive, alias-aware).
    - Identifies time-series candidates and temporal intervals.
    - Identifies unknown / unregistered columns (to flag as ignored per Case 4).
    """

    def detect(self, df: pd.DataFrame) -> SchemaReport:
        report = SchemaReport(row_count=len(df))

        # 1. Detect timestamp column
        for col in df.columns:
            if normalize_col_name(col) in TIMESTAMP_CANDIDATES:
                report.timestamp_col = col
                break

        if report.timestamp_col is None:
            # Fallback: check if any column can be parsed as datetime
            for col in df.columns:
                if df[col].dtype == "object":
                    try:
                        sample = df[col].dropna().head(15)
                        if len(sample) > 0:
                            pd.to_datetime(sample)
                            report.timestamp_col = col
                            break
                    except Exception:
                        continue

        # Estimate data period if timestamp exists
        if report.timestamp_col and len(df) > 1:
            try:
                dt_series = pd.to_datetime(df[report.timestamp_col], errors="coerce").dropna()
                if len(dt_series) >= 2:
                    diff = (dt_series.max() - dt_series.min()).total_seconds() / 86400.0
                    report.data_period_days = round(max(diff, 1.0), 1)
            except Exception:
                pass

        # 2. Categorize all columns
        for col in df.columns:
            col_norm = normalize_col_name(col)

            if col == report.timestamp_col:
                continue

            if col_norm in METADATA_COLUMNS:
                report.ignored_metadata_columns.append(col)
                continue

            if col_norm in AGE_COLUMNS:
                report.age_col = col
                continue

            # Check if recognized in biometric registry or keyword substring matches
            is_biometric = (
                col_norm in REGISTERED_BIOMETRIC_COLUMNS
                or any(kw in col_norm for kw in (
                    "activity", "step", "sleep", "hrv", "heart_rate", "heartrate", "pulse", "recovery",
                    "respiratory", "breathing", "vo2", "weight", "height", "systolic", "diastolic", "temp", "enmo"
                ))
            )

            if is_biometric:
                report.registered_columns.append(col)

                # Check if it has time-series potential (numeric with variance)
                numeric_vals = pd.to_numeric(df[col], errors="coerce").dropna()
                if len(numeric_vals) >= 20 and report.timestamp_col is not None:
                    if numeric_vals.std() > 0.001:
                        report.time_series_candidates.append(col)
            else:
                # Column is not recognized in validated registry!
                report.unknown_columns.append(col)

        return report
