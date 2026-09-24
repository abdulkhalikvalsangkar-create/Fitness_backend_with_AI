"""Smoke tests for the biological_age_calculator action and module.

No database and no network calls: the module is pure computation over an
in-memory features dict or an in-memory file, and the action handler only
touches the database on the attachment_id path, which these tests avoid.
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("JWT_SECRET", "test-secret-for-auth-tests-32-bytes-minimum")
os.environ.setdefault("JWT_AUDIENCE", "fitness-api")
os.environ.setdefault("JWT_ISSUER", "movenetics-api")

from fastapi import HTTPException

from apps.api.actions import handle_biological_age_calculator
from apps.api.security import Principal
from packages.biological_age import (
    calculate_biological_age,
    calculate_biological_age_from_features,
)

PRINCIPAL = Principal(user_id="test-user")

FULL_FEATURES = {
    "activity_mean": 8000,
    "sleep_hours": 7.5,
    "resting_heart_rate": 60,
    "hrv": 55,
}

CSV_BYTES = (
    b"timestamp,age,activity,sleep,resting_heart_rate,hrv\n"
    b"2026-08-01 08:00:00,24,12000,8.2,48,82\n"
    b"2026-08-02 08:00:00,24,12400,8.4,45,85\n"
)


class BiologicalAgeModuleTests(unittest.TestCase):
    def test_features_dict_selects_model_a_when_hrv_present(self) -> None:
        result = calculate_biological_age_from_features(30, FULL_FEATURES)
        self.assertTrue(result["can_predict"])
        self.assertIn("Model A", result["prediction_method"])
        self.assertIsInstance(result["predicted_biological_age"], float)

    def test_features_dict_falls_back_to_model_b_without_hrv(self) -> None:
        features = {k: v for k, v in FULL_FEATURES.items() if k != "hrv"}
        result = calculate_biological_age_from_features(30, features)
        self.assertTrue(result["can_predict"])
        self.assertIn("Model B", result["prediction_method"])

    def test_features_dict_uses_rule_fallback_with_activity_only(self) -> None:
        result = calculate_biological_age_from_features(50, {"activity_mean": 9000})
        self.assertTrue(result["can_predict"])
        self.assertIn("Fallback", result["prediction_method"])

    def test_unrecognised_features_raise(self) -> None:
        with self.assertRaises(ValueError):
            calculate_biological_age_from_features(50, {"not_a_real_parameter": 1})

    def test_out_of_range_feature_raises(self) -> None:
        with self.assertRaises(ValueError):
            calculate_biological_age_from_features(50, {"resting_heart_rate": 999})

    def test_file_input_parses_csv_and_predicts(self) -> None:
        result = calculate_biological_age(CSV_BYTES, "daily.csv", chronological_age=24)
        self.assertTrue(result["can_predict"])
        self.assertIn("Model A", result["prediction_method"])


class BiologicalAgeActionTests(unittest.TestCase):
    def test_features_path_end_to_end(self) -> None:
        body = {"chronological_age": 30, "gender": "male", "features": FULL_FEATURES}
        result = handle_biological_age_calculator(body, PRINCIPAL, None)
        self.assertEqual(result["action"], "biological_age_calculator")
        self.assertTrue(result["can_predict"])
        self.assertIsInstance(result["predicted_biological_age"], float)

    def test_file_attachment_path_end_to_end(self) -> None:
        body = {
            "chronological_age": 24,
            "attachments": [{"bytes": CSV_BYTES, "filename": "daily.csv"}],
        }
        result = handle_biological_age_calculator(body, PRINCIPAL, None)
        self.assertEqual(result["action"], "biological_age_calculator")
        self.assertTrue(result["can_predict"])

    def test_missing_input_is_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            handle_biological_age_calculator({"chronological_age": 30}, PRINCIPAL, None)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_insufficient_data_is_422(self) -> None:
        body = {"chronological_age": 30, "features": {"vo2_max": 45}}
        with self.assertRaises(HTTPException) as ctx:
            handle_biological_age_calculator(body, PRINCIPAL, None)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_invalid_age_is_400(self) -> None:
        body = {"chronological_age": 500, "features": FULL_FEATURES}
        with self.assertRaises(HTTPException) as ctx:
            handle_biological_age_calculator(body, PRINCIPAL, None)
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
