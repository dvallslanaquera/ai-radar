"""Weekly PDF digest of the last 7 days' top-scoring articles, optionally
uploaded to Google Drive.

Built by `main.run_weekly_digest()` (entry point: `python main.py --digest`),
which the external scheduler triggers every Friday at 17:00. It aggregates
every daily run's score>=min_score winners over the trailing week via
`Database.items_evaluated_between`, so one PDF covers Mon-Fri instead of a
single run's snapshot. The daily `main.run()` no longer builds a PDF.

Design notes:
- PDF library: fpdf2 (pure Python, no system deps). Its built-in fonts
  (Helvetica) are Latin-1 only, so every string is run through `ascii_safe()`
  first - typographic chars (em-dash, curly quotes, ellipsis) and accented
  letters are mapped to ASCII. No TTF file is bundled; nothing to install.
- Drive upload uses a service account + a folder you share with that
  account (one-time setup; see the plan / README). The service account
  touches only that folder, so no OAuth consent-screen flow is needed.
- Everything is best-effort: a PDF or Drive failure logs a warning and the
  digest step is still considered successful. A digest artifact must never
  break the weekly job.
"""

from __future__ import annotations

import json
import logging
import os
import unicodedata
from datetime import datetime, timedelta, timezone
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


def build_report_pdf(
    items: list[dbmod.Item],
    out_path: Path,
    *,
    title: str = "AI Radar - weekly digest",
    subtitle: str | None = None,
) -> Path:
    """Render `items` (already filtered to score >= min_score) into a PDF at
    `out_path`. `title` and `subtitle` drive the header block. Returns the
    written path."""
    pdf = _DigestPDF()
    pdf.add_page()

    # --- header -------------------------------------------------------
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(20)
    pdf.cell(0, 12, ascii_safe(title), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(90)
    if subtitle is None:
        subtitle = (
            f"{datetime.now(timezone.utc).astimezone().strftime('%A, %Y-%m-%d')}"
            f"  -  {len(items)} article(s)"
        )
    pdf.cell(0, 6, ascii_safe(subtitle), ln=True)
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


def _load_user_credentials(token_file: str):
    """Load saved OAuth *user* credentials, refreshing (and re-saving) them
    silently when expired. Returns a Credentials object, or None if there's no
    usable token yet.

    This never opens a browser: the one-time consent that creates the token
    lives in `authorize_drive.py`, so the nightly scheduled run can't block on
    a browser prompt. Once refreshed here, the new token is written back so the
    next run starts from a fresh access token.
    """
    try:
        from google.oauth2.credentials import Credentials  # type: ignore
        from google.auth.transport.requests import Request  # type: ignore
    except ImportError as exc:
        log.warning("Drive upload skipped - google libs not installed (%s).", exc)
        return None

    token_path = Path(token_file)
    if not token_path.exists():
        log.warning(
            "Drive OAuth token not found at %s - run `python authorize_drive.py` "
            "once to authorize. Leaving the PDF local only.", token_file,
        )
        return None

    try:
        creds = Credentials.from_authorized_user_file(token_file, [_DRIVE_SCOPE])
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning(
            "Drive OAuth token at %s is malformed (%s) - re-run "
            "`python authorize_drive.py`. Leaving the PDF local only.",
            token_file, exc,
        )
        return None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as exc:  # noqa: BLE001 - a bad refresh must never fail the run
            log.warning(
                "Drive OAuth token refresh failed (%s) - re-run "
                "`python authorize_drive.py`. Leaving the PDF local only.", exc,
            )
            return None
    log.warning(
        "Drive OAuth token at %s is unusable (no refresh token) - re-run "
        "`python authorize_drive.py`. Leaving the PDF local only.", token_file,
    )
    return None


def _load_service_account_credentials(service_account_file: str):
    """Load a service-account credential (Workspace / Shared Drive setups).
    Returns a Credentials object, or None on failure."""
    try:
        from google.oauth2 import service_account  # type: ignore
    except ImportError as exc:
        log.warning("Drive upload skipped - google libs not installed (%s).", exc)
        return None
    try:
        return service_account.Credentials.from_service_account_file(
            service_account_file, scopes=[_DRIVE_SCOPE]
        )
    except Exception as exc:  # noqa: BLE001 - upload must never fail the run
        log.warning("Could not load service account file %s: %s", service_account_file, exc)
        return None


def upload_to_drive(pdf_path: Path, folder_id: str, creds) -> str | None:
    """Upload `pdf_path` into the Drive `folder_id` with the given credentials.
    Returns the new file id, or None if anything went wrong (logs a warning)."""
    try:
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaFileUpload  # type: ignore
    except ImportError as exc:
        log.warning("Drive upload skipped - google libs not installed (%s).", exc)
        return None

    try:
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


def generate_weekly_digest(database: dbmod.Database, config: dict) -> None:
    """Build (and optionally upload) the weekly PDF digest: every item fully
    evaluated in the last `window_days` (default 7) with score >= min_score,
    best scores first. Best-effort - a PDF or Drive failure logs a warning and
    returns without raising. Intended to be called by `main.run_weekly_digest()`
    (`python main.py --digest`), which the scheduler triggers every Friday 17:00."""
    report_cfg = config.get("report") or {}
    if not report_cfg.get("enabled", True):
        return

    min_score = int(report_cfg.get("min_score", 50))
    window_days = int(report_cfg.get("window_days", 7))

    # Window in UTC to match how `evaluated_at` is stored; half-open [start, end)
    # so adjacent weekly windows don't double-count an item on the boundary.
    end_utc = datetime.now(timezone.utc)
    start_utc = end_utc - timedelta(days=window_days)
    items = database.items_evaluated_between(start_utc, end_utc, min_score=min_score)
    if not items:
        log.info(
            "No items scored >= %d in the last %d days; skipping weekly PDF digest.",
            min_score, window_days,
        )
        return

    # Local-time equivalents for the filename and the human-readable header.
    end_local = end_utc.astimezone()
    start_local = start_utc.astimezone()
    out_dir = Path(report_cfg.get("out_dir", "reports"))
    pdf_path = out_dir / f"ai-radar-weekly-{end_local.strftime('%Y-%m-%d')}.pdf"
    subtitle = (
        f"{start_local.strftime('%Y-%m-%d')} to {end_local.strftime('%Y-%m-%d')}"
        f"  -  {len(items)} article(s) scoring >= {min_score}"
    )

    try:
        build_report_pdf(items, pdf_path, title="AI Radar - weekly digest", subtitle=subtitle)
        log.info("Wrote weekly digest PDF: %s (%d item(s)).", pdf_path, len(items))
    except Exception as exc:  # noqa: BLE001 - PDF build must never fail the job
        log.warning("Failed to build weekly digest PDF: %s", exc)
        return

    _upload_pdf_if_configured(pdf_path, report_cfg)


def _upload_pdf_if_configured(pdf_path: Path, report_cfg: dict) -> None:
    """Drive upload tail shared by the digest builder. Reads `report_cfg`
    (the `report` block), loads the right credential type, and uploads.
    Best-effort: logs and returns on any misconfiguration or failure."""
    drive_cfg = report_cfg.get("google_drive") or {}
    if not drive_cfg.get("enabled", False):
        return

    folder_id = os.environ.get(
        drive_cfg.get("folder_id_env", "GOOGLE_DRIVE_FOLDER_ID"), ""
    ).strip()
    if not folder_id:
        log.warning(
            "Drive upload enabled but GOOGLE_DRIVE_FOLDER_ID not set; "
            "leaving the PDF local only."
        )
        return

    # method: "oauth" (personal Gmail, default) or "service_account" (Workspace).
    method = (drive_cfg.get("method") or "oauth").strip().lower()
    if method == "service_account":
        sa_file = os.environ.get(
            drive_cfg.get("service_account_file_env", "GOOGLE_SERVICE_ACCOUNT_FILE"),
            "",
        ).strip()
        if not sa_file:
            log.warning(
                "Drive method=service_account but GOOGLE_SERVICE_ACCOUNT_FILE "
                "not set; leaving the PDF local only."
            )
            return
        creds = _load_service_account_credentials(sa_file)
    else:
        token_file = os.environ.get(
            drive_cfg.get("oauth_token_file_env", "GOOGLE_OAUTH_TOKEN_FILE"), ""
        ).strip()
        if not token_file:
            log.warning(
                "Drive method=oauth but GOOGLE_OAUTH_TOKEN_FILE not set; "
                "leaving the PDF local only."
            )
            return
        creds = _load_user_credentials(token_file)

    if creds is None:
        return  # helper already logged why; PDF stays local
    upload_to_drive(pdf_path, folder_id, creds)