"""News and controversy stage of the restaurant investigation."""

import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

from packages.restaurant import news, places
from packages.restaurant.analyzer import FindingKind, FindingScope, RestaurantAnalyzer


def when(days_ago: int) -> str:
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))


def item(title: str, outlet: str = "India Today", days_ago: int = 30) -> str:
    return (
        f"<item><title>{title} - {outlet}</title><link>https://news.example/{abs(hash(title))}</link>"
        f"<pubDate>{when(days_ago)}</pubDate><source url='https://x'>{outlet}</source></item>"
    )


def feed(*items: str) -> mock.Mock:
    body = ("<?xml version='1.0'?><rss><channel>" + "".join(items) + "</channel></rss>").encode()
    return mock.Mock(status_code=200, content=body)


class NewsStageTests(unittest.TestCase):
    def setUp(self) -> None:
        news._cache.clear()
        self.addCleanup(news._cache.clear)
        self.broker = mock.Mock()
        for target in (
            mock.patch.object(news, "get_broker", return_value=self.broker),
            mock.patch.object(places, "available", return_value=False),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_incident_headlines_naming_the_restaurant_are_findings(self) -> None:
        self.broker.get.return_value = feed(
            item("Mumbai FDA Drive: Food Licenses Of McDonald's Colaba Suspended Over Hygiene Lapses"),
            item("McDonald's launches new burger"),
            item("Dominos hit by food poisoning claims"),
        )
        report = RestaurantAnalyzer().analyze("Mc Donald's")
        self.assertIn("news", report.stages_completed)
        self.assertEqual(len(report.findings), 1)
        finding = report.findings[0]
        self.assertEqual((finding.kind, finding.scope, finding.verified), (FindingKind.CLOSURE, FindingScope.BRAND, False))
        self.assertTrue(finding.summary.startswith("News report:"))
        self.assertEqual(finding.source, "India Today")
        self.assertIsNotNone(finding.occurred_on)
        self.assertNotIn("No adverse findings", report.summary_line())
        self.assertIn("brand generally", report.summary_line())

    def test_a_city_typed_after_the_name_is_ignored_when_matching(self) -> None:
        self.broker.get.return_value = feed(item("Pizza Hut outlet sealed after cockroach found"))
        report = RestaurantAnalyzer().analyze("Pizza Hut, Mumbai")
        self.assertEqual(len(report.findings), 1)

    def test_a_generic_word_does_not_match(self) -> None:
        self.broker.get.return_value = feed(item("Pizza shop owner arrested over fine dispute"))
        report = RestaurantAnalyzer().analyze("Pizza Hut Mumbai")
        self.assertEqual(report.findings, [])

    def test_clean_feed_means_news_checked_and_nothing_found(self) -> None:
        self.broker.get.return_value = feed(item("McDonald's opens new store"))
        report = RestaurantAnalyzer().analyze("McDonald's")
        self.assertIn("news", report.stages_completed)
        self.assertEqual(report.findings, [])
        self.assertIn("No adverse findings", report.summary_line())

    def test_refused_feed_is_unavailable_not_clean(self) -> None:
        self.broker.get.return_value = mock.Mock(status_code=429, content=b"")
        report = RestaurantAnalyzer().analyze("McDonald's")
        self.assertIn("news", report.stages_unavailable)
        self.assertNotIn("news", report.stages_completed)

    def test_network_failure_is_unavailable(self) -> None:
        self.broker.get.side_effect = news.FetchError("down")
        self.assertIn("news", RestaurantAnalyzer().analyze("McDonald's").stages_unavailable)

    def test_old_stories_and_duplicates_are_dropped(self) -> None:
        self.broker.get.return_value = feed(
            item("McDonald's outlet sealed over hygiene", days_ago=900),
            item("McDonald's outlet fined over hygiene", days_ago=10),
            item("McDonald's outlet fined over hygiene", outlet="Mint", days_ago=9),
        )
        items, answered = news.search("McDonald's")
        self.assertTrue(answered)
        self.assertEqual(len(items), 1)

    def test_xml_with_entities_is_rejected(self) -> None:
        bomb = (
            b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'aaaa'>]>"
            b"<rss><channel><item><title>&a;</title></item></channel></rss>"
        )
        self.broker.get.return_value = mock.Mock(status_code=200, content=bomb)
        self.assertEqual(news.search("McDonald's"), ([], False))

    def test_results_are_cached(self) -> None:
        self.broker.get.return_value = feed(item("McDonald's fined over hygiene"))
        news.search("McDonald's")
        news.search("mcdonalds")
        self.assertEqual(self.broker.get.call_count, 1)

    def test_classification(self) -> None:
        self.assertEqual(news.classify("Customers fall ill, food poisoning at outlet"), ("news", "high"))
        self.assertEqual(news.classify("FSSAI issues notice"), ("news", "moderate"))
        self.assertEqual(news.classify("Brand recalls product batch"), ("recall", "moderate"))
        self.assertIsNone(news.classify("New menu launched"))
        self.assertIsNone(news.classify("Operator broke ratings record"))  # 'rat' inside a word


if __name__ == "__main__":
    unittest.main()
