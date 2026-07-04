"""Daily PDF digest of the run's top-scoring articles, optionally uploaded
to Google Drive.

Called at the end of `main.run()` *after* the status report, so a crash
earlier in the run still leaves the already-evaluated winners available.

Design notes:
- PDF library: fpdf2 (pure Python, no system deps). Its built-in fonts
  (Helvetica) are Latin-1 only, so every string is run through `ascii_safe()`
  first - typographic chars (em-dash, curly quotes, ellipsis) and accented
  letters are mapped to ASCII. No TTF file is bundled; nothing to install.
- Drive upload uses a service account + a folder you share with that
  account (one-time setup; see the plan / README). The service account
  touches only that folder, so no OAuth consent-screen flow is needed.
- Everything is best-effort: a PDF or Drive failure logs a warning and the
  pipeline run is still considered successful. A digest artifact must never
  break the nightly job.
"""

from __future__ import annotations

import json
import logging
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF

import db as dbmod

log = logging.getLogger("reporter")

# Drive file scope: the service account can create files in the shared
# folder but cannot rummage through the rest of your Drive.
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


# --- text sanitization ------------------------------------------------
# Built-in fpdf2 fonts are Latin-1 (ISO-8859-1). Map the typographic chars
# that show up in news copy to ASCII, then encode/replace as a safety net.
_CHARMAP = {
    "—": "-",   # em dash
    "–": "-",   # en dash
    "’": "'",
    "‘": "'",
    "”": '"',
    "“": '"',
    "…": "...",
    "•": "*",
    "·": "*",
    "→": "->",
    "©": "(c)",
    "®": "(r)",
    "™": "(tm)",
    " ": " ",   # nbsp
    " ": " ",   # thin space
    " ": " ",   # narrow nbsp
    "\t": " ",
    "\r": " ",
}


def ascii_safe(text: str | None) -> str:
    """Map typographic / non-Latin-1 chars to ASCII so built-in fonts render
    them instead of showing a `?` box."""
    if not text:
        return ""
    out = []
    for ch in str(text):
        out.append(_CHARMAP.get(ch, ch))
    text = "".join(out)
    # NFKD pulls accented letters apart (é -> e + combining mark); dropping
    # the combining marks leaves the base letter.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Final safety net: anything still outside Latin-1 becomes '?'.
    return text.encode("latin-1", "replace").decode("latin-1")


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "unknown date"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Show in local time for a human-readable digest.
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


class _DigestPDF(FPDF):
    """Minimal FPDF subclass: a footer with page numbers and an auto header
    line on the first page."""

    def __init__(self) -> None:
        super().__init__(orientation="P", format="A4")
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(18, 18, 18)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(150)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")
        self.set_text_color(0)


def build_report_pdf(items: list[dbmod.Item], out_path: Path) -> Path:
    """Render `items` (already filtered to score >= min_score) into a PDF at
    `out_path`. Returns the written path."""
    pdf = _DigestPDF()
    pdf.add_page()

    # --- header -------------------------------------------------------
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(20)
    pdf.cell(0, 12, "AI Radar - daily digest", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(90)
    pdf.cell(
        0,
        6,
        ascii_safe(f"{datetime.now(timezone.utc).astimezone().strftime('%A, %Y-%m-%d')}  -  {len(items)} article(s) scoring >= 50"),
        ln=True,
    )
    pdf.ln(4)
    pdf.set_draw_color(210)
    pdf.line(18, pdf.get_y(), pdf.epw + 18, pdf.get_y())
    pdf.ln(6)
    pdf.set_text_color(0)

    for i, item in enumerate(items, 1):
        # Heading: score + title (bold)
        pdf.set_font("Helvetica", "B", 13)
        title = ascii_safe(item.title or "(untitled)")
        pdf.multi_cell(0, 7, f"{i}. [{item.score}] {title}")
        pdf.ln(1)

        # Meta line: source - published - read time
        meta_bits = [item.source or "", _fmt_dt(item.published_at)]
        if item.read_time_minutes:
            meta_bits.append(f"~{item.read_time_minutes} min read")
        meta = " - ".join(b for b in meta_bits if b)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(110)
        pdf.multi_cell(0, 5, ascii_safe(meta))
        pdf.set_text_color(0)

        # TL;DR (bottom-line conclusion, ahead of the fuller summary)
        if item.tldr:
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 10)
            pdf.multi_cell(0, 5.5, ascii_safe(f"TL;DR: {item.tldr}"))

        # Summary
        if item.summary:
            pdf.ln(1)
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5.5, ascii_safe(item.summary))

        # Reasons (why it scored this way)
        if item.reasons:
            pdf.ln(1)
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(70)
            pdf.multi_cell(0, 5.5, ascii_safe(f"Why: {item.reasons}"))
            pdf.set_text_color(0)

        # Tags
        try:
            tags = json.loads(item.tags or "[]")
        except (json.JSONDecodeError, ValueError):
            tags = []
        if tags:
            pdf.ln(1)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(40, 90, 160)
            pdf.multi_cell(0, 5, ascii_safe("Tags: " + ", ".join(tags)))
            pdf.set_text_color(0)

        # Link
        if item.url:
            pdf.ln(1)
            pdf.set_font("Helvetica", "U", 9)
            pdf.set_text_color(0, 0, 200)
            pdf.multi_cell(0, 5, ascii_safe(item.url))
            pdf.set_text_color(0)

        # Separator before the next item
        if i < len(items):
            pdf.ln(3)
            pdf.set_draw_color(225)
            pdf.line(18, pdf.get_y(), pdf.epw + 18, pdf.get_y())
            pdf.ln(4)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path


def upload_to_drive(pdf_path: Path, folder_id: str, service_account_file: str) -> str | None:
    """Upload `pdf_path` into the Drive `folder_id` using a service account.
    Returns the new file id, or None if anything went wrong (logs a warning)."""
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except ImportError as exc:
        log.warning("Drive upload skipped - google libs not installed (%s).", exc)
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(
            service_account_file, scopes=[_DRIVE_SCOPE]
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        media = MediaFileUpload(str(pdf_path), mimetype="application/pdf")
        created = (
            service.files()
            .create(
                body={"name": pdf_path.name, "parents": [folder_id]},
                media_body=media,
                fields="id",
            )
            .execute()
        )
        file_id = created.get("id")
        log.info("Uploaded %s to Drive (file id: %s).", pdf_path.name, file_id)
        return file_id
    except Exception as exc:  # noqa: BLE001 - upload must never fail the run
        log.warning("Drive upload failed for %s: %s", pdf_path.name, exc)
        return None


def maybe_generate_and_upload(database: dbmod.Database, run_start_dt: datetime, config: dict) -> None:
    """End-of-run orchestrator. Pulls this run's >= min_score winners, writes
    a local PDF, and uploads it to Drive if configured. Best-effort."""
    report_cfg = config.get("report") or {}
    if not report_cfg.get("enabled", True):
        return

    min_score = int(report_cfg.get("min_score", 50))
    items = database.items_evaluated_since(run_start_dt, min_score=min_score)
    if not items:
        log.info("No items scored >= %d this run; skipping PDF digest.", min_score)
        return

    out_dir = Path(report_cfg.get("out_dir", "reports"))
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    pdf_path = out_dir / f"ai-radar-{stamp}.pdf"

    try:
        build_report_pdf(items, pdf_path)
        log.info("Wrote digest PDF: %s (%d item(s)).", pdf_path, len(items))
    except Exception as exc:  # noqa: BLE001 - PDF build must never fail the run
        log.warning("Failed to build digest PDF: %s", exc)
        return

    drive_cfg = report_cfg.get("google_drive") or {}
    if not drive_cfg.get("enabled", False):
        return

    folder_id = os.environ.get(drive_cfg.get("folder_id_env", "GOOGLE_DRIVE_FOLDER_ID"), "").strip()
    sa_file = os.environ.get(
        drive_cfg.get("service_account_file_env", "GOOGLE_SERVICE_ACCOUNT_FILE"), ""
    ).strip()
    if not folder_id or not sa_file:
        log.warning(
            "Drive upload enabled but GOOGLE_DRIVE_FOLDER_ID / GOOGLE_SERVICE_ACCOUNT_FILE not set; "
            "leaving the PDF local only."
        )
        return

    upload_to_drive(pdf_path, folder_id, sa_file)