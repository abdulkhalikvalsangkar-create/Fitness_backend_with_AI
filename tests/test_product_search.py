"""Product search, analysing a searched product by barcode, and scan caching."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("JWT_SECRET", "test-secret-for-product-search-32-bytes")

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from apps.api import actions, app_actions
from apps.api.security import Principal
from packages.connectors import openfacts
from packages.connectors.openfacts import OpenFactsConnector, ProductRecord
from packages.domain.enums import CacheTier, ProductUnidentifiedReason, RouteLabel, RouteStage
from packages.orchestrator import nodes
from packages.product import analyzer as analyzer_mod
from packages.product.analyzer import ProductAnalyzer
from packages.storage.repositories.products import ProductRepository

PRINCIPAL = Principal(user_id="u1")


FOOD_URL = "https://search.openfoodfacts.org/search"
BEAUTY_URL = "https://world.openbeautyfacts.org/cgi/search.pl"


def food_payload(*items: dict) -> mock.Mock:
    """Open Food Facts' search service: results under `hits`, `brands` is a list."""
    response = mock.Mock(status_code=200)
    response.json.return_value = {"hits": list(items), "count": len(items)}
    return response


def food_item(code="8901234567890", name="Choco Bar", **extra) -> dict:
    return {"code": code, "product_name": name, "brands": ["Acme", "Other"], "quantity": "50 g",
            "categories": "Snacks, Chocolate", "image_front_small_url": "https://img/s.jpg", **extra}


def beauty_payload(*items: dict) -> mock.Mock:
    """Open Beauty Facts' legacy search: results under `products`, `brands` is text."""
    response = mock.Mock(status_code=200)
    response.json.return_value = {"products": list(items), "count": len(items)}
    return response


def beauty_item(code="8711600804357", name="Dove bar", **extra) -> dict:
    return {"code": code, "product_name": name, "brands": "Dove, Unilever", "quantity": "90 g",
            "categories": "Soaps, Cosmetics", "image_front_small_url": "https://img/d.jpg",
            "ingredients_text": "Sodium Lauroyl Isethionate", **extra}


class ConnectorSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        openfacts._search_cache.clear()
        self.addCleanup(openfacts._search_cache.clear)
        self.broker = mock.Mock()
        patch = mock.patch.object(openfacts, "get_broker", return_value=self.broker)
        patch.start()
        self.addCleanup(patch.stop)

    def test_food_results_are_mapped_and_unusable_entries_dropped(self) -> None:
        self.broker.get.return_value = food_payload(
            food_item(),
            food_item(code="", name="No code"),
            food_item(code="abc123", name="Bad code"),
            food_item(code="8909999999999", name="  "),
            food_item(code="8908888888888", name="Plain", brands="OneBrand", quantity="", image_front_small_url="", image_url="https://img/l.jpg"),
            "not a dict",
        )
        records, answered = OpenFactsConnector().search("choco", sources=["off"])
        self.assertTrue(answered)
        self.assertEqual([r.name for r in records], ["Choco Bar", "Plain"])
        first, second = records
        self.assertEqual((first.brand, first.category, first.quantity, first.image_url, first.source, first.product_class),
                         ("Acme", "Snacks", "50 g", "https://img/s.jpg", "off", "food"))
        self.assertEqual(second.brand, "OneBrand")  # a plain string brand works too
        self.assertEqual((second.quantity, second.image_url), (None, "https://img/l.jpg"))

    def test_food_results_do_not_claim_to_know_about_ingredients(self) -> None:
        # The food search service never returns ingredient text, so "no
        # ingredients" would be a guess.
        self.broker.get.return_value = food_payload(food_item())
        (record,), _ = OpenFactsConnector().search("choco", sources=["off"])
        self.assertFalse(record.ingredients_known)

    def test_cosmetic_results_report_ingredients(self) -> None:
        self.broker.get.return_value = beauty_payload(beauty_item(), beauty_item(code="8712222222222", name="No list", ingredients_text=""))
        records, answered = OpenFactsConnector().search("dove", sources=["obf"])
        self.assertTrue(answered)
        self.assertEqual([(r.name, r.brand, r.ingredients_known, r.has_ingredients) for r in records],
                         [("Dove bar", "Dove", True, True), ("No list", "Dove", True, False)])
        self.assertEqual(records[0].product_class, "cosmetic")

    def test_each_database_gets_its_own_kind_of_request(self) -> None:
        self.broker.get.return_value = food_payload()
        OpenFactsConnector().search("choco bar", limit=7, sources=["off"])
        url, params = self.broker.get.call_args.args[0], self.broker.get.call_args.kwargs["params"]
        self.assertEqual(url, FOOD_URL)
        self.assertEqual((params["q"], params["page_size"]), ("choco bar", 7))
        self.assertNotIn("search_terms", params)

        self.broker.get.return_value = beauty_payload()
        OpenFactsConnector().search("dove soap", limit=5, sources=["obf"])
        url, params = self.broker.get.call_args.args[0], self.broker.get.call_args.kwargs["params"]
        self.assertEqual(url, BEAUTY_URL)
        self.assertEqual((params["search_terms"], params["json"], params["page_size"]), ("dove soap", 1, 5))

    def test_the_retired_food_endpoint_is_never_used(self) -> None:
        self.broker.get.return_value = food_payload()
        OpenFactsConnector().search("choco")
        urls = [c.args[0] for c in self.broker.get.call_args_list]
        self.assertNotIn("https://world.openfoodfacts.org/cgi/search.pl", urls)

    def test_food_and_cosmetics_are_searched_by_default_not_general_products(self) -> None:
        def get(url, **kwargs):
            return food_payload() if url == FOOD_URL else beauty_payload()

        self.broker.get.side_effect = get
        OpenFactsConnector().search("x1")
        self.assertEqual([c.args[0] for c in self.broker.get.call_args_list], [FOOD_URL, BEAUTY_URL])

    def test_the_search_host_is_on_the_default_fetch_allowlist(self) -> None:
        from packages.config import get_settings

        self.assertIn("search.openfoodfacts.org", get_settings().security.fetch_allowlist)

    def test_an_unreachable_source_is_not_an_empty_answer(self) -> None:
        self.broker.get.side_effect = openfacts.FetchError("down")
        records, answered = OpenFactsConnector().search("choco")
        self.assertEqual((records, answered), ([], False))

    def test_one_source_down_still_answers_from_the_other(self) -> None:
        def get(url, **kwargs):
            if url == FOOD_URL:
                raise openfacts.FetchError("down")
            return beauty_payload(beauty_item())

        self.broker.get.side_effect = get
        records, answered = OpenFactsConnector().search("dove")
        self.assertTrue(answered)
        self.assertEqual([r.name for r in records], ["Dove bar"])

    def test_bad_status_wrong_shape_and_bad_json_count_as_unreachable(self) -> None:
        self.broker.get.return_value = mock.Mock(status_code=503)  # what the retired endpoint returns
        self.assertEqual(OpenFactsConnector().search("a1", sources=["off"]), ([], False))
        wrong = mock.Mock(status_code=200)
        wrong.json.return_value = {"products": []}  # legacy shape from the food service
        self.broker.get.return_value = wrong
        self.assertEqual(OpenFactsConnector().search("a2", sources=["off"]), ([], False))
        bad = mock.Mock(status_code=200)
        bad.json.side_effect = ValueError("not json")
        self.broker.get.return_value = bad
        self.assertEqual(OpenFactsConnector().search("a3", sources=["off"]), ([], False))

    def test_identical_searches_are_served_from_memory(self) -> None:
        self.broker.get.return_value = food_payload(food_item())
        connector = OpenFactsConnector()
        connector.search("Choco", sources=["off"])
        records, answered = connector.search("choco", sources=["off"])  # case-insensitive
        self.assertTrue(answered)
        self.assertEqual(len(records), 1)
        self.assertEqual(self.broker.get.call_count, 1)

    def test_failures_are_never_cached(self) -> None:
        self.broker.get.side_effect = openfacts.FetchError("down")
        connector = OpenFactsConnector()
        connector.search("choco", sources=["off"])
        self.broker.get.side_effect = None
        self.broker.get.return_value = food_payload(food_item())
        records, answered = connector.search("choco", sources=["off"])
        self.assertTrue(answered)
        self.assertEqual(len(records), 1)

    def test_the_cache_is_bounded(self) -> None:
        self.broker.get.return_value = food_payload()
        connector = OpenFactsConnector()
        for i in range(openfacts._SEARCH_CACHE_MAX + 20):
            connector.search(f"query {i}", sources=["off"])
        self.assertLessEqual(len(openfacts._search_cache), openfacts._SEARCH_CACHE_MAX)


PRODUCT_DDL = """
CREATE TABLE product (
  product_id TEXT PRIMARY KEY, barcode TEXT, name TEXT, brand TEXT, category TEXT, quantity TEXT,
  image_url TEXT, product_class TEXT, ingredients_text TEXT, source TEXT,
  confidence REAL DEFAULT 0, expires_at TIMESTAMP NULL
)
"""


class LocalSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text(PRODUCT_DDL))
        self.session = sessionmaker(bind=engine)()
        self.addCleanup(self.session.close)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=5)
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=5)
        rows = [
            ("1", "8901111111111", "Apple Juice", "Fresh Co", "food", "Sugar", 0.9, future),
            ("2", "8902222222222", "50% Cocoa Bar", "Dark Co", "food", "", 0.8, None),
            ("3", "8903333333333", "aXb Soap", "Clean", "cosmetic", "Aqua", 0.7, future),
            ("4", "8904444444444", "a_b Soap", "Clean", "cosmetic", "Aqua", 0.6, future),
            ("5", "8905555555555", "Expired Apple", "Old Co", "food", "x", 0.9, past),
            ("6", None, "Apple No Barcode", "Co", "food", "x", 0.9, future),
        ]
        for pid, code, name, brand, pclass, ing, conf, exp in rows:
            self.session.execute(
                text(
                    "INSERT INTO product (product_id, barcode, name, brand, product_class, "
                    "ingredients_text, confidence, expires_at) "
                    "VALUES (:p, :c, :n, :b, :pc, :i, :cf, :e)"
                ),
                {"p": pid, "c": code, "n": name, "b": brand, "pc": pclass, "i": ing, "cf": conf, "e": exp},
            )
        self.repo = ProductRepository(self.session)

    def names(self, query: str) -> list[str]:
        return [r["name"] for r in self.repo.search_local(query)]

    def test_matches_name_and_brand_case_insensitively(self) -> None:
        self.assertEqual(self.names("apple"), ["Apple Juice"])
        self.assertEqual(self.names("fresh co"), ["Apple Juice"])

    def test_expired_and_barcode_less_rows_are_excluded(self) -> None:
        self.assertNotIn("Expired Apple", self.names("apple"))
        self.assertNotIn("Apple No Barcode", self.names("apple"))

    def test_like_wildcards_in_the_query_are_literal(self) -> None:
        self.assertEqual(self.names("50%"), ["50% Cocoa Bar"])  # not "match everything"
        self.assertEqual(self.names("a_b"), ["a_b Soap"])  # not "aXb" as well
        self.assertEqual(self.names("%"), ["50% Cocoa Bar"])

    def test_limit_is_applied(self) -> None:
        self.assertEqual(len(self.repo.search_local("o", limit=2)), 2)


class SearchActionTests(unittest.TestCase):
    def _local(self, **kw) -> dict:
        row = {"barcode": "8901111111111", "name": "Apple Juice", "brand": "Fresh Co", "category": "Drinks",
               "quantity": "1 L", "image_url": "https://i/a.jpg", "product_class": "food", "source": "off",
               "ingredients_text": "Apple"}
        return {**row, **kw}

    def _record(self, code="8902222222222", name="Remote Juice", **kw) -> ProductRecord:
        defaults = dict(barcode=code, found=True, name=name, brand="B", category="C", quantity="1 L",
                        image_url="https://i/r.jpg", product_class="food", ingredients_text="Water", source="off")
        return ProductRecord(**{**defaults, **kw})

    def _call(self, body: dict, local=(), remote=((), True)):
        connector = mock.Mock()
        connector.search.return_value = (list(remote[0]), remote[1])
        with mock.patch.object(app_actions, "ProductRepository") as repo, mock.patch.object(
            app_actions, "OpenFactsConnector", return_value=connector
        ):
            repo.return_value.search_local.return_value = list(local)
            result = app_actions.handle_product_search(body, PRINCIPAL, mock.Mock())
        return result, connector

    def test_validation(self) -> None:
        for body in ({}, {"query": "a"}, {"query": " "}, {"query": "x" * 101},
                     {"query": "ok", "limit": "many"}, {"query": "ok", "product_type": "pet"}):
            with self.assertRaises(HTTPException, msg=str(body)) as ctx:
                self._call(body)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_local_products_come_first_then_remote(self) -> None:
        result, _ = self._call({"query": "juice"}, local=[self._local()], remote=([self._record()], True))
        self.assertEqual([p["name"] for p in result["products"]], ["Apple Juice", "Remote Juice"])
        self.assertEqual([p["source"] for p in result["products"]], ["local", "off"])
        self.assertTrue(result["remote_available"])

    def test_result_shape(self) -> None:
        result, _ = self._call({"query": "juice"}, local=[self._local()])
        self.assertEqual(
            result["products"][0],
            {"barcode": "8901111111111", "name": "Apple Juice", "brand": "Fresh Co", "quantity": "1 L",
             "image_url": "https://i/a.jpg", "category": "Drinks", "product_class": "food",
             "source": "local", "has_ingredients": True},
        )
        self.assertEqual(result["action"], "product.search")
        self.assertEqual(result["query"], "juice")

    def test_has_ingredients_is_null_when_the_source_cannot_say(self) -> None:
        unknown = self._record(code="8902222222222", name="Food", ingredients_text=None, ingredients_known=False)
        known_without = self._record(code="8903333333333", name="Soap", ingredients_text=None, ingredients_known=True)
        known_with = self._record(code="8904444444444", name="Cream", ingredients_text="Aqua", ingredients_known=True)
        result, _ = self._call({"query": "x1"}, remote=([unknown, known_without, known_with], True))
        self.assertEqual([p["has_ingredients"] for p in result["products"]], [None, False, True])

    def test_the_same_product_is_listed_once_across_barcode_forms(self) -> None:
        # EAN-13 locally, the same article as a zero-padded UPC-A upstream.
        result, _ = self._call(
            {"query": "juice"},
            local=[self._local(barcode="8901111111111")],
            remote=([self._record(code="08901111111111")], True),
        )
        self.assertEqual(len(result["products"]), 1)
        self.assertEqual(result["products"][0]["source"], "local")

    def test_upstream_is_skipped_when_local_results_fill_the_page(self) -> None:
        local = [self._local(barcode=f"89011111111{i:02d}", name=f"Juice {i}") for i in range(3)]
        result, connector = self._call({"query": "juice", "limit": 3}, local=local)
        connector.search.assert_not_called()
        self.assertIsNone(result["remote_available"])
        self.assertEqual(len(result["products"]), 3)

    def test_limit_is_respected(self) -> None:
        remote = [self._record(code=f"89022222222{i:02d}", name=f"R{i}") for i in range(10)]
        result, _ = self._call({"query": "juice", "limit": 4}, remote=(remote, True))
        self.assertEqual(len(result["products"]), 4)

    def test_no_match_is_an_empty_list_when_upstream_answered(self) -> None:
        result, _ = self._call({"query": "zzzz"}, remote=([], True))
        self.assertEqual(result["products"], [])

    def test_no_match_and_upstream_down_is_503_not_empty(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call({"query": "juice"}, remote=([], False))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_upstream_down_but_local_results_still_return(self) -> None:
        result, _ = self._call({"query": "juice"}, local=[self._local()], remote=([], False))
        self.assertEqual(len(result["products"]), 1)
        self.assertFalse(result["remote_available"])

    def test_product_type_filters_local_rows_and_picks_the_upstream_source(self) -> None:
        local = [self._local(product_class="food"), self._local(barcode="8903333333333", name="Soap", product_class="cosmetic")]
        result, connector = self._call({"query": "so", "product_type": "cosmetic"}, local=local)
        self.assertEqual([p["name"] for p in result["products"]], ["Soap"])
        self.assertEqual(connector.search.call_args.kwargs["sources"], ["obf"])

    def test_rows_without_a_numeric_barcode_are_not_offered(self) -> None:
        result, _ = self._call({"query": "juice"}, local=[self._local(barcode="NOT-A-CODE")])
        self.assertEqual(result["products"], [])

    def test_the_action_is_registered(self) -> None:
        self.assertIn("product.search", actions.action_names())


class BarcodeLookupTests(unittest.TestCase):
    """`scan` with a barcode and no photo: analyse a product picked from search."""

    BARCODE = "8901234567890"

    def setUp(self) -> None:
        self.analyzer = ProductAnalyzer(mock.Mock(), user_id="u1")
        self.analyzer.products = mock.Mock()
        self.analyzer.products.by_barcode.return_value = None
        self.analyzer.products.upsert.return_value = "pid"
        self.analyzer.ocr = mock.Mock()
        self.addCleanup(mock.patch.stopall)
        self.scan = mock.patch.object(analyzer_mod.barcode_mod, "scan").start()
        self.lookup = mock.patch("packages.connectors.openfacts.OpenFactsConnector.lookup").start()

    def _record(self, ingredients="Sugar, Cocoa"):
        return SimpleNamespace(
            found=True, name="Choco Bar", brand="Acme", category=None, quantity="50 g", image_url="https://i/x.jpg",
            product_class="food", ingredients_text=ingredients, source="off", confidence=0.9, fetched_at=None,
            has_ingredients=bool(ingredients),
        )

    def test_no_photo_means_no_barcode_decoding_and_the_database_is_asked(self) -> None:
        self.lookup.return_value = self._record()
        trace = analyzer_mod.AnalysisTrace()
        identity, text_, _ = self.analyzer._identify([], trace, None, barcode=self.BARCODE)
        self.scan.assert_not_called()
        self.lookup.assert_called_once_with(self.BARCODE)
        self.analyzer.ocr.read_many.assert_not_called()
        self.assertEqual((identity.name, identity.barcode, text_), ("Choco Bar", self.BARCODE, "Sugar, Cocoa"))
        self.assertEqual(trace.identified_via, "barcode")

    def test_unknown_barcode_is_not_found_and_never_ocr(self) -> None:
        self.lookup.return_value = SimpleNamespace(found=False)
        analysis, _ = self.analyzer.analyze([], barcode=self.BARCODE)
        self.assertEqual(analysis.unidentified_reason, ProductUnidentifiedReason.UNLISTED_PRODUCT)
        self.assertEqual(analysis.identity.barcode, self.BARCODE)
        self.analyzer.ocr.read_many.assert_not_called()

    def test_no_photo_and_no_valid_barcode_is_unreadable(self) -> None:
        for bad in (None, "", "abc", "123", "1" * 20):
            analysis, _ = self.analyzer.analyze([], barcode=bad)
            self.assertEqual(analysis.unidentified_reason, ProductUnidentifiedReason.UNREADABLE, repr(bad))
        self.lookup.assert_not_called()

    def test_a_photo_wins_over_a_barcode(self) -> None:
        self.scan.return_value = SimpleNamespace(
            found=False, barcode=None, gtin14=None, symbology=None, confidence=0.0,
            saw_non_retail_symbol=False, rejected_symbols=[],
        )
        mock.patch.object(analyzer_mod.barcode_mod, "backend_available", return_value=True).start()
        self.analyzer.ocr.read_many.return_value = []
        self.analyzer._identify([b"img"], analyzer_mod.AnalysisTrace(), None, barcode=self.BARCODE)
        self.scan.assert_called_once()
        self.lookup.assert_not_called()


class ScanActionLookupTests(unittest.TestCase):
    def _scan(self, body: dict):
        with mock.patch.object(actions, "handle_chat", return_value={"payload": {"blocks": []}}) as chat, \
                mock.patch.object(actions, "_complete_scan_jobs", side_effect=lambda result, *_: result):
            actions.handle_scan({"scan_type": "product", **body}, PRINCIPAL, mock.Mock())
        return chat.call_args.args[0]

    def test_barcode_without_a_photo_becomes_a_lookup(self) -> None:
        hints = self._scan({"barcode": " 8901234567890 "})["client_hints"]
        self.assertEqual(hints, {"barcode_lookup": "8901234567890"})

    def test_a_photo_means_the_barcode_field_is_ignored(self) -> None:
        hints = self._scan({"barcode": "8901234567890", "attachments": [{"attachment_id": "a"}]})["client_hints"]
        self.assertNotIn("barcode_lookup", hints)

    def test_label_scan_keeps_its_own_meaning_for_barcode(self) -> None:
        hints = self._scan({"label_scan": True, "barcode": "8901234567890"})["client_hints"]
        self.assertNotIn("barcode_lookup", hints)
        self.assertEqual(hints["hint"]["barcode"], "8901234567890")

    def test_invalid_barcode_is_400(self) -> None:
        for bad in ("abc", "123", "1" * 15, "", "12 34 56 78"):
            with self.assertRaises(HTTPException, msg=repr(bad)) as ctx:
                self._scan({"barcode": bad})
            self.assertEqual(ctx.exception.status_code, 400)

    def test_restaurant_scans_ignore_the_barcode_field(self) -> None:
        with mock.patch.object(actions, "handle_chat", return_value={"payload": {"blocks": []}}) as chat, \
                mock.patch.object(actions, "_complete_scan_jobs", side_effect=lambda result, *_: result):
            actions.handle_scan({"scan_type": "restaurant", "barcode": "8901234567890"}, PRINCIPAL, mock.Mock())
        self.assertNotIn("barcode_lookup", chat.call_args.args[0]["client_hints"])


def scan_state(label=RouteLabel.PRODUCT, attachments=(), scan_type=None):
    return SimpleNamespace(
        route=SimpleNamespace(label=label),
        input=SimpleNamespace(attachments=list(attachments), scan_type=scan_type),
    )


class ScanCachingTests(unittest.TestCase):
    """A scan's answer depends on the photo/barcode/place, not on the empty message
    the cache is keyed on — serving a cached one returned the wrong product."""

    def test_which_turns_are_scans(self) -> None:
        self.assertTrue(nodes._is_scan_turn(scan_state(scan_type="product")))
        self.assertTrue(nodes._is_scan_turn(scan_state(scan_type="restaurant", label=RouteLabel.RESTAURANT)))
        self.assertTrue(nodes._is_scan_turn(scan_state(attachments=["a"])))
        self.assertFalse(nodes._is_scan_turn(scan_state()))  # a typed question about products
        self.assertFalse(nodes._is_scan_turn(scan_state(label=RouteLabel.PERSONAL, scan_type="product")))
        self.assertFalse(nodes._is_scan_turn(SimpleNamespace(route=None, input=SimpleNamespace(attachments=[], scan_type=None))))

    def test_a_barcode_lookup_never_reads_the_cache(self) -> None:
        state = scan_state(scan_type="product")
        with mock.patch.object(nodes, "CacheService", side_effect=AssertionError("cache must not be read")):
            self.assertEqual(nodes.cache_probe(state, mock.Mock()), (state, None))

    def test_a_barcode_lookup_never_writes_the_cache(self) -> None:
        from packages.domain.models import AnswerPayload, RouteDecision
        from packages.orchestrator.pipeline import new_state

        state = new_state("u1", "", scan_type="product")
        state.route = RouteDecision(
            label=RouteLabel.PRODUCT, confidence=1.0, stage=RouteStage.S0_RULES, rationale="explicit scan_type=product"
        )
        state.payload = AnswerPayload(blocks=[])
        state.telemetry.cache_status = CacheTier.MISS
        with mock.patch.object(nodes, "CacheService") as cache, mock.patch.object(nodes, "ConversationRepository"):
            nodes.persist(state, mock.Mock())
        cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
