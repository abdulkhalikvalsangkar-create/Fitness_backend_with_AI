"""Export file formats: JSON (complete), CSV (complete, as a ZIP) and PDF (report).

JSON and CSV carry everything. The PDF is a human-readable summary — it leaves
out chat history and the attachment list, which are long and mostly free text —
and says so on its last page.

PDF text is limited to the Latin-1 range (the standard PDF fonts). Characters
outside it, such as Devanagari, are printed as '?' rather than failing the
export; the JSON and CSV files hold the original text.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional


@dataclass(frozen=True)
class ExportFormat:
    name: str
    extension: str
    media_type: str


FORMATS: dict[str, ExportFormat] = {
    "json": ExportFormat("json", "json", "application/json"),
    "csv": ExportFormat("csv", "zip", "application/zip"),
    "pdf": ExportFormat("pdf", "pdf", "application/pdf"),
}
DEFAULT_FORMAT = "json"


def get_format(name: Any) -> ExportFormat:
    key = str(name or DEFAULT_FORMAT).strip().lower()
    if key not in FORMATS:
        raise ValueError(f"format must be one of: {', '.join(FORMATS)}")
    return FORMATS[key]


def download_filename(fmt: ExportFormat) -> str:
    return f"health-data-export.{fmt.extension}"


def pdf_available() -> bool:
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return False
    return True


# -- JSON ---------------------------------------------------------------------


def render_json(document: dict[str, Any]) -> bytes:
    return json.dumps(document, default=str, ensure_ascii=False).encode("utf-8")


# -- CSV (ZIP of one CSV per section) -----------------------------------------

# Sections that are lists of rows, in the order they appear in the archive.
_TABLE_SECTIONS = (
    "metrics", "nutrition", "activities", "medical", "memories",
    "reminders", "consent", "chat_history", "attachments",
)
_OBJECT_SECTIONS = ("account", "profile")

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    """One CSV cell.

    Text that starts with = + - @ is prefixed with an apostrophe so a
    spreadsheet shows it as text instead of running it as a formula — chat
    messages and reminder titles are user-controlled. Numbers are never text
    here, so negative values stay numeric.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        value = json.dumps(value, default=str, ensure_ascii=False)
    text = str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in columns])
    # UTF-8 with a BOM: Excel otherwise reads non-ASCII text as Windows-1252.
    return buffer.getvalue().encode("utf-8-sig")


def render_csv_zip(data: dict[str, Any], generated_at: Optional[datetime] = None) -> bytes:
    stamp = (generated_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    included: list[str] = []
    skipped: list[str] = []
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for section in _OBJECT_SECTIONS:
            value = data.get(section)
            if isinstance(value, dict) and value:
                archive.writestr(f"{section}.csv", _csv_bytes([value]))
                included.append(section)
            else:
                skipped.append(section)
        for section in _TABLE_SECTIONS:
            rows = [r for r in (data.get(section) or []) if isinstance(r, dict)]
            if rows:
                archive.writestr(f"{section}.csv", _csv_bytes(rows))
                included.append(section)
            else:
                skipped.append(section)

        readme = [
            "Health data export",
            f"Generated: {stamp}",
            "",
            "One CSV file per section, UTF-8 encoded. Open with Excel, Numbers or Google Sheets.",
            "",
            "Included: " + (", ".join(f"{s}.csv" for s in included) or "(nothing)"),
            "No data for: " + (", ".join(skipped) or "(none)"),
        ]
        archive.writestr("README.txt", "\r\n".join(readme) + "\r\n")
    return buffer.getvalue()


# -- PDF ----------------------------------------------------------------------


def _latin1(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.encode("latin-1", "replace").decode("latin-1")


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_num(value: Any) -> str:
    number = _number(value)
    if number is None:
        return ""
    return f"{number:.1f}".rstrip("0").rstrip(".") if number != int(number) else str(int(number))


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def _list_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return "" if value is None else str(value)


def _metric_summary(rows: list[dict[str, Any]], today: date) -> list[list[str]]:
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("metric"):
            by_metric.setdefault(str(row["metric"]), []).append(row)

    summary: list[list[str]] = []
    cutoff = today - timedelta(days=30)
    for name in sorted(by_metric):
        points = [(r, _as_date(r.get("measured_on")), _number(r.get("value"))) for r in by_metric[name]]
        dated = [p for p in points if p[1] is not None and p[2] is not None]
        if not dated:
            continue
        latest = max(dated, key=lambda p: p[1])
        recent = [p[2] for p in dated if p[1] >= cutoff]
        summary.append(
            [
                name,
                _fmt_num(latest[2]),
                str(latest[0].get("unit") or ""),
                latest[1].isoformat(),
                _fmt_num(sum(recent) / len(recent)) if recent else "",
                str(len(dated)),
            ]
        )
    return summary


def render_pdf(data: dict[str, Any], generated_at: Optional[datetime] = None) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    now = generated_at or datetime.now(timezone.utc)
    today = now.date()

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_title("Health data export")
    pdf.add_page()

    def heading(title: str) -> None:
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, _latin1(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", size=10)

    def line(text: str) -> None:
        pdf.multi_cell(0, 5.5, _latin1(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def note(text: str) -> None:
        pdf.set_font("Helvetica", "I", 9)
        line(text)
        pdf.set_font("Helvetica", size=10)

    def table(header: list[str], rows: list[list[str]], widths: Optional[tuple[int, ...]] = None) -> None:
        pdf.set_font("Helvetica", size=9)
        with pdf.table(
            col_widths=widths, text_align="LEFT", line_height=5.5, first_row_as_headings=True
        ) as grid:
            head = grid.row()
            for title in header:
                head.cell(_latin1(title))
            for values in rows:
                row = grid.row()
                for value in values:
                    row.cell(_latin1(value))
        pdf.set_font("Helvetica", size=10)

    profile = data.get("profile") or {}
    account = data.get("account") or {}

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, "Health data export", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=10)
    line(f"Generated {now:%Y-%m-%d %H:%M} UTC")
    if profile.get("display_name"):
        line(f"Name: {profile['display_name']}")
    if account.get("email"):
        line(f"Account: {account['email']}")

    heading("Profile")
    profile_lines = [
        ("Date of birth", profile.get("date_of_birth")),
        ("Age band", profile.get("age_band")),
        ("Sex", profile.get("sex")),
        ("Height (cm)", _fmt_num(profile.get("height_cm"))),
        ("Weight (kg)", _fmt_num(profile.get("weight_kg"))),
        ("Pregnancy status", profile.get("pregnancy_status")),
        ("Goals", _list_text(profile.get("goals"))),
        ("Preferences", _list_text(profile.get("preferences"))),
    ]
    shown = [(k, v) for k, v in profile_lines if v not in (None, "")]
    if shown:
        for label, value in shown:
            line(f"{label}: {value}")
    else:
        note("No profile data.")

    heading("Health metrics")
    metrics = _metric_summary(data.get("metrics") or [], today)
    if metrics:
        table(["Metric", "Latest", "Unit", "Date", "30-day avg", "Records"], metrics, (34, 22, 20, 28, 26, 20))
    else:
        note("No health metrics recorded.")

    heading("Nutrition (last 30 days)")
    cutoff = today - timedelta(days=30)
    nutrition = [
        r for r in (data.get("nutrition") or [])
        if (_as_date(r.get("consumed_on")) or date.min) >= cutoff
    ]
    nutrition.sort(key=lambda r: str(r.get("consumed_on")), reverse=True)
    if nutrition:
        table(
            ["Date", "kcal", "Protein g", "Carbs g", "Fat g", "Water ml"],
            [
                [str(_as_date(r.get("consumed_on")) or ""), _fmt_num(r.get("calories")),
                 _fmt_num(r.get("protein_g")), _fmt_num(r.get("carbs_g")),
                 _fmt_num(r.get("fat_g")), _fmt_num(r.get("water_ml"))]
                for r in nutrition
            ],
        )
    else:
        note("No nutrition logged in the last 30 days.")

    heading("Activities (most recent 30)")
    activities = sorted(data.get("activities") or [], key=lambda r: str(r.get("started_at")), reverse=True)[:30]
    if activities:
        table(
            ["Date", "Type", "Minutes", "Distance m", "kcal"],
            [
                [str(_as_date(r.get("started_at")) or ""), str(r.get("activity_type") or ""),
                 _fmt_num(r.get("duration_min")), _fmt_num(r.get("distance_m")), _fmt_num(r.get("calories"))]
                for r in activities
            ],
        )
    else:
        note("No activities recorded.")

    heading("Medical")
    medical = sorted(data.get("medical") or [], key=lambda r: str(r.get("report_date")), reverse=True)
    if medical:
        latest = medical[0]
        line(f"Latest report: {_as_date(latest.get('report_date')) or ''}")
        if latest.get("systolic") is not None and latest.get("diastolic") is not None:
            line(f"Blood pressure: {latest['systolic']}/{latest['diastolic']} mmHg")
        for label, key in (("BMI", "bmi"), ("HbA1c", "hba1c")):
            if latest.get(key) is not None:
                line(f"{label}: {_fmt_num(latest[key])}")
        for label, key in (("Conditions", "conditions"), ("Allergies", "allergies"), ("Medications", "medications")):
            text = _list_text(latest.get(key))
            if text:
                line(f"{label}: {text}")
        if len(medical) > 1:
            note(f"{len(medical) - 1} earlier report(s) are in the CSV and JSON exports.")
    else:
        note("No medical reports.")

    memories = data.get("memories") or []
    if memories:
        heading("Saved facts and goals")
        for row in memories:
            line(f"- {row.get('kind', '')}: {row.get('value', '')}")

    heading("Reminders")
    reminders = data.get("reminders") or []
    if reminders:
        table(
            ["Category", "Title", "Time", "Date", "Days", "On"],
            [
                [str(r.get("category") or ""), str(r.get("title") or ""), str(r.get("remind_time") or ""),
                 str(r.get("remind_date") or ""), _list_text(r.get("days")),
                 "yes" if r.get("enabled") in (1, True) else "no"]
                for r in reminders
            ],
        )
    else:
        note("No reminders.")

    heading("About this report")
    note(
        "This PDF is a summary. Chat history, the attachment list and every individual "
        "record are in the CSV and JSON exports. Characters outside the standard Latin "
        "alphabet are shown as '?' here and are preserved in those files."
    )
    return bytes(pdf.output())


# -- dispatch -----------------------------------------------------------------

def render(fmt: ExportFormat, data: dict[str, Any], document: dict[str, Any]) -> bytes:
    """`document` is the JSON envelope; `data` is its `data` member."""
    if fmt.name == "json":
        return render_json(document)
    if fmt.name == "csv":
        return render_csv_zip(data)
    if fmt.name == "pdf":
        return render_pdf(data)
    raise ValueError(f"unsupported export format '{fmt.name}'")
