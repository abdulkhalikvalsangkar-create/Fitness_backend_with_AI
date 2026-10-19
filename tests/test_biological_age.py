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
    "height_cm": 178,
    "weight_kg": 75,
    "hrv": 65,
    "resting_heart_rate": 58,
    "calories_burned": 2800,
    "activity_type": "Running",
    "fitness_level": "Advanced",
    "recovery_score": 70,
    "day_strain": 10,
    "sleep_hours": 7.5,
}

CSV_BYTES = (
    b"age,gender,height_cm,weight_kg,hrv,resting_heart_rate,calories_burned,"
    b"activity_type,fitness_level,recovery_score,day_strain,sleep_hours\n"
    b"24,Female,165,60,70,55,2600,Yoga,Intermediate,75,8,7.8\n"
)


class BiologicalAgeModuleTests(unittest.TestCase):
    def test_full_features_predicts(self) -> None:
        result = calculate_biological_age_from_features(30, FULL_FEATURES, gender="Male")
        self.assertTrue(result["can_predict"])
        self.assertIsInstance(result["predicted_biological_age"], float)
        self.assertEqual(result["data_quality_percent"], 100.0)

    def test_partial_features_still_predicts_with_lower_data_quality(self) -> None:
        result = calculate_biological_age_from_features(
            30, {"sleep_hours": 7.5, "resting_heart_rate": 58}
        )
        self.assertTrue(result["can_predict"])
        self.assertLess(result["data_quality_percent"], 100.0)

    def test_alias_keys_are_recognised(self) -> None:
        # "rhr"/"sleep"/"weight"/"height" are old-module-style synonyms that
        # should still map onto this model's canonical feature names.
        result = calculate_biological_age_from_features(
            30, {"rhr": 58, "sleep": 7.5, "weight": 75, "height": 178}
        )
        self.assertTrue(result["can_predict"])

    def test_unknown_categorical_value_does_not_crash(self) -> None:
        result = calculate_biological_age_from_features(
            30, {"sleep_hours": 7.5, "activity_type": "SomeBrandNewActivity"}
        )
        self.assertTrue(result["can_predict"])

    def test_unrecognised_parameter_is_a_warning_not_an_error(self) -> None:
        result = calculate_biological_age_from_features(
            30, {"sleep_hours": 7.5, "not_a_real_parameter": 123}
        )
        self.assertTrue(result["can_predict"])
        warned = {w["column"] for w in result["additional_input_detected"]}
        self.assertIn("not_a_real_parameter", warned)

    def test_out_of_range_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            calculate_biological_age_from_features(30, {"resting_heart_rate": 999})

    def test_empty_features_raises(self) -> None:
        with self.assertRaises(ValueError):
            calculate_biological_age_from_features(30, {})

    def test_only_unrecognised_keys_is_insufficient_data(self) -> None:
        result = calculate_biological_age_from_features(30, {"totally_unknown_field": 1})
        self.assertFalse(result["can_predict"])

    def test_file_input_parses_csv_and_predicts(self) -> None:
        result = calculate_biological_age(CSV_BYTES, "daily.csv", chronological_age=24)
        self.assertTrue(result["can_predict"])
        self.assertEqual(result["data_quality_percent"], 100.0)


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
        body = {"chronological_age": 30, "features": {"totally_unknown": 1}}
        with self.assertRaises(HTTPException) as ctx:
            handle_biological_age_calculator(body, PRINCIPAL, None)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_out_of_range_value_is_400(self) -> None:
        body = {"chronological_age": 30, "features": {"resting_heart_rate": 999}}
        with self.assertRaises(HTTPException) as ctx:
            handle_biological_age_calculator(body, PRINCIPAL, None)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_invalid_age_is_400(self) -> None:
        body = {"chronological_age": 500, "features": FULL_FEATURES}
        with self.assertRaises(HTTPException) as ctx:
            handle_biological_age_calculator(body, PRINCIPAL, None)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_gender_flows_through_to_the_model(self) -> None:
        # Unlike the previous module, gender is an actual model feature here
        # (one-hot encoded), not just an echoed-back field.
        body = {"chronological_age": 30, "gender": "Male", "features": {"sleep_hours": 7.5}}
        result = handle_biological_age_calculator(body, PRINCIPAL, None)
        self.assertEqual(result["gender"], "Male")
        self.assertIn("gender", result["features_used"])


if __name__ == "__main__":
    unittest.main()
