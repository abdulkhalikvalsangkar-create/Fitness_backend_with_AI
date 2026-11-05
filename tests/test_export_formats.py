"""Export formats: JSON, CSV (ZIP of CSVs) and PDF."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("JWT_SECRET", "test-secret-for-export-format-tests-32b")

import jwt
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.api import app_actions
from apps.api.main import app
from apps.api.security import Principal
from packages.account import export, formats
from packages.jobs import handlers
from packages.jobs.registry import JobContext

TODAY = date.today()


def sample_data() -> dict:
    return {
        "account": {"email": "jane@example.com", "locale": "en", "jurisdiction": "IN"},
        "profile": {
            "display_name": "Jane", "date_of_birth": date(1994, 3, 2), "age_band": "25-34",
            "sex": "female", "height_cm": Decimal("168.0"), "weight_kg": Decimal("61.5"),
            "pregnancy_status": "none", "goals": ["run a 10k"], "preferences": ["vegetarian"],
        },
        "consent": [{"scope": "profile", "granted": 1}],
        "metrics": [
            {"metric": "weight", "measured_on": TODAY, "value": Decimal("61.5"), "unit": "kg", "source": "manual"},
            {"metric": "weight", "measured_on": TODAY - timedelta(days=3), "value": Decimal("62.5"), "unit": "kg", "source": "manual"},
            {"metric": "rhr", "measured_on": TODAY, "value": Decimal("-3"), "unit": "bpm", "source": "watch"},
        ],
        "nutrition": [
            {"consumed_on": TODAY, "calories": Decimal("2100"), "protein_g": Decimal("130"),
             "carbs_g": Decimal("220"), "fat_g": Decimal("70"), "water_ml": Decimal("2000")},
        ],
        "activities": [
            {"started_at": datetime(TODAY.year, TODAY.month, TODAY.day, 6, 30), "activity_type": "run",
             "duration_min": Decimal("42"), "distance_m": Decimal("8000"), "calories": Decimal("410")},
        ],
        "medical": [
            {"report_date": TODAY, "bmi": Decimal("22.1"), "systolic": 118, "diastolic": 76, "hba1c": Decimal("5.4"),
             "conditions": ["PCOS"], "allergies": ["peanuts"], "medications": ["metformin"]},
        ],
        "memories": [{"kind": "goal", "value": "run a 10k"}],
        "reminders": [
            {"category": "dosage", "title": "Vitamin D", "remind_time": "08:30", "remind_date": None,
             "days": [1, 3, 5], "enabled": 1},
        ],
        "chat_history": [{"session_id": "s1", "role": "user", "content": "=HYPERLINK(\"http://evil\")"}],
        "attachments": [],
    }


class FormatRegistryTests(unittest.TestCase):
    def test_known_formats(self) -> None:
        self.assertEqual(formats.get_format(None).name, "json")
        self.assertEqual(formats.get_format("CSV").extension, "zip")
        self.assertEqual(formats.get_format(" pdf ").media_type, "application/pdf")

    def test_unknown_format_is_rejected(self) -> None:
        for bad in ("xml", "xlsx", "docx", "../etc"):
            with self.assertRaises(ValueError):
                formats.get_format(bad)

    def test_filenames(self) -> None:
        self.assertEqual(formats.download_filename(formats.FORMATS["json"]), "health-data-export.json")
        self.assertEqual(formats.download_filename(formats.FORMATS["csv"]), "health-data-export.zip")
        self.assertEqual(formats.download_filename(formats.FORMATS["pdf"]), "health-data-export.pdf")


class CsvTests(unittest.TestCase):
    def _archive(self, data: dict) -> zipfile.ZipFile:
        return zipfile.ZipFile(io.BytesIO(formats.render_csv_zip(data)))

    @staticmethod
    def _rows(archive: zipfile.ZipFile, name: str) -> list[list[str]]:
        text = archive.read(name).decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text)))

    def test_one_csv_per_section_plus_readme(self) -> None:
        names = set(self._archive(sample_data()).namelist())
        self.assertTrue(
            {"account.csv", "profile.csv", "metrics.csv", "nutrition.csv", "activities.csv",
             "medical.csv", "memories.csv", "reminders.csv", "consent.csv", "chat_history.csv",
             "README.txt"} <= names
        )

    def test_empty_sections_are_left_out_and_listed_in_the_readme(self) -> None:
        archive = self._archive(sample_data())
        self.assertNotIn("attachments.csv", archive.namelist())
        self.assertIn("attachments", archive.read("README.txt").decode())

    def test_rows_and_headers(self) -> None:
        rows = self._rows(self._archive(sample_data()), "metrics.csv")
        self.assertEqual(rows[0], ["metric", "measured_on", "value", "unit", "source"])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1][:3], ["weight", TODAY.isoformat(), "61.5"])

    def test_negative_numbers_stay_numeric_but_formula_text_is_neutralised(self) -> None:
        archive = self._archive(sample_data())
        metrics = self._rows(archive, "metrics.csv")
        self.assertIn("-3", [r[2] for r in metrics])  # a number, left alone
        chat = self._rows(archive, "chat_history.csv")
        self.assertTrue(chat[1][2].startswith("'="), chat[1][2])

    def test_every_dangerous_prefix_is_neutralised(self) -> None:
        for prefix in ("=", "+", "-", "@"):
            self.assertTrue(formats._cell(f"{prefix}1+1").startswith("'"), prefix)
        self.assertEqual(formats._cell("hello"), "hello")
        self.assertEqual(formats._cell(-5), "-5")

    def test_lists_become_json_text_and_none_is_empty(self) -> None:
        rows = self._rows(self._archive(sample_data()), "reminders.csv")
        header = rows[0]
        self.assertEqual(rows[1][header.index("days")], "[1, 3, 5]")
        self.assertEqual(rows[1][header.index("remind_date")], "")

    def test_non_ascii_text_survives_and_file_has_a_bom_for_excel(self) -> None:
        data = sample_data()
        data["profile"]["display_name"] = "अभय"
        archive = self._archive(data)
        raw = archive.read("profile.csv")
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("अभय", raw.decode("utf-8-sig"))

    def test_no_data_still_produces_a_valid_archive(self) -> None:
        archive = self._archive({})
        self.assertEqual(archive.namelist(), ["README.txt"])


@unittest.skipUnless(formats.pdf_available(), "fpdf2 is not installed")
class PdfTests(unittest.TestCase):
    def test_produces_a_pdf(self) -> None:
        content = formats.render_pdf(sample_data())
        self.assertTrue(content.startswith(b"%PDF-"))
        self.assertGreater(len(content), 1500)
        self.assertIn(b"%%EOF", content[-32:])

    def test_non_latin_text_does_not_break_the_export(self) -> None:
        data = sample_data()
        data["profile"]["display_name"] = "अभय गुप्ता"
        data["reminders"][0]["title"] = "विटामिन डी — morning"
        self.assertTrue(formats.render_pdf(data).startswith(b"%PDF-"))

    def test_empty_data_is_still_a_valid_pdf(self) -> None:
        self.assertTrue(formats.render_pdf({}).startswith(b"%PDF-"))

    def test_large_data_paginates(self) -> None:
        data = sample_data()
        data["nutrition"] = [
            {"consumed_on": TODAY - timedelta(days=i % 30), "calories": 2000 + i} for i in range(200)
        ]
        data["metrics"] = [
            {"metric": f"m{i}", "measured_on": TODAY, "value": i, "unit": "u"} for i in range(150)
        ]
        self.assertTrue(formats.render_pdf(data).startswith(b"%PDF-"))

    def test_metric_summary_uses_latest_value_and_30_day_average(self) -> None:
        rows = [
            {"metric": "weight", "measured_on": TODAY, "value": 60, "unit": "kg"},
            {"metric": "weight", "measured_on": TODAY - timedelta(days=5), "value": 62, "unit": "kg"},
            {"metric": "weight", "measured_on": TODAY - timedelta(days=90), "value": 80, "unit": "kg"},
        ]
        (summary,) = formats._metric_summary(rows, TODAY)
        self.assertEqual(summary, ["weight", "60", "kg", TODAY.isoformat(), "61", "3"])


class WriteExportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.object(
            export, "export_path",
            lambda u, j, f="json": self.root / u / f"{j}.{formats.FORMATS[f].extension}",
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_each_format_writes_the_right_file(self) -> None:
        json_path = export.write_export("u1", "j1", sample_data(), "json")
        zip_path = export.write_export("u1", "j1", sample_data(), "csv")
        self.assertEqual((json_path.suffix, zip_path.suffix), (".json", ".zip"))
        self.assertEqual(json.loads(json_path.read_text("utf-8"))["data"]["profile"]["display_name"], "Jane")
        self.assertIn("metrics.csv", zipfile.ZipFile(zip_path).namelist())
        if formats.pdf_available():
            pdf_path = export.write_export("u1", "j1", sample_data(), "pdf")
            self.assertTrue(pdf_path.read_bytes().startswith(b"%PDF-"))

    def test_no_temp_files_are_left_behind(self) -> None:
        export.write_export("u1", "j1", sample_data(), "csv")
        self.assertEqual([p.name for p in (self.root / "u1").iterdir()], ["j1.zip"])

    def test_failed_render_leaves_nothing_behind(self) -> None:
        with mock.patch.object(formats, "render", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                export.write_export("u1", "j1", sample_data(), "pdf")
        self.assertFalse((self.root / "u1").exists())

    def test_purge_removes_old_files_of_every_format(self) -> None:
        import time

        cfg = export.get_settings().export
        with mock.patch.object(type(cfg), "dir", self.root, create=True):
            old = self.root / "u1"
            old.mkdir(parents=True)
            for name in ("a.json", "b.zip", "c.pdf"):
                path = old / name
                path.write_bytes(b"x")
                os.utime(path, (time.time() - 90000, time.time() - 90000))
            fresh = old / "d.pdf"
            fresh.write_bytes(b"x")
            with mock.patch.object(export, "get_settings") as settings:
                settings.return_value.export.dir = self.root
                settings.return_value.export.link_ttl_hours = 24
                removed = export.purge_expired_files()
        self.assertEqual(removed, 3)
        self.assertTrue(fresh.exists())


class LinkFormatTests(unittest.TestCase):
    def _sign(self, **claims) -> str:
        base = {"sub": "u1", "jid": "j1", "aud": export.EXPORT_AUDIENCE,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
        return jwt.encode({**base, **claims}, os.environ["JWT_SECRET"], algorithm="HS256")

    def test_format_round_trips_through_the_link(self) -> None:
        for fmt in ("json", "csv", "pdf"):
            token = export.sign_link("u1", "j1", datetime.now(timezone.utc) + timedelta(hours=1), fmt)
            self.assertEqual(export.verify_link(token), ("u1", "j1", fmt))

    def test_links_issued_before_formats_existed_are_json(self) -> None:
        self.assertEqual(export.verify_link(self._sign()), ("u1", "j1", "json"))

    def test_unknown_format_claim_is_rejected(self) -> None:
        with self.assertRaises(jwt.InvalidTokenError):
            export.verify_link(self._sign(fmt="../../etc/passwd"))


class DownloadEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.object(
            export, "export_path",
            lambda u, j, f="json": self.root / u / f"{j}.{formats.FORMATS[f].extension}",
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _get(self, fmt: str):
        export.write_export("u1", "j1", sample_data(), fmt)
        token = export.sign_link("u1", "j1", datetime.now(timezone.utc) + timedelta(hours=1), fmt)
        return self.client.get("/export/download", params={"token": token})

    def test_csv_downloads_as_a_zip(self) -> None:
        response = self._get("csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn('filename="health-data-export.zip"', response.headers["content-disposition"])
        self.assertIn("metrics.csv", zipfile.ZipFile(io.BytesIO(response.content)).namelist())

    @unittest.skipUnless(formats.pdf_available(), "fpdf2 is not installed")
    def test_pdf_downloads_as_a_pdf(self) -> None:
        response = self._get("pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/pdf")
        self.assertIn('filename="health-data-export.pdf"', response.headers["content-disposition"])
        self.assertTrue(response.content.startswith(b"%PDF-"))

    def test_json_still_downloads(self) -> None:
        response = self._get("json")
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertIn("export_version", response.json())

    def test_a_link_cannot_fetch_a_different_format_than_it_was_issued_for(self) -> None:
        export.write_export("u1", "j1", sample_data(), "json")  # no csv file exists
        token = export.sign_link("u1", "j1", datetime.now(timezone.utc) + timedelta(hours=1), "csv")
        self.assertEqual(self.client.get("/export/download", params={"token": token}).status_code, 404)


class ExportRequestActionTests(unittest.TestCase):
    PRINCIPAL = Principal(user_id="u1")

    def _call(self, body: dict):
        session = mock.Mock()
        session.execute.return_value.first.return_value = None  # no job to rejoin
        with mock.patch.object(app_actions, "UserRepository"), mock.patch.object(
            app_actions, "JobRepository"
        ) as jobs:
            jobs.return_value.enqueue.return_value = "job-1"
            result = app_actions.handle_export_request(body, self.PRINCIPAL, session)
        return result, jobs.return_value.enqueue

    def test_format_defaults_to_json(self) -> None:
        result, enqueue = self._call({})
        self.assertEqual(result["format"], "json")
        self.assertEqual(enqueue.call_args.kwargs["payload"], {"format": "json"})

    def test_csv_is_queued_with_its_format(self) -> None:
        result, enqueue = self._call({"format": "CSV"})
        self.assertEqual(result["format"], "csv")
        self.assertEqual(enqueue.call_args.kwargs["payload"], {"format": "csv"})

    def test_unknown_format_is_400(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call({"format": "xlsx"})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("format must be one of", ctx.exception.detail)

    def test_pdf_is_503_when_the_library_is_missing(self) -> None:
        with mock.patch.object(formats, "pdf_available", return_value=False):
            with self.assertRaises(HTTPException) as ctx:
                self._call({"format": "pdf"})
        self.assertEqual(ctx.exception.status_code, 503)

    def test_rejoin_is_scoped_to_the_requested_format(self) -> None:
        session = mock.Mock()
        session.execute.return_value.first.return_value = ("existing-job", "succeeded")
        with mock.patch.object(app_actions, "UserRepository"), mock.patch.object(app_actions, "JobRepository"):
            result = app_actions.handle_export_request({"format": "csv"}, self.PRINCIPAL, session)
        self.assertEqual(result["job_id"], "existing-job")
        self.assertEqual(session.execute.call_args.args[1]["fmt"], "csv")


class ExportJobTests(unittest.TestCase):
    def _run(self, payload: dict, tmp: Path) -> dict:
        session = mock.Mock()
        session.execute.return_value.first.return_value = None  # no email on file
        ctx = JobContext(job_id="job9", session=session, payload=payload, user_id="u1")
        with mock.patch.object(
            export, "export_path",
            lambda u, j, f="json": tmp / u / f"{j}.{formats.FORMATS[f].extension}",
        ), mock.patch.object(export, "collect", return_value=sample_data()), mock.patch(
            "packages.storage.db.session_scope"
        ):
            return handlers.export_user_data(ctx)

    def test_job_builds_the_requested_format_and_signs_a_matching_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for fmt, ext in (("json", "json"), ("csv", "zip")):
                result = self._run({"format": fmt}, Path(tmp))
                self.assertEqual(result["format"], fmt)
                self.assertEqual(result["filename"], f"health-data-export.{ext}")
                self.assertTrue((Path(tmp) / "u1" / f"job9.{ext}").is_file())
                token = result["download_url"].split("token=")[1]
                from urllib.parse import unquote

                self.assertEqual(export.verify_link(unquote(token))[2], fmt)

    def test_missing_format_means_json_for_jobs_queued_before_this_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run({}, Path(tmp))["format"], "json")

    def test_a_forged_payload_format_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._run({"format": "../../x"}, Path(tmp))


if __name__ == "__main__":
    unittest.main()


class RootDownloadTests(unittest.TestCase):
    """Download links live on `/`, the only path the cPanel host routes to the app."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.object(
            export, "export_path",
            lambda u, j, f="json": self.root / u / f"{j}.{formats.FORMATS[f].extension}",
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _token(self, fmt: str, hours: float = 1) -> str:
        return export.sign_link("u1", "j1", datetime.now(timezone.utc) + timedelta(hours=hours), fmt)

    def test_download_url_points_at_the_root_path(self) -> None:
        url = export.download_url("abc.def")
        self.assertIn("/?export_token=abc.def", url)
        self.assertNotIn("/export/download", url)

    def test_every_format_downloads_from_the_root_path(self) -> None:
        for fmt in ("json", "csv") + (("pdf",) if formats.pdf_available() else ()):
            export.write_export("u1", "j1", sample_data(), fmt)
            response = self.client.get("/", params={"export_token": self._token(fmt)})
            self.assertEqual(response.status_code, 200, fmt)
            self.assertEqual(response.headers["content-type"], formats.FORMATS[fmt].media_type)
            self.assertIn(formats.download_filename(formats.FORMATS[fmt]), response.headers["content-disposition"])

    def test_the_url_the_job_hands_out_actually_works(self) -> None:
        from urllib.parse import unquote

        export.write_export("u1", "j1", sample_data(), "csv")
        url = export.download_url(self._token("csv"))
        query = unquote(url.split("export_token=")[1])
        response = self.client.get("/", params={"export_token": query})
        self.assertEqual(response.status_code, 200)

    def test_bad_expired_and_missing_files_on_the_root_path(self) -> None:
        self.assertEqual(self.client.get("/", params={"export_token": "abc"}).status_code, 404)
        self.assertEqual(self.client.get("/", params={"export_token": self._token("json", -1)}).status_code, 410)
        self.assertEqual(self.client.get("/", params={"export_token": self._token("csv")}).status_code, 404)

    def test_root_without_a_token_is_still_the_status_endpoint(self) -> None:
        with mock.patch("apps.api.main.ping", return_value=False):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("actions", response.json())

    def test_old_path_still_works_where_the_host_allows_it(self) -> None:
        export.write_export("u1", "j1", sample_data(), "json")
        response = self.client.get("/export/download", params={"token": self._token("json")})
        self.assertEqual(response.status_code, 200)


class ExportRejoinTests(unittest.TestCase):
    """Reusing an earlier export must never hand back a link this host cannot serve."""

    PRINCIPAL = Principal(user_id="u1")

    def _call(self, body: dict, existing=None):
        session = mock.Mock()
        session.execute.return_value.first.return_value = existing
        with mock.patch.object(app_actions, "UserRepository"), mock.patch.object(
            app_actions, "JobRepository"
        ) as jobs:
            jobs.return_value.enqueue.return_value = "new-job"
            result = app_actions.handle_export_request(body, self.PRINCIPAL, session)
        return result, session, jobs.return_value.enqueue

    def test_finished_jobs_are_only_reused_if_their_link_is_the_current_form(self) -> None:
        _, session, _ = self._call({"format": "csv"})
        sql = str(session.execute.call_args.args[0])
        self.assertIn("result LIKE '%export_token=%'", sql)
        self.assertIn("status <> 'succeeded'", sql)

    def test_a_usable_earlier_job_is_rejoined(self) -> None:
        result, _, enqueue = self._call({"format": "csv"}, existing=("old-job", "succeeded"))
        self.assertEqual(result["job_id"], "old-job")
        enqueue.assert_not_called()

    def test_force_always_starts_a_new_export(self) -> None:
        for flag in (True, "true", 1):
            result, session, enqueue = self._call({"format": "csv", "force": flag}, existing=("old-job", "succeeded"))
            self.assertEqual(result["job_id"], "new-job")
            self.assertEqual(result["status"], "queued")
            session.execute.assert_not_called()
            enqueue.assert_called_once()

    def test_force_false_does_not_skip_the_rejoin(self) -> None:
        result, _, _ = self._call({"format": "json", "force": False}, existing=("old-job", "running"))
        self.assertEqual(result["job_id"], "old-job")
