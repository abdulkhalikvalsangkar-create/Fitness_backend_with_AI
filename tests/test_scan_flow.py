"""Scan flow: a barcode that no database knows must not fall through to OCR."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("JWT_SECRET", "test-secret-for-scan-flow-tests-32-bytes")

from fastapi import HTTPException

from apps.api import actions
from apps.api.security import Principal
from packages.domain.enums import ProductUnidentifiedReason
from packages.domain.models import ProductIdentity
from packages.product import analyzer as analyzer_mod
from packages.product.analyzer import ProductAnalyzer, _apply_hint

BARCODE = "8901234567890"
PANEL = "Ingredients: Sugar, Palm Oil, Tartrazine"


def barcode_found(code: str = BARCODE):
    return SimpleNamespace(
        found=True, barcode=code, gtin14="0" + code, symbology="EAN13", confidence=0.99,
        saw_non_retail_symbol=False, rejected_symbols=[],
    )


def barcode_missing(non_retail: bool = False):
    return SimpleNamespace(
        found=False, barcode=None, gtin14=None, symbology=None, confidence=0.0,
        saw_non_retail_symbol=non_retail, rejected_symbols=["QRCode"] if non_retail else [],
    )


def off_record(*, found: bool, ingredients: str | None = None, name: str | None = None):
    return SimpleNamespace(
        found=found, name=name, brand="Acme" if found else None, category=None, quantity=None,
        image_url=None, product_class="food", ingredients_text=ingredients, source="off",
        confidence=0.9, fetched_at=None, has_ingredients=bool(ingredients),
    )


class IdentifyTests(unittest.TestCase):
    """`_identify` with the scanner, the product tables and OCR all simulated."""

    def setUp(self) -> None:
        self.analyzer = ProductAnalyzer(mock.Mock(), user_id="u1")
        self.analyzer.products = mock.Mock()
        self.analyzer.products.by_barcode.return_value = None
        self.analyzer.products.upsert.return_value = "pid"
        self.ocr_calls = 0

        def read_many(images):
            self.ocr_calls += 1
            return [SimpleNamespace(text=PANEL, cached=False, error=None, ok=True)]

        self.analyzer.ocr = mock.Mock()
        self.analyzer.ocr.read_many.side_effect = read_many
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(analyzer_mod, "stitch", lambda results: " ".join(r.text for r in results)).start()
        mock.patch.object(analyzer_mod.barcode_mod, "backend_available", return_value=True).start()
        self.scan = mock.patch.object(analyzer_mod.barcode_mod, "scan").start()
        self.lookup = mock.patch("packages.connectors.openfacts.OpenFactsConnector.lookup").start()

    def identify(self, **kwargs):
        trace = analyzer_mod.AnalysisTrace()
        result = self.analyzer._identify([b"image"], trace, None, **kwargs)
        return result, trace

    # -- the reported bug -------------------------------------------------

    def test_barcode_not_in_any_database_does_not_run_ocr(self) -> None:
        self.scan.return_value = barcode_found()
        self.lookup.return_value = off_record(found=False)
        (identity, text, _), trace = self.identify()
        self.assertEqual(self.ocr_calls, 0)
        self.assertEqual(text, "")
        self.assertEqual(identity.barcode, BARCODE)
        self.assertIsNone(trace.identified_via)

    def test_product_known_by_name_but_without_ingredients_does_not_run_ocr(self) -> None:
        self.scan.return_value = barcode_found()
        self.lookup.return_value = off_record(found=True, ingredients=None, name="Choco Bar")
        (identity, text, _), _ = self.identify()
        self.assertEqual(self.ocr_calls, 0)
        self.assertEqual(text, "")
        self.assertEqual(identity.name, "Choco Bar")

    def test_locally_cached_product_without_ingredients_does_not_run_ocr(self) -> None:
        self.scan.return_value = barcode_found()
        self.analyzer.products.by_barcode.return_value = {
            "product_id": "p1", "barcode": BARCODE, "name": "Choco Bar", "ingredients_text": "",
        }
        (_, text, _), _ = self.identify()
        self.assertEqual(self.ocr_calls, 0)
        self.assertEqual(text, "")

    # -- behaviour that must not change -----------------------------------

    def test_product_with_ingredients_is_identified_by_barcode_without_ocr(self) -> None:
        self.scan.return_value = barcode_found()
        self.lookup.return_value = off_record(found=True, ingredients=PANEL, name="Choco Bar")
        (identity, text, _), trace = self.identify()
        self.assertEqual(self.ocr_calls, 0)
        self.assertEqual(text, PANEL)
        self.assertEqual(trace.identified_via, "barcode")

    def test_photo_without_a_barcode_is_read_by_ocr(self) -> None:
        self.scan.return_value = barcode_missing()
        (identity, text, _), trace = self.identify()
        self.assertEqual(self.ocr_calls, 1)
        self.assertEqual(text, PANEL)
        self.assertEqual(trace.identified_via, "ocr")

    def test_a_non_retail_symbol_still_falls_back_to_ocr(self) -> None:
        # A QR code on a document or label is not a product barcode: read the text.
        self.scan.return_value = barcode_missing(non_retail=True)
        (_, text, _), _ = self.identify()
        self.assertEqual(self.ocr_calls, 1)
        self.assertEqual(text, PANEL)

    # -- the follow-up: the user photographs the label --------------------

    def test_label_scan_reads_the_label_even_when_the_photo_shows_a_barcode(self) -> None:
        self.scan.return_value = barcode_found()
        self.lookup.return_value = off_record(found=False)
        (identity, text, _), trace = self.identify(label_scan=True)
        self.assertEqual(self.ocr_calls, 1)
        self.assertEqual(text, PANEL)
        self.assertEqual(trace.identified_via, "ocr")
        self.assertEqual(identity.barcode, BARCODE)

    def test_label_scan_still_prefers_a_database_record_with_ingredients(self) -> None:
        self.scan.return_value = barcode_found()
        self.lookup.return_value = off_record(found=True, ingredients=PANEL, name="Choco Bar")
        _, trace = self.identify(label_scan=True)
        self.assertEqual(self.ocr_calls, 0)
        self.assertEqual(trace.identified_via, "barcode")


class AnalyzeTests(unittest.TestCase):
    """The outcome the app sees, end to end through `analyze`."""

    def setUp(self) -> None:
        self.analyzer = ProductAnalyzer(mock.Mock(), user_id="u1")
        self.analyzer.products = mock.Mock()
        self.analyzer.products.by_barcode.return_value = None
        self.analyzer.ocr = mock.Mock()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(analyzer_mod.barcode_mod, "backend_available", return_value=True).start()
        mock.patch.object(analyzer_mod.barcode_mod, "scan", return_value=barcode_found()).start()
        mock.patch(
            "packages.connectors.openfacts.OpenFactsConnector.lookup",
            return_value=off_record(found=False),
        ).start()

    def test_unknown_barcode_returns_unlisted_product_with_the_barcode(self) -> None:
        analysis, _ = self.analyzer.analyze([b"image"])
        self.assertEqual(analysis.unidentified_reason, ProductUnidentifiedReason.UNLISTED_PRODUCT)
        self.assertEqual(analysis.identity.barcode, BARCODE)
        self.assertEqual(analysis.ingredients, [])
        self.analyzer.ocr.read_many.assert_not_called()

    def test_no_ocr_text_is_ever_analysed_for_a_barcode_only_photo(self) -> None:
        self.analyzer.ocr.read_many.return_value = [
            SimpleNamespace(text="8901234567890 ||| 0 1", cached=False, error=None, ok=True)
        ]
        analysis, _ = self.analyzer.analyze([b"image"])
        self.assertEqual(analysis.ingredients, [])
        self.assertIsNone(analysis.identified_via)


class HintTests(unittest.TestCase):
    def test_hint_fills_gaps_only(self) -> None:
        known = ProductIdentity(barcode=BARCODE, name="From Database")
        merged = _apply_hint(known, {"barcode": "123456", "product_name": "Typed", "brand": "TypedBrand"})
        self.assertEqual((merged.barcode, merged.name, merged.brand), (BARCODE, "From Database", "TypedBrand"))

    def test_hint_creates_an_identity_when_there_is_none(self) -> None:
        made = _apply_hint(None, {"barcode": BARCODE, "product_name": "Choco Bar", "brand": "Acme"})
        self.assertEqual((made.barcode, made.name, made.brand, made.source), (BARCODE, "Choco Bar", "Acme", "user"))

    def test_bad_or_empty_hints_are_ignored(self) -> None:
        self.assertIsNone(_apply_hint(None, {"barcode": "abc", "product_name": "  "}))
        self.assertIsNone(_apply_hint(None, None))
        self.assertIsNone(_apply_hint(None, "not a dict"))
        identity = ProductIdentity(name="X")
        self.assertIs(_apply_hint(identity, {}), identity)

    def test_hint_values_are_length_capped(self) -> None:
        made = _apply_hint(None, {"product_name": "x" * 1000, "brand": "y" * 1000})
        self.assertEqual((len(made.name), len(made.brand)), (255, 191))


class ScanActionTests(unittest.TestCase):
    PRINCIPAL = Principal(user_id="u1")

    def _scan(self, body: dict, chat_result: dict):
        with mock.patch.object(actions, "handle_chat", return_value=chat_result) as chat, mock.patch.object(
            actions, "_complete_scan_jobs", side_effect=lambda result, *_: result
        ):
            result = actions.handle_scan({"scan_type": "product", **body}, self.PRINCIPAL, mock.Mock())
        return result, chat

    @staticmethod
    def _unlisted():
        return {"payload": {"blocks": [{
            "block_id": "product_unidentified_1", "type": "product_unidentified",
            "data": {"reason": "unlisted_product", "action": "photograph_ingredient_panel"},
        }]}}

    def test_not_found_response_tells_the_app_what_to_do_next(self) -> None:
        result, _ = self._scan({}, self._unlisted())
        self.assertFalse(result["product_found"])
        self.assertEqual(result["verdict"], "unidentified")
        self.assertEqual(result["reason"], "unlisted_product")
        self.assertEqual(result["next_action"], "photograph_ingredient_panel")

    def test_a_found_product_is_marked_found(self) -> None:
        chat = {"payload": {"blocks": [{"block_id": "verdict_1", "data": {"verdict": "Generally suitable", "reason": "ok"}}]}}
        result, _ = self._scan({}, chat)
        self.assertTrue(result["product_found"])
        self.assertNotIn("next_action", result)

    def test_label_scan_hint_is_passed_to_the_analyzer(self) -> None:
        _, chat = self._scan(
            {"label_scan": True, "barcode": BARCODE, "product_name": " Choco Bar ", "brand": "Acme"},
            self._unlisted(),
        )
        hints = chat.call_args.args[0]["client_hints"]
        self.assertTrue(hints["label_scan"])
        self.assertEqual(hints["hint"], {"barcode": BARCODE, "product_name": "Choco Bar", "brand": "Acme"})

    def test_label_scan_flag_may_arrive_as_a_multipart_string(self) -> None:
        _, chat = self._scan({"label_scan": "true", "barcode": BARCODE}, self._unlisted())
        self.assertTrue(chat.call_args.args[0]["client_hints"]["label_scan"])

    def test_without_the_flag_typed_details_are_ignored(self) -> None:
        _, chat = self._scan({"barcode": BARCODE, "product_name": "X"}, self._unlisted())
        self.assertNotIn("label_scan", chat.call_args.args[0]["client_hints"])

    def test_an_invalid_barcode_in_a_label_scan_is_a_400(self) -> None:
        for bad in ("abc", "123", "1" * 20, "12 34 56 78"):
            with self.assertRaises(HTTPException, msg=bad) as ctx:
                self._scan({"label_scan": True, "barcode": bad}, self._unlisted())
            self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
