"""A restaurant scan resolves without a place_id."""

import unittest
from unittest import mock

from packages.restaurant import news, places
from packages.restaurant.analyzer import RestaurantAnalyzer


def search_text_response(*found: dict) -> mock.Mock:
    response = mock.Mock(status_code=200)
    response.json.return_value = {"places": list(found)}
    return response


class RestaurantResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = mock.Mock()
        for target in (
            mock.patch.object(places, "get_broker", return_value=self.broker),
            mock.patch.object(places, "available", return_value=True),
            mock.patch.object(news, "search", return_value=([], False)),  # never the live feed
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_a_name_alone_is_looked_up(self) -> None:
        self.broker.post.return_value = search_text_response(
            {"id": "P1", "displayName": {"text": "Cafe Nine"}, "formattedAddress": "1 Main Rd"}
        )
        report = RestaurantAnalyzer().analyze("cafe nine")
        self.assertTrue(report.place.resolved)
        self.assertEqual((report.place.place_id, report.place.name), ("P1", "Cafe Nine"))
        self.assertTrue(report.place.branch_confirmed)
        self.assertIn("places:searchText", self.broker.post.call_args.args[0])

    def test_provider_down_falls_back_to_the_typed_name(self) -> None:
        self.broker.post.side_effect = places.FetchError("down")
        report = RestaurantAnalyzer().analyze("Cafe Nine")
        self.assertTrue(report.place.resolved)
        self.assertEqual(report.place.name, "Cafe Nine")
        self.assertFalse(report.place.branch_confirmed)
        self.assertIn("could not confirm which branch", report.summary_line())

    def test_no_key_falls_back_to_the_typed_name(self) -> None:
        with mock.patch.object(places, "available", return_value=False):
            report = RestaurantAnalyzer().analyze("Cafe Nine")
        self.assertTrue(report.place.resolved)
        self.broker.post.assert_not_called()

    def test_no_match_falls_back_to_the_typed_name(self) -> None:
        self.broker.post.return_value = search_text_response()
        report = RestaurantAnalyzer().analyze("zzz")
        self.assertEqual((report.place.name, report.place.place_id), ("zzz", None))

    def test_an_explicit_place_id_still_wins(self) -> None:
        response = mock.Mock(status_code=200)
        response.json.return_value = {"id": "P2", "displayName": {"text": "Real Name"}}
        self.broker.get.return_value = response
        report = RestaurantAnalyzer().analyze("typed", place_id="P2")
        self.assertEqual(report.place.name, "Real Name")
        self.broker.post.assert_not_called()

    def test_nothing_checked_is_not_reported_as_no_adverse_findings(self) -> None:
        with mock.patch.object(places, "available", return_value=False):
            line = RestaurantAnalyzer().analyze("Mc Donald's").summary_line()
        self.assertNotIn("No adverse findings", line)
        self.assertIn("can't say anything", line)
        self.assertIn("could not confirm which branch", line)

    def test_blank_query_stays_unresolved(self) -> None:
        self.assertFalse(RestaurantAnalyzer().analyze("  ").place.resolved)


if __name__ == "__main__":
    unittest.main()
