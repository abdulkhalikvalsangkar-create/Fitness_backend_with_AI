"""Entry points for the `biological_age_calculator` action.

Single missing-aware Linear Regression model, replacing the previous
two-tier RandomForest cascade (Model A / Model B / rule fallback). One
model, trained with feature-availability masks, handles any combination of
the 12 recognised inputs being present or missing — accuracy degrades
gracefully rather than switching between discrete tiers. See README.md for
the model-provenance caveat; the same caution applies as before.

Two ways in, same as the previous module:

  * `calculate_biological_age` — a CSV/JSON file; one row (or the mean of
    several) is read for the recognised columns.
  * `calculate_biological_age_from_features` — a features dict directly,
    no file at all.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import warnings
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from packages.biological_age.parameter_validator import (
    ALIASES,
    CATEGORICAL_FEATURES,
    FEATURES,
    ParameterValidator,
)

logger = logging.getLogger(__name__)

_DATA_KEYS = ("data", "records", "samples", "measurements", "readings")
_MODEL_PATH = Path(__file__).resolve().parent / "models" / "missing_aware_biological_age_model.joblib"

# Loading the joblib bundle is the only non-trivial cost on this path; one
# process-wide cache means it's paid once per worker, not once per request.
_bundle_cache: Any = None
_cache_lock = threading.Lock()


def _load_bundle() -> Dict[str, Any]:
    global _bundle_cache
    if _bundle_cache is not None:
        return _bundle_cache

    with _cache_lock:
        if _bundle_cache is None:
            import joblib

            with warnings.catch_warnings():
                # Pickled under a newer scikit-learn than the one pinned
                # here; verified compatible (loads and predicts correctly)
                # — this just silences the warning noise on the one load
                # per worker.
                warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")
                _bundle_cache = joblib.load(_MODEL_PATH)
        return _bundle_cache


def _parse_dataframe(contents: bytes, filename: str) -> pd.DataFrame:
    """CSV or JSON bytes -> DataFrame. Unchanged from the previous module."""
    if not contents:
        raise ValueError("Uploaded file is completely empty.")

    if filename.lower().endswith(".json"):
        try:
            data = json.loads(contents.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Malformed JSON: {exc}") from exc

        if isinstance(data, list):
            df = pd.DataFrame(data)
        elif isinstance(data, dict):
            if not data:
                raise ValueError("JSON object is empty.")
            records = next((data[k] for k in _DATA_KEYS if isinstance(data.get(k), list)), None)
            df = pd.DataFrame(records) if records is not None else pd.DataFrame([data])
        else:
            raise ValueError(
                "Unrecognized JSON structure. Expected an array of records or an "
                "object with data fields."
            )
    else:
        try:
            df = pd.read_csv(io.BytesIO(contents))
        except Exception as exc:
            raise ValueError(f"Failed to parse CSV: {exc}") from exc

    if df.empty or len(df.dropna(how="all")) == 0:
        raise ValueError("Dataset has no data rows.")

    return df


def _extract_features_from_dataframe(df: pd.DataFrame) -> Dict[str, Any]:
    """Map recognised columns (by name, case-insensitive, alias-aware) onto
    one feature dict — numeric columns averaged across rows, categorical
    columns take the first non-null value."""
    col_for_feature: Dict[str, str] = {}
    for col in df.columns:
        key = ALIASES.get(str(col).strip().lower(), str(col).strip().lower())
        if key in FEATURES and key not in col_for_feature:
            col_for_feature[key] = col

    extracted: Dict[str, Any] = {}
    for feature, col in col_for_feature.items():
        if feature in CATEGORICAL_FEATURES:
            non_null = df[col].dropna()
            if len(non_null):
                extracted[feature] = str(non_null.iloc[0])
        else:
            numeric = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(numeric):
                extracted[feature] = float(numeric.mean())

    return extracted


def _predict(
    chronological_age: float, features: Dict[str, Any], gender: Optional[str]
) -> Dict[str, Any]:
    """Validate -> build the availability mask -> transform -> predict.

    `age` is always treated as available: it comes from the already-
    validated `chronological_age` parameter, not from `features`, so it
    can't be missing or out of range by the time it gets here. `gender` is
    taken from the top-level parameter only if the caller didn't already
    put it in `features`.
    """
    merged = dict(features or {})
    merged.setdefault("age", chronological_age)
    has_gender = any(str(k).strip().lower() == "gender" for k in merged)
    if gender and not has_gender:
        merged["gender"] = gender

    report = ParameterValidator().validate(merged)
    if not report["valid"]:
        raise ValueError(next(iter(report["errors"].values())))

    cleaned = dict(report["cleaned_features"])
    cleaned["age"] = float(chronological_age)

    # Unlike the model's raw behaviour (it will happily impute every
    # missing value and return something close to the training-set mean
    # age for a completely empty request), this API refuses to answer with
    # nothing but chronological age — the old cascade's fallback tier
    # required at least one real signal (activity or sleep) too. Returning
    # can_predict: False (rather than raising) matches how the old cascade
    # signalled "ran, but not enough data" — the action handler maps that
    # to a 422, not a 400.
    if not any(k != "age" for k in cleaned):
        return {
            "can_predict": False,
            "biological_age": None,
            "chronological_age": round(float(chronological_age), 1),
            "prediction_method": "No Prediction — Insufficient Validated Data",
            "model_type": "",
            "model_description": "",
            "data_period": "Single snapshot",
            "features_used": {},
            "unavailable": [{"name": f, "reason": "Not supplied."} for f in FEATURES if f != "age"],
            "additional_input_detected": [
                {"column": k, "reason": v} for k, v in report["warnings"].items()
            ],
            "data_quality_percent": round(1 / len(FEATURES) * 100.0, 1),
            "adjustments": [],
            "explanations": [
                "Insufficient validated data to compute biological age. "
                "Provide at least one health/wearable measurement besides age."
            ],
        }

    bundle = _load_bundle()
    preprocessor = bundle["preprocessor"]
    model = bundle["model"]

    values = {f: cleaned.get(f, np.nan) for f in FEATURES}
    mask = {f: (1 if f in cleaned else 0) for f in FEATURES}

    values_df = pd.DataFrame([values], columns=FEATURES)
    mask_df = pd.DataFrame([mask], columns=FEATURES)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")
        encoded = preprocessor.transform(values_df)
        combined = np.hstack([np.asarray(encoded), mask_df.to_numpy(dtype=float)])
        raw_pred = float(model.predict(combined)[0])

    bio_age = round(max(1.0, min(120.0, raw_pred)), 1)

    available = [f for f in FEATURES if mask[f] == 1]
    unavailable = [f for f in FEATURES if mask[f] == 0]

    explanations = [
        f"Missing-aware linear model inference: {bio_age:.1f} yrs from "
        f"{len(available)} of {len(FEATURES)} recognised parameters "
        f"({', '.join(available)})."
    ]
    if unavailable:
        explanations.append(f"Not supplied or excluded: {', '.join(unavailable)}.")

    return {
        "can_predict": True,
        "biological_age": bio_age,
        "chronological_age": round(float(chronological_age), 1),
        "prediction_method": "Missing-Aware Linear Regression Model",
        "model_type": "Scikit-Learn Linear Regression (availability-mask features)",
        "model_description": (
            "Single Multiple Linear Regression model trained with feature-"
            "availability masks, so any subset of the 12 recognised "
            "parameters can be used; accuracy degrades gracefully as fewer "
            "are supplied rather than switching between discrete tiers."
        ),
        "data_period": "Single snapshot",
        "features_used": cleaned,
        "unavailable": [
            {"name": f, "reason": "Not supplied, or supplied but invalid/unverified."}
            for f in unavailable
        ],
        "additional_input_detected": [
            {"column": k, "reason": v} for k, v in report["warnings"].items()
        ],
        "data_quality_percent": round(len(available) / len(FEATURES) * 100.0, 1),
        "adjustments": [],
        "explanations": explanations,
    }


def calculate_biological_age(
    contents: bytes,
    filename: str,
    chronological_age: float,
    gender: Optional[str] = None,
    return_features: bool = False,
) -> Dict[str, Any]:
    """End-to-end: parse an uploaded file, run the model, shape the result."""
    df = _parse_dataframe(contents, filename)
    features = _extract_features_from_dataframe(df)
    result = _predict(chronological_age, features, gender)
    return _shape_result(result, gender=gender, return_features=return_features)


def calculate_biological_age_from_features(
    chronological_age: float,
    features: Dict[str, Any],
    gender: Optional[str] = None,
) -> Dict[str, Any]:
    """No file: validate a features dict and run the model directly."""
    if not isinstance(features, dict) or not features:
        raise ValueError("features must be a non-empty object.")

    result = _predict(chronological_age, features, gender)
    return _shape_result(result, gender=gender, return_features=False)


def _shape_result(
    result: Dict[str, Any], *, gender: Optional[str], return_features: bool
) -> Dict[str, Any]:
    """Model output -> action response. Keeps the exact field names the app
    already parses (`predicted_biological_age`, `biological_age_advance`,
    etc.) — unchanged from the previous module's response shape."""
    bio_age = result.get("biological_age")
    chrono_age = result.get("chronological_age")

    shaped: Dict[str, Any] = {
        "can_predict": result.get("can_predict", True),
        "predicted_biological_age": bio_age,
        "chronological_age": chrono_age,
        "gender": gender,
        "biological_age_advance": (
            round(bio_age - chrono_age, 2)
            if isinstance(bio_age, (int, float)) and isinstance(chrono_age, (int, float))
            else None
        ),
        "prediction_method": result.get("prediction_method"),
        "model_type": result.get("model_type"),
        "model_description": result.get("model_description"),
        "data_period": result.get("data_period"),
        "features_used": result.get("features_used", {}),
        "unavailable": result.get("unavailable", []),
        "additional_input_detected": result.get("additional_input_detected", []),
        "data_quality_percent": result.get("data_quality_percent"),
        "adjustments": result.get("adjustments", []),
        "explanations": result.get("explanations", []),
    }

    if return_features:
        shaped["features"] = result.get("features_used", {})

    return shaped
