"""Account deletion, export links, reminders, place search and product summary.

None of these need a database: the SQL-facing code is exercised through its
validation and signing layers, and the handlers through patched collaborators.
"""

from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("JWT_SECRET", "test-secret-for-app-feature-tests-32-bytes")
os.environ.setdefault("GOOGLE_PLACES_API_KEY", "test-key")

import jwt
from fastapi import HTTPException

from apps.api import app_actions
from apps.api.security import Principal
from packages.account import export
from packages.domain.auth import FirebaseIdentity
from packages.domain.enums import HazardLevel, Severity, Verdict
from packages.domain.models import (
    HazardFinding,
    PersonalFlag,
    Profile,
    ProductAnalysis,
    ProductIdentity,
)
from packages.product.summary import product_score, product_summary
from packages.restaurant import places
from packages.storage.repositories import reminders as rem


class ProfileTests(unittest.TestCase):
    def test_profile_exposes_date_of_birth_goals_and_pregnancy_status(self) -> None:
        dumped = Profile(
            date_of_birth="1994-03-02", goals=["run a 10k"], pregnancy_status="none"
        ).model_dump(mode="json")
        self.assertEqual(dumped["date_of_birth"], "1994-03-02")
        self.assertEqual(dumped["goals"], ["run a 10k"])
        self.assertEqual(dumped["pregnancy_status"], "none")

    def test_assistant_summary_does_not_render_date_of_birth(self) -> None:
        from packages.chains.personal import render_context_summary
        from packages.domain.models import UserContext

        context = UserContext(
            user_id="u1", profile=Profile(date_of_birth="1994-03-02", age_band="25-34")
        )
        summary = render_context_summary(context)
        self.assertNotIn("1994", summary)
        self.assertIn("25-34", summary)


class ProductSummaryTests(unittest.TestCase):
    def _analysis(self, verdict: Verdict, **kwargs) -> ProductAnalysis:
        return ProductAnalysis(
            identity=ProductIdentity(
                name="Choco Bar", brand="Acme", quantity="50 g",
                barcode="8901234567890", image_url="https://img/x.jpg",
            ),
            verdict=verdict,
            **kwargs,
        )

    def test_summary_carries_identity_and_method(self) -> None:
        analysis = self._analysis(Verdict.GENERALLY_SUITABLE, identified_via="barcode")
        summary = product_summary(analysis)
        self.assertEqual(
            summary["product"],
            {
                "name": "Choco Bar", "brand": "Acme", "quantity": "50 g",
                "barcode": "8901234567890", "image_url": "https://img/x.jpg",
            },
        )
        self.assertEqual(summary["identified_via"], "barcode")

    def test_decoded_barcode_with_ocr_fallback_is_distinguishable(self) -> None:
        summary = product_summary(self._analysis(Verdict.USE_WITH_CAUTION, identified_via="ocr"))
        self.assertEqual(summary["identified_via"], "ocr")
        self.assertEqual(summary["product"]["barcode"], "8901234567890")

    def test_no_analysis_gives_empty_summary(self) -> None:
        self.assertEqual(
            product_summary(None), {"product": None, "identified_via": None, "score": None}
        )

    def test_insufficient_data_has_no_score(self) -> None:
        self.assertIsNone(product_score(self._analysis(Verdict.INSUFFICIENT_DATA)))

    def test_clean_product_scores_full_marks(self) -> None:
        score = product_score(self._analysis(Verdict.GENERALLY_SUITABLE))
        self.assertEqual(score, {"value": 100, "scale_max": 100, "label": "Generally suitable"})

    def test_score_never_contradicts_verdict(self) -> None:
        # One minor hazard would leave the raw number high; the verdict wins.
        caution = product_score(
            self._analysis(
                Verdict.USE_WITH_CAUTION,
                hazards=[HazardFinding(chemical_id="c1", display_name="X", hazard_level=HazardLevel.LOW)],
            )
        )
        self.assertLessEqual(caution["value"], 69)
        self.assertGreaterEqual(caution["value"], 40)

        bad = product_score(
            self._analysis(
                Verdict.NOT_RECOMMENDED,
                personal_flags=[
                    PersonalFlag(chemical_id="c2", display_name="Y", reason="allergy", severity=Severity.CRITICAL)
                ],
            )
        )
        self.assertLessEqual(bad["value"], 39)
        self.assertEqual(bad["label"], "Not recommended for you")

    def test_hazards_lower_the_score(self) -> None:
        clean = product_score(self._analysis(Verdict.GENERALLY_SUITABLE))["value"]
        hazard = HazardFinding(chemical_id="c1", display_name="X", hazard_level=HazardLevel.MODERATE)
        # Stays within the "suitable" band but is still lower than a clean panel.
        lowered = product_score(self._analysis(Verdict.GENERALLY_SUITABLE, hazards=[hazard]))["value"]
        self.assertLess(lowered, clean)


class ReminderValidationTests(unittest.TestCase):
    def test_create_requires_category_title_and_time(self) -> None:
        with self.assertRaises(rem.ReminderError):
            rem.normalise({"title": "Vitamin D", "time": "08:00"}, partial=False)
        with self.assertRaises(rem.ReminderError):
            rem.normalise({"category": "dosage", "time": "08:00"}, partial=False)
        with self.assertRaises(rem.ReminderError):
            rem.normalise({"category": "dosage", "title": "Vitamin D"}, partial=False)

    def test_all_six_categories_accepted_and_others_rejected(self) -> None:
        for category in ("dosage", "feeding", "medication", "activity", "health", "petcare"):
            values = rem.normalise({"category": category, "title": "t", "time": "07:05"}, partial=False)
            self.assertEqual(values["category"], category)
        with self.assertRaises(rem.ReminderError):
            rem.normalise({"category": "sleep", "title": "t", "time": "07:05"}, partial=False)

    def test_time_must_be_24_hour_hh_mm(self) -> None:
        for bad in ("8:00", "24:00", "12:60", "08:00 PM", "", None, 800):
            with self.assertRaises(rem.ReminderError, msg=repr(bad)):
                rem.normalise({"category": "health", "title": "t", "time": bad}, partial=False)
        for good in ("00:00", "09:30", "23:59"):
            rem.normalise({"category": "health", "title": "t", "time": good}, partial=False)

    def test_days_are_validated_deduped_and_sorted(self) -> None:
        values = rem.normalise({"days": [5, 1, 5, 3]}, partial=True)
        self.assertEqual(values["days"], [1, 3, 5])
        for bad in ([0], [8], ["1"], [True], "1,2", [1.5]):
            with self.assertRaises(rem.ReminderError, msg=repr(bad)):
                rem.normalise({"days": bad}, partial=True)

    def test_date_must_be_iso(self) -> None:
        self.assertEqual(rem.normalise({"date": "2026-11-02"}, partial=True)["remind_date"], "2026-11-02")
        self.assertIsNone(rem.normalise({"date": None}, partial=True)["remind_date"])
        with self.assertRaises(rem.ReminderError):
            rem.normalise({"date": "02/11/2026"}, partial=True)

    def test_partial_update_only_validates_supplied_keys(self) -> None:
        self.assertEqual(rem.normalise({"enabled": False}, partial=True), {"enabled": False})
        with self.assertRaises(rem.ReminderError):
            rem.normalise({"enabled": "no"}, partial=True)

    def test_date_and_days_together_are_rejected(self) -> None:
        with self.assertRaises(rem.ReminderError):
            rem._check_schedule("2026-11-02", [1, 2])
        rem._check_schedule("2026-11-02", [])
        rem._check_schedule(None, [1, 2])

    def test_api_shape(self) -> None:
        shaped = rem.to_api(
            {
                "reminder_id": "abc", "category": "dosage", "enabled": 1, "title": "Vit D",
                "subtitle": None, "remind_time": "08:00", "remind_date": None,
                "days": "[1, 3]", "supplement_id": "s1", "pet_id": None, "item": None,
            }
        )
        self.assertEqual(shaped["id"], "abc")
        self.assertEqual(shaped["time"], "08:00")
        self.assertIs(shaped["enabled"], True)
        self.assertEqual(shaped["days"], [1, 3])
        self.assertEqual(shaped["supplement_id"], "s1")


class ReminderActionTests(unittest.TestCase):
    def _call(self, body: dict, repo: mock.Mock):
        with mock.patch.object(app_actions, "UserRepository"), mock.patch.object(
            app_actions, "ReminderRepository", return_value=repo
        ):
            return app_actions.handle_reminders(body, Principal(user_id="u1"), mock.Mock())

    def test_unknown_op_is_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call({"op": "archive", "id": "x"}, mock.Mock())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_update_without_id_is_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call({"op": "update"}, mock.Mock())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_update_missing_reminder_is_404(self) -> None:
        repo = mock.Mock()
        repo.update.side_effect = rem.ReminderNotFound("x")
        with self.assertRaises(HTTPException) as ctx:
            self._call({"op": "update", "id": "x", "title": "t"}, repo)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_delete_is_idempotent(self) -> None:
        repo = mock.Mock()
        repo.delete.return_value = False
        result = self._call({"op": "delete", "id": "gone"}, repo)
        self.assertEqual(result["deleted"], False)

    def test_list_rejects_unknown_category(self) -> None:
        with self.assertRaises(HTTPException):
            self._call({"op": "list", "category": "sleep"}, mock.Mock())


class ExportLinkTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        token = export.sign_link("u1", "job1", datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(export.verify_link(token), ("u1", "job1"))

    def test_expired_link_is_rejected(self) -> None:
        token = export.sign_link("u1", "job1", datetime.now(timezone.utc) - timedelta(seconds=5))
        with self.assertRaises(jwt.ExpiredSignatureError):
            export.verify_link(token)

    def test_access_token_cannot_be_used_as_download_link(self) -> None:
        from packages.auth.tokens import create_access_token

        access, _ = create_access_token("u1", ("user",))
        with self.assertRaises(jwt.InvalidTokenError):
            export.verify_link(access)

    def test_tampered_link_is_rejected(self) -> None:
        token = export.sign_link("u1", "job1", datetime.now(timezone.utc) + timedelta(hours=1))
        with self.assertRaises(jwt.InvalidTokenError):
            export.verify_link(token[:-3] + "abc")

    def test_export_path_cannot_escape_the_export_directory(self) -> None:
        path = export.export_path("../../etc", "../passwd")
        root = export.get_settings().export.dir.resolve()
        self.assertTrue(str(path.resolve()).startswith(str(root)))

    def test_write_export_is_atomic_and_complete(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(export, "export_path", lambda u, j: Path(tmp) / u / f"{j}.json"):
                path = export.write_export("u1", "j1", {"profile": {"display_name": "A"}})
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(document["data"]["profile"]["display_name"], "A")
            self.assertEqual([p.name for p in path.parent.iterdir()], ["j1.json"])


class PlacesTests(unittest.TestCase):
    def _response(self, payload: dict, status: int = 200) -> mock.Mock:
        response = mock.Mock(status_code=status)
        response.json.return_value = payload
        return response

    def test_search_parses_place_id_name_and_address(self) -> None:
        payload = {
            "suggestions": [
                {
                    "placePrediction": {
                        "placeId": "ChIJ1",
                        "text": {"text": "Truffles, Koramangala, Bengaluru"},
                        "structuredFormat": {
                            "mainText": {"text": "Truffles"},
                            "secondaryText": {"text": "Koramangala, Bengaluru"},
                        },
                    }
                },
                {"queryPrediction": {"text": {"text": "truffles near me"}}},
            ]
        }
        broker = mock.Mock()
        broker.post.return_value = self._response(payload)
        with mock.patch.object(places, "get_broker", return_value=broker):
            found = places.search("truffles")
        self.assertEqual(
            [p.to_dict() for p in found],
            [{"place_id": "ChIJ1", "name": "Truffles", "address": "Koramangala, Bengaluru"}],
        )

    def test_provider_error_is_unavailable_not_empty(self) -> None:
        broker = mock.Mock()
        broker.post.return_value = self._response({}, status=403)
        with mock.patch.object(places, "get_broker", return_value=broker):
            with self.assertRaises(places.PlacesUnavailable):
                places.search("truffles")

    def test_missing_key_is_unavailable_not_empty(self) -> None:
        with mock.patch.object(places, "available", return_value=False):
            with self.assertRaises(places.PlacesUnavailable):
                places.search("truffles")

    def test_action_maps_unavailable_to_503_and_validates_query(self) -> None:
        with self.assertRaises(HTTPException) as short:
            app_actions.handle_places_search({"query": "a"}, Principal(user_id="u1"), mock.Mock())
        self.assertEqual(short.exception.status_code, 400)

        with mock.patch.object(places, "search", side_effect=places.PlacesUnavailable("x")):
            with self.assertRaises(HTTPException) as down:
                app_actions.handle_places_search({"query": "pizza"}, Principal(user_id="u1"), mock.Mock())
        self.assertEqual(down.exception.status_code, 503)


class DeleteAccountTests(unittest.TestCase):
    PRINCIPAL = Principal(user_id="u1")

    def _identity(self, uid: str = "fb-1", age: int = 10) -> FirebaseIdentity:
        return FirebaseIdentity(uid=uid, auth_time=int(time.time()) - age)

    def _run(self, identity, *, user=None, firebase_delete=True, body=None):
        session = mock.Mock()
        user_repo = mock.Mock()
        user_repo.get.return_value = user
        with mock.patch("packages.auth.firebase.verify_id_token", return_value=identity), \
             mock.patch("packages.auth.firebase.delete_user", return_value=firebase_delete) as fb, \
             mock.patch.object(app_actions, "UserRepository", return_value=user_repo), \
             mock.patch.object(app_actions, "delete_user_data", return_value=({"profile": 1}, [])) as erase, \
             mock.patch.object(app_actions, "remove_files"):
            result = app_actions.handle_delete_account(
                body or {"firebase_id_token": "tok"}, self.PRINCIPAL, session
            )
        return result, session, fb, erase

    def test_token_is_required(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            app_actions.handle_delete_account({}, self.PRINCIPAL, mock.Mock())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_invalid_token_is_401_and_deletes_nothing(self) -> None:
        with mock.patch("packages.auth.firebase.verify_id_token", side_effect=ValueError("bad")), \
             mock.patch.object(app_actions, "delete_user_data") as erase:
            with self.assertRaises(HTTPException) as ctx:
                app_actions.handle_delete_account({"firebase_id_token": "x"}, self.PRINCIPAL, mock.Mock())
        self.assertEqual(ctx.exception.status_code, 401)
        erase.assert_not_called()

    def test_stale_sign_in_is_rejected(self) -> None:
        with mock.patch.object(app_actions, "delete_user_data") as erase, \
             mock.patch("packages.auth.firebase.verify_id_token", return_value=self._identity(age=3600)):
            with self.assertRaises(HTTPException) as ctx:
                app_actions.handle_delete_account({"firebase_id_token": "x"}, self.PRINCIPAL, mock.Mock())
        self.assertEqual(ctx.exception.status_code, 401)
        erase.assert_not_called()

    def test_token_for_a_different_account_is_403(self) -> None:
        with mock.patch.object(app_actions, "delete_user_data") as erase, \
             mock.patch("packages.auth.firebase.verify_id_token", return_value=self._identity("someone-else")), \
             mock.patch.object(app_actions, "UserRepository") as repo:
            repo.return_value.get.return_value = {"user_id": "u1", "external_id": "fb-1"}
            with self.assertRaises(HTTPException) as ctx:
                app_actions.handle_delete_account({"firebase_id_token": "x"}, self.PRINCIPAL, mock.Mock())
        self.assertEqual(ctx.exception.status_code, 403)
        erase.assert_not_called()

    def test_success_erases_commits_then_deletes_firebase_user(self) -> None:
        result, session, fb, erase = self._run(
            self._identity(), user={"user_id": "u1", "external_id": "fb-1"}
        )
        self.assertTrue(result["deleted"])
        self.assertTrue(result["firebase_deleted"])
        erase.assert_called_once()
        session.commit.assert_called_once()
        fb.assert_called_once_with("fb-1")

    def test_repeat_call_after_success_is_idempotent(self) -> None:
        # Local account already gone and Firebase user already gone.
        result, _, _, _ = self._run(self._identity(), user=None, firebase_delete=False)
        self.assertTrue(result["deleted"])
        self.assertFalse(result["firebase_deleted"])

    def test_firebase_failure_surfaces_as_502_after_local_erasure_commits(self) -> None:
        session = mock.Mock()
        with mock.patch("packages.auth.firebase.verify_id_token", return_value=self._identity()), \
             mock.patch("packages.auth.firebase.delete_user", side_effect=RuntimeError("down")), \
             mock.patch.object(app_actions, "UserRepository") as repo, \
             mock.patch.object(app_actions, "delete_user_data", return_value=({}, [])), \
             mock.patch.object(app_actions, "remove_files"):
            repo.return_value.get.return_value = {"user_id": "u1", "external_id": "fb-1"}
            with self.assertRaises(HTTPException) as ctx:
                app_actions.handle_delete_account({"firebase_id_token": "x"}, self.PRINCIPAL, session)
        self.assertEqual(ctx.exception.status_code, 502)
        session.commit.assert_called_once()


class ActionRegistrationTests(unittest.TestCase):
    def test_new_actions_are_registered(self) -> None:
        from apps.api.actions import action_names

        for name in ("auth.delete_account", "export.request", "places.search", "reminders"):
            self.assertIn(name, action_names())


if __name__ == "__main__":
    unittest.main()
