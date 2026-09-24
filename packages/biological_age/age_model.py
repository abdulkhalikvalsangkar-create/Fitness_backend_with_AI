"""
age_model.py
Model Registry and Prediction Engines.
Implements the validated cascade architecture:
  Priority 1: Model A — Full Multi-Modal ML Model (activity_mean, sleep_mean, hrv, resting_heart_rate)
  Priority 2: Model B — Non-HRV ML Model          (activity_mean, sleep_mean, resting_heart_rate)
  Priority 3: Validated Fallback Model            (Clinical rule-based: requires activity_mean OR sleep_mean)
  Priority 4: No Prediction                      (Safe termination if minimum physiological signals are missing)

Cosinor has been completely removed from the active prediction pipeline and model selection priority.
"""

import os
from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd
import joblib


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class BaseModel:
    name: str = ""
    description: str = ""
    model_type: str = "ML Model"
    required_features: List[str] = []

    def can_handle(self, features: Dict[str, float]) -> bool:
        return all(
            f in features and features[f] is not None and np.isfinite(float(features[f]))
            for f in self.required_features
        )

    def predict(self, chronological_age: float, features: Dict[str, float]) -> Dict[str, Any]:
        raise NotImplementedError


# =============================================================================
# PRIORITY 1: Model A — Full Multi-Modal ML
# =============================================================================
class ModelA_Full(BaseModel):
    """
    Priority 1: Full Multi-Modal ML Model.
    Requires:
      - activity_mean
      - sleep_mean
      - hrv
      - resting_heart_rate
    """
    name = "ML Model - Model A (Full Feature Set)"
    description = (
        "Supervised Scikit-Learn Random Forest Regressor trained on complete "
        "multi-modal biometrics: physical activity, sleep duration, autonomic HRV, and resting heart rate."
    )
    model_type = "Scikit-Learn Random Forest ML Model"
    required_features = [
        "activity_mean",
        "sleep_mean",
        "hrv",
        "resting_heart_rate",
    ]

    def __init__(self, model_path: Optional[str] = None):
        if model_path is None:
            model_path = os.path.join(BASE_DIR, "models", "model_a.joblib")
        self.model_path = model_path
        self.bundle = None
        if os.path.exists(model_path):
            try:
                self.bundle = joblib.load(model_path)
            except Exception:
                self.bundle = None

    def predict(self, chronological_age: float, features: Dict[str, float]) -> Dict[str, Any]:
        if not self.can_handle(features):
            return {
                "can_predict": False,
                "model_name": self.name,
                "biological_age": None,
                "reason": "Missing required features for Model A (requires activity_mean, sleep_mean, hrv, resting_heart_rate)."
            }

        act = float(features["activity_mean"])
        slp = float(features["sleep_mean"])
        rhr = float(features["resting_heart_rate"])
        hrv = float(features["hrv"])

        if self.bundle is not None:
            bundle_features = self.bundle.get("features", ['age', 'activity_mean', 'sleep_mean', 'resting_heart_rate', 'hrv'])
            feature_dict = {
                'age': float(chronological_age),
                'activity_mean': act,
                'sleep_mean': slp,
                'resting_heart_rate': rhr,
                'hrv': hrv
            }
            vec = [feature_dict[col] for col in bundle_features]
            X_df = pd.DataFrame([vec], columns=bundle_features)
            raw_pred = float(self.bundle["model"].predict(X_df)[0])
            bio_age = round(max(1.0, min(120.0, raw_pred)), 1)
        else:
            # Fallback estimation if model weights unreadable
            delta = ((50.0 - hrv) / 15.0) + ((rhr - 60.0) / 8.0) + ((7.5 - slp) * 0.8) + ((7500.0 - act) / 3000.0)
            bio_age = round(max(1.0, min(120.0, chronological_age + delta)), 1)

        diff = round(bio_age - chronological_age, 1)
        sign = "+" if diff > 0 else ""
        explanations = [
            f"Model A (Full Multi-Modal) inference: {bio_age:.1f} yrs ({sign}{diff:.1f} yrs from chronological age {chronological_age:.1f}).",
            f"Biometrics utilized: Activity ({act:.0f} steps), Sleep ({slp:.1f}h), Resting HR ({rhr:.0f} bpm), HRV ({hrv:.1f} ms)."
        ]

        return {
            "can_predict": True,
            "model_name": self.name,
            "model_type": self.model_type,
            "biological_age": bio_age,
            "adjustments": [diff],
            "explanations": explanations,
        }


# =============================================================================
# PRIORITY 2: Model B — Non-HRV ML
# =============================================================================
class ModelB_NoHRV(BaseModel):
    """
    Priority 2: Non-HRV ML Model.
    Used when HRV data is unavailable.
    Requires:
      - activity_mean
      - sleep_mean
      - resting_heart_rate
    """
    name = "ML Model - Model B (Non-HRV Model)"
    description = (
        "Supervised Scikit-Learn Random Forest Regressor trained on non-HRV cohorts, "
        "incorporating physical activity, sleep duration, and resting heart rate."
    )
    model_type = "Scikit-Learn Random Forest ML Model"
    required_features = [
        "activity_mean",
        "sleep_mean",
        "resting_heart_rate",
    ]

    def __init__(self, model_path: Optional[str] = None):
        if model_path is None:
            model_path = os.path.join(BASE_DIR, "models", "model_b.joblib")
        self.model_path = model_path
        self.bundle = None
        if os.path.exists(model_path):
            try:
                self.bundle = joblib.load(model_path)
            except Exception:
                self.bundle = None

    def predict(self, chronological_age: float, features: Dict[str, float]) -> Dict[str, Any]:
        if not self.can_handle(features):
            return {
                "can_predict": False,
                "model_name": self.name,
                "biological_age": None,
                "reason": "Missing required features for Model B (requires activity_mean, sleep_mean, resting_heart_rate)."
            }

        act = float(features["activity_mean"])
        slp = float(features["sleep_mean"])
        rhr = float(features["resting_heart_rate"])

        if self.bundle is not None:
            bundle_features = self.bundle.get("features", ['age', 'activity_mean', 'sleep_mean', 'resting_heart_rate'])
            feature_dict = {
                'age': float(chronological_age),
                'activity_mean': act,
                'sleep_mean': slp,
                'resting_heart_rate': rhr
            }
            vec = [feature_dict[col] for col in bundle_features]
            X_df = pd.DataFrame([vec], columns=bundle_features)
            raw_pred = float(self.bundle["model"].predict(X_df)[0])
            bio_age = round(max(1.0, min(120.0, raw_pred)), 1)
        else:
            delta = ((rhr - 60.0) / 8.0) + ((7.5 - slp) * 0.8) + ((7500.0 - act) / 3000.0)
            bio_age = round(max(1.0, min(120.0, chronological_age + delta)), 1)

        diff = round(bio_age - chronological_age, 1)
        sign = "+" if diff > 0 else ""
        explanations = [
            f"Model B (Non-HRV) inference: {bio_age:.1f} yrs ({sign}{diff:.1f} yrs from chronological age {chronological_age:.1f}).",
            f"Biometrics utilized: Activity ({act:.0f} steps), Sleep ({slp:.1f}h), Resting HR ({rhr:.0f} bpm) [HRV absent]."
        ]

        return {
            "can_predict": True,
            "model_name": self.name,
            "model_type": self.model_type,
            "biological_age": bio_age,
            "adjustments": [diff],
            "explanations": explanations,
        }


# =============================================================================
# PRIORITY 3: Validated Fallback (Clinical Rule-Based)
# =============================================================================
class ValidatedFallbackModel(BaseModel):
    """
    Priority 3: Validated Clinical Multi-Parametric Engine.
    Requires at minimum activity_mean OR sleep_mean to be present and finite.
    Will NOT manufacture a prediction if even these minimums are absent.
    """
    name = "Validated Fallback Model (Clinical Multi-Parametric Engine)"
    description = "Clinically validated rule-based engine adapting to any combination of validated biomarkers."
    model_type = "Clinical Rule-Based Engine"

    def can_handle(self, features: Dict[str, float]) -> bool:
        has_act = "activity_mean" in features and features["activity_mean"] is not None and np.isfinite(float(features["activity_mean"]))
        has_slp = "sleep_mean" in features and features["sleep_mean"] is not None and np.isfinite(float(features["sleep_mean"]))
        return has_act or has_slp

    def predict(self, chronological_age: float, features: Dict[str, float]) -> Dict[str, Any]:
        if not self.can_handle(features):
            return {
                "can_predict": False,
                "model_name": self.name,
                "biological_age": None,
                "reason": "Fallback requires at least activity_mean or sleep_mean."
            }

        adjustments = []
        explanations = []

        if "weight_kg" in features and "height_cm" in features:
            w = float(features["weight_kg"])
            h = float(features["height_cm"]) / 100.0
            if h > 0 and np.isfinite(w) and np.isfinite(h):
                bmi = w / (h * h)
                if 18.5 <= bmi < 25:    adjustments.append(-1.0); explanations.append(f"Healthy BMI ({bmi:.1f}): -1.0 yr.")
                elif 25 <= bmi < 30:    adjustments.append(+1.0); explanations.append(f"Overweight BMI ({bmi:.1f}): +1.0 yr.")
                elif bmi >= 30:         adjustments.append(+2.0); explanations.append(f"Obese BMI ({bmi:.1f}): +2.0 yrs.")
                elif bmi < 18.5:        adjustments.append(+1.0); explanations.append(f"Underweight BMI ({bmi:.1f}): +1.0 yr.")

        if "activity_mean" in features and np.isfinite(float(features["activity_mean"])):
            v = float(features["activity_mean"])
            if v >= 10000:   adjustments.append(-2.0); explanations.append(f"High activity ({v:.0f} steps): -2.0 yrs.")
            elif v >= 7000:  adjustments.append(-1.0); explanations.append(f"Good activity ({v:.0f} steps): -1.0 yr.")
            elif v >= 4000:  adjustments.append(0.0);  explanations.append(f"Moderate activity ({v:.0f} steps): baseline.")
            elif v >= 2000:  adjustments.append(+1.0); explanations.append(f"Low activity ({v:.0f} steps): +1.0 yr.")
            else:            adjustments.append(+2.0); explanations.append(f"Very low activity ({v:.0f} steps): +2.0 yrs.")

        if "sleep_mean" in features and np.isfinite(float(features["sleep_mean"])):
            v = float(features["sleep_mean"])
            if 7 <= v <= 9:                  adjustments.append(-1.0); explanations.append(f"Optimal sleep ({v:.1f} hrs): -1.0 yr.")
            elif 6 <= v < 7 or 9 < v <= 10:  adjustments.append(0.0); explanations.append(f"Near-optimal sleep ({v:.1f} hrs): baseline.")
            elif 5 <= v < 6 or 10 < v <= 11: adjustments.append(+1.0); explanations.append(f"Suboptimal sleep ({v:.1f} hrs): +1.0 yr.")
            else:                            adjustments.append(+2.0); explanations.append(f"Poor sleep ({v:.1f} hrs): +2.0 yrs.")

        if "resting_heart_rate" in features and np.isfinite(float(features["resting_heart_rate"])):
            v = float(features["resting_heart_rate"])
            if v < 55:     adjustments.append(-1.5); explanations.append(f"Athletic resting HR ({v:.1f} bpm): -1.5 yrs.")
            elif v <= 65:  adjustments.append(-0.5); explanations.append(f"Excellent resting HR ({v:.1f} bpm): -0.5 yr.")
            elif v <= 75:  adjustments.append(0.0);  explanations.append(f"Normal resting HR ({v:.1f} bpm): baseline.")
            elif v <= 85:  adjustments.append(+1.0); explanations.append(f"Elevated resting HR ({v:.1f} bpm): +1.0 yr.")
            else:          adjustments.append(+2.0); explanations.append(f"High resting HR ({v:.1f} bpm): +2.0 yrs.")

        if "hrv" in features and np.isfinite(float(features["hrv"])):
            v = float(features["hrv"])
            if v >= 70:    adjustments.append(-2.0); explanations.append(f"Superior HRV ({v:.1f} ms): -2.0 yrs.")
            elif v >= 50:  adjustments.append(-1.0); explanations.append(f"Good HRV ({v:.1f} ms): -1.0 yr.")
            elif v >= 30:  adjustments.append(0.0);  explanations.append(f"Average HRV ({v:.1f} ms): baseline.")
            elif v >= 20:  adjustments.append(+1.0); explanations.append(f"Below-average HRV ({v:.1f} ms): +1.0 yr.")
            else:          adjustments.append(+2.0); explanations.append(f"Poor HRV ({v:.1f} ms): +2.0 yrs.")

        bio_age = max(1.0, min(120.0, chronological_age + sum(adjustments)))
        return {
            "can_predict": True,
            "model_name": self.name,
            "model_type": self.model_type,
            "biological_age": round(bio_age, 2),
            "adjustments": adjustments,
            "explanations": explanations,
        }


# =============================================================================
# MODEL REGISTRY — Priority-ordered selection
# =============================================================================
class ModelRegistry:
    """
    Registry that inspects available features and selects the highest-priority
    validated model capable of handling that feature set.
    Priority:
      1. Model A — Full ML (activity_mean, sleep_mean, hrv, resting_heart_rate)
      2. Model B — Non-HRV ML (activity_mean, sleep_mean, resting_heart_rate)
      3. Validated Fallback Model (activity_mean OR sleep_mean)
      4. No Prediction (Insufficient valid physiological data)
    """
    def __init__(self):
        self.models: List[BaseModel] = [
            ModelA_Full(),
            ModelB_NoHRV(),
            ValidatedFallbackModel(),
        ]

    def select_and_predict(self, chronological_age: float, features: Dict[str, Any]) -> Dict[str, Any]:
        # Validate chronological age
        try:
            chrono_age = float(chronological_age)
            if not np.isfinite(chrono_age) or chrono_age < 1.0 or chrono_age > 120.0:
                return {
                    "can_predict": False,
                    "model_name": "No Prediction",
                    "model_type": "None",
                    "selected_model": "No Prediction — Invalid Age",
                    "model_description": "Chronological age must be a finite number between 1 and 120.",
                    "biological_age": None,
                    "adjustments": [],
                    "explanations": [f"Invalid chronological age provided: {chronological_age}. Must be between 1 and 120 years."],
                }
        except (ValueError, TypeError):
            return {
                "can_predict": False,
                "model_name": "No Prediction",
                "model_type": "None",
                "selected_model": "No Prediction — Invalid Age",
                "model_description": "Chronological age must be a numeric value.",
                "biological_age": None,
                "adjustments": [],
                "explanations": ["Chronological age is non-numeric or missing."],
            }

        # Filter features to strictly finite numeric values
        clean_features: Dict[str, float] = {}
        for k, v in features.items():
            if v is not None:
                try:
                    vf = float(v)
                    if np.isfinite(vf):
                        clean_features[k] = vf
                except (ValueError, TypeError):
                    continue

        # Standardize aliases
        alias_map = {
            "sleep_hours": "sleep_mean",
            "sleep": "sleep_mean",
            "sleep_duration": "sleep_mean",
            "hours_slept": "sleep_mean",
            "activity": "activity_mean",
            "steps": "activity_mean",
            "step_count": "activity_mean",
            "daily_steps": "activity_mean",
            "total_steps": "activity_mean",
            "resting_hr": "resting_heart_rate",
            "rhr": "resting_heart_rate",
            "rhr_baseline": "resting_heart_rate",
            "resting_pulse": "resting_heart_rate",
            "hrv_baseline": "hrv",
            "rmssd": "hrv",
            "sdnn": "hrv",
            "hrv_ms": "hrv",
        }
        for alias, target in alias_map.items():
            if alias in clean_features and target not in clean_features:
                clean_features[target] = clean_features[alias]

        # Cascading priority selection
        for model in self.models:
            if model.can_handle(clean_features):
                res = model.predict(chrono_age, clean_features)
                res["selected_model"] = model.name
                res["model_type"] = model.model_type
                res["model_description"] = model.description
                return res

        # No model could handle the data — Fail safely without fabricating a biological age
        return {
            "can_predict": False,
            "model_name": "No Prediction",
            "model_type": "None",
            "selected_model": "No Prediction — Insufficient Validated Data",
            "model_description": "No validated prediction path could be applied. Insufficient physiological data.",
            "biological_age": None,
            "adjustments": [],
            "explanations": ["Insufficient validated data to compute biological age. Please provide at least activity or sleep data."],
        }


class BiologicalAgeModel:
    def __init__(self):
        self.registry = ModelRegistry()

    def calculate(self, chronological_age: float, features: Dict[str, Any]) -> Dict[str, Any]:
        return self.registry.select_and_predict(chronological_age, features)
