"""Entry points for the `biological_age_calculator` action.

Two ways in, both ending at the same cascade
(`packages.biological_age.age_model.ModelRegistry`):

  * `calculate_biological_age` — a CSV/JSON file of daily wearable summaries
    (activity/steps, sleep, resting HR, HRV, ...), or raw tri-axial
    accelerometer (x_g/y_g/z_g) that gets reduced to ENMO first.
  * `calculate_biological_age_from_features` — a features dict directly, no
    file at all, for a client that already has the day's numbers on hand.

Replaces the previous CosinorAge-based implementation, which required a
minute-level ENMO timeseries CSV — a much harder thing for a mobile app to
produce than the daily summaries most wearable integrations already expose.
"""

from __future__ import annotations

import io
import json
import logging
import warnings
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_DATA_KEYS = ("data", "records", "samples", "measurements", "readings")


def _parse_dataframe(contents: bytes, filename: str) -> pd.DataFrame:
    """CSV or JSON bytes -> DataFrame, deriving ENMO from raw x/y/z if present.

    Ported from the source engine's `server.py::_parse_upload`.
    """
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

    col_lower = {c.lower(): c for c in df.columns}
    has_xyz = all(k in col_lower for k in ("x_g", "y_g", "z_g"))
    has_enmo = any("enmo" in c.lower() for c in df.columns)
    if has_xyz and not has_enmo:
        x = pd.to_numeric(df[col_lower["x_g"]], errors="coerce")
        y = pd.to_numeric(df[col_lower["y_g"]], errors="coerce")
        z = pd.to_numeric(df[col_lower["z_g"]], errors="coerce")
        df["enmo"] = (np.sqrt(x**2 + y**2 + z**2) - 1.0).clip(lower=0.0)
        logger.info("derived ENMO from x_g/y_g/z_g (mean=%.5fg)", float(df["enmo"].mean()))

    return df


def _load_model_registry():
    # Deferred: importing age_model.py loads two joblib bundles (~27 MB) off
    # disk. Nothing else in the request path needs that cost paid up front.
    from packages.biological_age.age_model import ModelRegistry

    with warnings.catch_warnings():
        # The bundled models were pickled under a newer scikit-learn than the
        # one pinned here; verified compatible (loads and predicts
        # correctly) — this just silences the per-tree warning noise on
        # every request.
        warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")
        return ModelRegistry()


def calculate_biological_age(
    contents: bytes,
    filename: str,
    chronological_age: float,
    gender: Optional[str] = None,
    return_features: bool = False,
) -> Dict[str, Any]:
    """End-to-end: parse an uploaded file, run the cascade, shape the result."""
    from packages.biological_age.engine import AdaptiveBiologicalAgeEngine

    df = _parse_dataframe(contents, filename)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")
        engine = AdaptiveBiologicalAgeEngine()
        result = engine.calculate(df, chronological_age=chronological_age)

    return _shape_result(result, gender=gender, return_features=return_features)


def calculate_biological_age_from_features(
    chronological_age: float,
    features: Dict[str, Any],
    gender: Optional[str] = None,
) -> Dict[str, Any]:
    """No file: validate a features dict and run the cascade directly."""
    from packages.biological_age.parameter_validator import ParameterValidator

    if not isinstance(features, dict) or not features:
        raise ValueError("features must be a non-empty object.")

    report = ParameterValidator().validate(features)
    if not report["valid"]:
        raise ValueError(next(iter(report["errors"].values())))

    cleaned = report["cleaned_features"]
    if not cleaned:
        raise ValueError("No recognised parameters provided.")

    registry = _load_model_registry()
    prediction = registry.select_and_predict(chronological_age, cleaned)

    result = {
        "can_predict": prediction.get("can_predict", True),
        "biological_age": prediction.get("biological_age"),
        "chronological_age": round(float(chronological_age), 1),
        "prediction_method": prediction.get("selected_model", prediction.get("model_name")),
        "model_type": prediction.get("model_type", ""),
        "model_description": prediction.get("model_description", ""),
        "data_period": "Manual input",
        "features_used": cleaned,
        "unavailable": [],
        "additional_input_detected": [
            {"column": k, "reason": "Not used (no validated model mapping)"}
            for k in report["warnings"].keys()
        ],
        "data_quality_percent": 100.0,
        "adjustments": prediction.get("adjustments", []),
        "explanations": prediction.get("explanations", []),
    }
    return _shape_result(result, gender=gender, return_features=False)


def _shape_result(
    result: Dict[str, Any], *, gender: Optional[str], return_features: bool
) -> Dict[str, Any]:
    """Engine output -> action response. Keeps the field names the app
    already parses (`predicted_biological_age`, `biological_age_advance`)
    alongside the engine's own richer fields."""
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
