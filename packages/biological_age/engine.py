import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
import logging

from packages.biological_age.schema_detector import SchemaDetector, SchemaReport, normalize_col_name
from packages.biological_age.missing_data_engine import MissingDataEngine, MissingDataReport
from packages.biological_age.feature_registry import FeatureRegistry, FeatureDefinition
from packages.biological_age.validators import DataQualityChecker
from packages.biological_age.age_model import ModelRegistry, BiologicalAgeModel

logger = logging.getLogger("bio_age.engine")


AGE_ALIASES = [
    "age",
    "chronological_age",
    "chrono_age",
    "participant_age",
    "subject_age",
    "user_age",
]

GENDER_ALIASES = [
    "gender",
    "sex",
    "participant_gender",
    "subject_gender",
]


def detect_age_column(df: pd.DataFrame) -> Tuple[Optional[float], Optional[str]]:
    """
    Detects chronological age from all reasonable age column aliases.
    Validates that age is between 1 and 120.
    Returns (age_val, col_name) if found, else (None, None).
    Raises ValueError if an age column is found but its value is outside [1, 120].
    """
    normalized = {
        str(col).strip().lower().replace(" ", "_").replace("-", "_"): col
        for col in df.columns
    }

    for alias in AGE_ALIASES:
        if alias in normalized:
            original_col = normalized[alias]
            values = pd.to_numeric(df[original_col], errors="coerce").dropna()
            if len(values) == 0:
                continue
            age = float(values.iloc[0])
            if not np.isfinite(age) or age < 1.0 or age > 120.0:
                raise ValueError(
                    f"Invalid chronological age ({age}) in column '{original_col}'. "
                    f"Age must be between 1 and 120 years."
                )
            return age, original_col

    return None, None


def detect_gender(df: pd.DataFrame) -> str:
    """
    Detects gender from dataframe column aliases. Returns 'female', 'male', or 'generic'.
    """
    normalized = {
        str(col).strip().lower().replace(" ", "_").replace("-", "_"): col
        for col in df.columns
    }
    for alias in GENDER_ALIASES:
        if alias in normalized:
            original_col = normalized[alias]
            val = str(df[original_col].dropna().iloc[0]).strip().lower()
            if val in ("female", "f"):
                return "female"
            elif val in ("male", "m"):
                return "male"
    return "generic"


class AdaptiveBiologicalAgeEngine:
    """
    Adaptive Biological Age Engine — explicit multi-stage architecture:
    Stage 1: Upfront Ingestion (Age detection, Gender detection, Timestamp detection, Alias mapping & Unit normalization)
    Stage 2: Time Series Check & Diagnostics (Frequency diagnostics, median interval, total days)
    Stage 3: Feature Extraction & Quality Checking (Quality checks across registered clinical biometrics)
    Stage 4: Cascade Model Execution:
             Priority 1: Model A (Full ML: HRV + activity + sleep + resting HR)
             Priority 2: Model B (Non-HRV ML: activity + sleep + resting HR)
             Priority 3: Validated Fallback (Clinical rule-based: activity OR sleep)
             Priority 4: No Prediction (Insufficient valid physiological data)
    Stage 5: Debug Report Logging
    """

    def __init__(self):
        self.schema_detector = SchemaDetector()
        self.quality_checker = DataQualityChecker(min_valid_ratio=0.70)
        self.missing_data_engine = MissingDataEngine()
        self.registry = FeatureRegistry()
        self.model_registry = ModelRegistry()

        self._register_features()

    def _register_features(self):
        # Activity
        self.registry.register(FeatureDefinition(
            name="activity_mean",
            required_columns=["activity"],
            calculator=lambda df: pd.to_numeric(df.get("activity", df.get("activity_mean", df.get("steps"))), errors="coerce").mean(),
            is_time_series=True,
            description="Mean daily physical activity / steps"
        ))

        # Sleep
        self.registry.register(FeatureDefinition(
            name="sleep_mean",
            required_columns=["sleep"],
            calculator=lambda df: pd.to_numeric(df.get("sleep", df.get("sleep_hours", df.get("sleep_mean"))), errors="coerce").mean(),
            is_time_series=True,
            description="Mean sleep duration in hours"
        ))

        # Resting HR
        self.registry.register(FeatureDefinition(
            name="resting_heart_rate",
            required_columns=["resting_heart_rate"],
            calculator=lambda df: pd.to_numeric(df.get("resting_heart_rate", df.get("resting_hr")), errors="coerce").mean(),
            is_time_series=True,
            description="Mean resting heart rate in bpm"
        ))

        # HRV
        self.registry.register(FeatureDefinition(
            name="hrv",
            required_columns=["hrv"],
            calculator=lambda df: pd.to_numeric(df.get("hrv"), errors="coerce").mean(),
            is_time_series=True,
            description="Heart Rate Variability in ms"
        ))

        # Respiratory rate
        self.registry.register(FeatureDefinition(
            name="respiratory_rate",
            required_columns=["respiratory_rate"],
            calculator=lambda df: pd.to_numeric(df.get("respiratory_rate"), errors="coerce").mean(),
            description="Mean resting respiratory rate"
        ))

        # Recovery score
        self.registry.register(FeatureDefinition(
            name="recovery_score",
            required_columns=["recovery_score"],
            calculator=lambda df: pd.to_numeric(df.get("recovery_score"), errors="coerce").mean(),
            description="WHOOP recovery score"
        ))

        # VO2 Max
        self.registry.register(FeatureDefinition(
            name="vo2_max",
            required_columns=["vo2_max"],
            calculator=lambda df: pd.to_numeric(df.get("vo2_max"), errors="coerce").mean(),
            description="Maximal oxygen consumption"
        ))

        # Body weight and height
        self.registry.register(FeatureDefinition(
            name="weight_kg",
            required_columns=["weight_kg"],
            calculator=lambda df: pd.to_numeric(df.get("weight_kg"), errors="coerce").median(),
            description="Body weight in kg"
        ))
        self.registry.register(FeatureDefinition(
            name="height_cm",
            required_columns=["height_cm"],
            calculator=lambda df: pd.to_numeric(df.get("height_cm"), errors="coerce").median(),
            description="Height in cm"
        ))

        # Blood pressure
        self.registry.register(FeatureDefinition(
            name="systolic_bp",
            required_columns=["systolic_bp"],
            calculator=lambda df: pd.to_numeric(df.get("systolic_bp"), errors="coerce").mean(),
            description="Systolic blood pressure in mmHg"
        ))
        self.registry.register(FeatureDefinition(
            name="diastolic_bp",
            required_columns=["diastolic_bp"],
            calculator=lambda df: pd.to_numeric(df.get("diastolic_bp"), errors="coerce").mean(),
            description="Diastolic blood pressure in mmHg"
        ))

        # Sleep metrics
        self.registry.register(FeatureDefinition(
            name="sleep_efficiency",
            required_columns=["sleep_efficiency"],
            calculator=lambda df: pd.to_numeric(df.get("sleep_efficiency"), errors="coerce").mean(),
            description="Sleep efficiency %"
        ))
        self.registry.register(FeatureDefinition(
            name="rem_sleep_hours",
            required_columns=["rem_sleep_hours"],
            calculator=lambda df: pd.to_numeric(df.get("rem_sleep_hours"), errors="coerce").mean(),
            description="REM sleep hours"
        ))
        self.registry.register(FeatureDefinition(
            name="deep_sleep_hours",
            required_columns=["deep_sleep_hours"],
            calculator=lambda df: pd.to_numeric(df.get("deep_sleep_hours"), errors="coerce").mean(),
            description="Deep sleep hours"
        ))

    def calculate(self, df: pd.DataFrame, chronological_age: Optional[float] = None) -> Dict[str, Any]:
        warnings = []

        if df.empty or len(df.dropna(how="all")) == 0:
            raise ValueError("The uploaded dataset contains no valid observations.")

        # =========================================================================
        # STAGE 1: INGESTION & CENTRALIZED METADATA DETECTION
        # =========================================================================
        schema_report = self.schema_detector.detect(df)

        # Detect Age from CSV or payload
        detected_age, age_col_found = detect_age_column(df)
        if detected_age is not None:
            chronological_age = detected_age

        if chronological_age is None:
            raise ValueError(
                "Missing chronological age. Dataset must contain 'age' or 'chronological_age' with values 1–120."
            )

        try:
            chronological_age = float(chronological_age)
            if not np.isfinite(chronological_age) or chronological_age < 1.0 or chronological_age > 120.0:
                raise ValueError(
                    f"Invalid chronological age ({chronological_age}). Must be between 1 and 120 years."
                )
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid chronological age: {e}")

        # Detect Gender
        gender_detected = detect_gender(df)
        timestamp_col   = schema_report.timestamp_col

        logger.info("--- Stage 1: Ingestion ---")
        logger.info("Age detected      : %.1f yrs  (col='%s')", chronological_age, age_col_found or "payload")
        logger.info("Gender detected   : %s", gender_detected)
        logger.info("Timestamp col     : %s", timestamp_col or "None")
        logger.info("Registered cols   : %s", schema_report.registered_columns)
        logger.info("Unknown cols      : %s", schema_report.unknown_columns)

        # Create normalized df copy with standardized column aliases & units
        df_norm = df.copy()

        # Handle duplicate records
        if df_norm.duplicated().any():
            dup_count = int(df_norm.duplicated().sum())
            logger.info("Removing %d duplicate record(s)", dup_count)
            df_norm = df_norm.drop_duplicates()

        for col in df.columns:
            col_clean = normalize_col_name(col)
            if col_clean in ("step_count", "steps", "daily_steps", "total_steps", "strain") and "activity" not in df_norm.columns:
                df_norm["activity"] = df[col]
            elif col_clean in ("sleep_hours", "sleep_duration", "hours_slept", "total_sleep", "sleep_time", "sleep_time_seconds", "sleeptimeseconds", "sleepanalysis") and "sleep" not in df_norm.columns:
                df_norm["sleep"] = df[col]
            elif col_clean in ("resting_hr", "rhr", "resting_pulse") and "resting_heart_rate" not in df_norm.columns:
                df_norm["resting_heart_rate"] = df[col]
            elif col_clean in ("hrv_baseline", "rmssd", "sdnn", "hrv_ms") and "hrv" not in df_norm.columns:
                df_norm["hrv"] = df[col]
            elif col_clean in ("enmo_mg", "enmo_milligravities") and "enmo" not in df_norm.columns:
                vals = pd.to_numeric(df[col], errors="coerce").dropna()
                if len(vals) > 0 and float(vals.mean()) > 2.0:
                    df_norm["enmo"] = pd.to_numeric(df[col], errors="coerce") / 1000.0
                else:
                    df_norm["enmo"] = df[col]

        # Unit Normalization: convert sleep duration to hours if given in seconds (> 24)
        if "sleep" in df_norm.columns:
            s_vals = pd.to_numeric(df_norm["sleep"], errors="coerce").dropna()
            if len(s_vals) > 0 and float(s_vals.mean()) > 24.0:
                df_norm["sleep"] = pd.to_numeric(df_norm["sleep"], errors="coerce") / 3600.0

        # Activity derivation from ENMO if activity/steps is absent but ENMO is present
        if "activity" not in df_norm.columns and "enmo" in df_norm.columns:
            enmo_vals = pd.to_numeric(df_norm["enmo"], errors="coerce").dropna()
            if len(enmo_vals) > 0:
                # Map ENMO (g) to step-scale activity proxy (ref: ~0.032g baseline = 7500 steps)
                REF_STEPS = 7500.0
                REF_ENMO_MESOR = 0.032
                mean_enmo = float(enmo_vals.mean())
                derived_steps = (mean_enmo / REF_ENMO_MESOR) * REF_STEPS
                df_norm["activity"] = np.clip(derived_steps, 0, 30000)
                logger.info("Derived activity from ENMO (mean=%.4fg -> ~%.0f steps)", mean_enmo, derived_steps)

        # =========================================================================
        # STAGE 2: TIME SERIES CHECK & DIAGNOSTICS
        # =========================================================================
        n_rows = len(df_norm)
        n_days = 0.0
        median_interval_str = "N/A"

        if timestamp_col and timestamp_col in df_norm.columns:
            try:
                ts_series = pd.to_datetime(df_norm[timestamp_col], errors="coerce").dropna().sort_values()
                if len(ts_series) >= 2:
                    n_days = float((ts_series.max() - ts_series.min()).total_seconds() / 86400.0)
                    intervals = ts_series.diff().dropna()
                    if len(intervals) > 0:
                        med_sec = intervals.median().total_seconds()
                        if med_sec >= 3600:
                            median_interval_str = f"{med_sec / 3600.0:.1f} hours"
                        else:
                            median_interval_str = f"{med_sec / 60.0:.1f} min"
            except Exception as ex:
                logger.debug("Timestamp parsing warning: %s", ex)

        logger.info("--- Stage 2: Time-Series Diagnostics ---")
        logger.info("Rows              : %d", n_rows)
        logger.info("Time span         : %.1f days", n_days)
        logger.info("Median interval   : %s", median_interval_str)

        # =========================================================================
        # STAGE 3: FEATURE EXTRACTION & DATA QUALITY CHECK
        # =========================================================================
        features_used: Dict[str, float] = {}
        failed_features: Dict[str, str] = {}
        quality_breakdown = self.quality_checker.inspect(df_norm)

        # Map all standard biometric columns to canonical feature names
        col_to_canonical = {
            "activity": "activity_mean",
            "sleep": "sleep_mean",
            "resting_heart_rate": "resting_heart_rate",
            "hrv": "hrv",
        }

        # Extract direct canonical columns from df_norm
        for norm_col, canon_name in col_to_canonical.items():
            if norm_col in df_norm.columns and canon_name not in features_used:
                series = pd.to_numeric(df_norm[norm_col], errors="coerce").dropna()
                if len(series) > 0:
                    val = float(series.mean())
                    if np.isfinite(val):
                        features_used[canon_name] = round(val, 2)

        # Also extract registered columns from schema report
        for col in schema_report.registered_columns:
            col_clean = normalize_col_name(col)
            matching_feature_name = None

            if any(k in col_clean for k in ("activity", "steps", "step_count", "daily_steps", "strain")):
                matching_feature_name = "activity_mean"
            elif any(k in col_clean for k in ("sleep", "sleep_hours", "sleep_mean", "sleep_duration", "hours_slept")):
                matching_feature_name = "sleep_mean"
            elif any(k in col_clean for k in ("resting_heart_rate", "resting_hr", "rhr", "resting_pulse")):
                matching_feature_name = "resting_heart_rate"
            elif "hrv" in col_clean or "rmssd" in col_clean or "sdnn" in col_clean:
                matching_feature_name = "hrv"
            elif "recovery" in col_clean:
                matching_feature_name = "recovery_score"
            elif any(k in col_clean for k in ("respiratory", "breathing", "respiration")):
                matching_feature_name = "respiratory_rate"
            elif "vo2" in col_clean:
                matching_feature_name = "vo2_max"
            elif "weight" in col_clean:
                matching_feature_name = "weight_kg"
            elif "height" in col_clean:
                matching_feature_name = "height_cm"
            elif "systolic" in col_clean:
                matching_feature_name = "systolic_bp"
            elif "diastolic" in col_clean:
                matching_feature_name = "diastolic_bp"
            elif col_clean in self.registry.get_all():
                matching_feature_name = col_clean

            if not matching_feature_name or matching_feature_name in features_used:
                continue

            q = quality_breakdown.get(col, {})
            if not q.get("usable", False):
                failed_features[matching_feature_name] = f"Quality check failed: {q.get('valid_ratio', 0)*100:.1f}% valid values."
                continue

            try:
                series = pd.to_numeric(df_norm[col], errors="coerce").dropna()
                if len(series) > 0:
                    val = float(series.mean())
                    if matching_feature_name == "sleep_mean" and val > 24.0:
                        val = val / 3600.0
                    if np.isfinite(val):
                        features_used[matching_feature_name] = round(val, 2)
                    else:
                        failed_features[matching_feature_name] = "Non-finite numeric values."
                else:
                    failed_features[matching_feature_name] = "No valid numeric rows found."
            except Exception as ex:
                failed_features[matching_feature_name] = str(ex)

        # =========================================================================
        # STAGE 4: CASCADING MODEL SELECTION & PREDICTION
        # =========================================================================
        # Priority 1: Model A (Full ML)
        # Priority 2: Model B (Non-HRV ML)
        # Priority 3: Validated Fallback Model
        # Priority 4: No Prediction (Safe fail)
        logger.info("--- Stage 4: Cascading Model Selection ---")
        logger.info("  Available features: %s", list(features_used.keys()))

        model_prediction = self.model_registry.select_and_predict(
            chronological_age=chronological_age,
            features=features_used
        )
        logger.info("  Selected model: %s", model_prediction.get("selected_model", "?"))
        logger.info("  Biological age: %s", model_prediction.get("biological_age", "None"))

        # Missing data evaluation
        missing_report = self.missing_data_engine.evaluate(
            schema_report=schema_report,
            extracted_features=features_used,
            feature_quality=quality_breakdown,
            failed_features=failed_features
        )

        # Build human-readable data period string
        if schema_report.data_period_days:
            data_period_str = f"{schema_report.data_period_days} days ({schema_report.row_count} observations)"
        else:
            data_period_str = f"{schema_report.row_count} observation{'s' if schema_report.row_count != 1 else ''}"

        return {
            "can_predict": model_prediction.get("can_predict", True),
            "biological_age": model_prediction["biological_age"],
            "chronological_age": round(chronological_age, 1),
            "prediction_method": model_prediction["selected_model"],
            "model_type": model_prediction.get("model_type", ""),
            "model_description": model_prediction.get("model_description", ""),
            "data_period": data_period_str,
            "features_used": features_used,
            "unavailable": missing_report.unavailable_features,
            "additional_input_detected": missing_report.additional_input_detected,
            "data_quality_percent": missing_report.data_quality_percent,
            "adjustments": model_prediction.get("adjustments", []),
            "explanations": model_prediction.get("explanations", []),
            "warnings": warnings
        }
