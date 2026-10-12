from __future__ import annotations

import unittest
from datetime import date, timedelta

from packages.health.scores import build_dashboard, recovery_score, sleep_score, strain_score


class HealthScoreTests(unittest.TestCase):
    def test_sleep_duration_score(self) -> None:
        self.assertEqual(sleep_score(8), 100.0)
        self.assertEqual(sleep_score(6), 80.0)
        self.assertIsNone(sleep_score(None))

    def test_strain_is_on_twenty_one_point_scale(self) -> None:
        self.assertEqual(strain_score(steps=12_000, active_minutes=90, calories=700), 21.0)
        self.assertEqual(strain_score(steps=0), 0.0)
        self.assertIsNone(strain_score())

    def test_recovery_reweights_when_hrv_is_missing(self) -> None:
        value, inputs = recovery_score(sleep=100, strain=10.5, rhr=60, rhr_baseline=60)
        self.assertAlmostEqual(value, 82.0, places=1)
        self.assertNotIn("hrv_vs_baseline", inputs)

    def test_dashboard_prefers_synced_scores_and_returns_history_and_body_fat(self) -> None:
        today = date(2026, 10, 12)
        points = [
            {"metric": "recovery", "value": 82, "unit": "%", "measured_on": today, "source": "health_connect"},
            {"metric": "strain", "value": 13.4, "unit": "score", "measured_on": today, "source": "wearable"},
            {"metric": "sleep", "value": 91, "unit": "%", "measured_on": today, "source": "wearable"},
            {"metric": "body_fat", "value": 18.7, "unit": "%", "measured_on": today - timedelta(days=2), "source": "manual"},
        ]
        result = build_dashboard(on=today, metric_points=points, activity_days={})
        self.assertEqual(result["recovery"]["value"], 82.0)
        self.assertEqual(result["strain"]["value"], 13.4)
        self.assertEqual(result["sleep"]["value"], 91.0)
        self.assertEqual(len(result["recovery_history"]), 7)
        self.assertEqual(result["body_fat"]["value"], 18.7)

    def test_dashboard_derives_scores_without_hrv(self) -> None:
        today = date(2026, 10, 12)
        yesterday = today - timedelta(days=1)
        points = [
            {"metric": "sleep_hours", "value": 8, "measured_on": today, "source": "health_connect"},
            {"metric": "steps", "value": 8_000, "measured_on": today, "source": "health_connect"},
            {"metric": "strain", "value": 10, "measured_on": yesterday, "source": "wearable"},
        ]
        result = build_dashboard(on=today, metric_points=points, activity_days={})
        self.assertEqual(result["sleep"]["value"], 100.0)
        self.assertIsNotNone(result["strain"]["value"])
        self.assertIsNotNone(result["recovery"]["value"])
        self.assertEqual(result["recovery"]["source"], "derived")

    def test_sleep_metric_in_hours_is_not_mistaken_for_a_score(self) -> None:
        today = date(2026, 10, 12)
        points = [{"metric": "sleep", "value": 8, "unit": "h", "measured_on": today, "source": "health_connect"}]
        result = build_dashboard(on=today, metric_points=points, activity_days={})
        self.assertEqual(result["sleep"]["value"], 100.0)
        self.assertEqual(result["sleep"]["source"], "derived")


if __name__ == "__main__":
    unittest.main()
