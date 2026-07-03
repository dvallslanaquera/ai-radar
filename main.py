"""AI Radar - nightly orchestrator.

Run once (Windows Task Scheduler triggers it at 7am):

    python main.py

Pipeline:
    1. load config, sources, and your two prompt files
    2. fetch every enabled source, normalize, keep last 24h
    3. insert into SQLite, skipping anything already seen (dedup)
    4. deep-eval FIRST: full read + summary for items already past triage
       (leftovers from previous runs), best scores first - so summaries land
       even if this run is interrupted later
    5. pass 1 - batched triage on every NEW item (~20 items per LLM call)
    6. deep-eval today's survivors with whatever eval budget remains
    7. print a short report

Every step writes to the DB as it goes, so a crash/rate-limit resumes cleanly.
The per-run deep-eval cap (`max_deep_evals_per_run`) keeps the eval model
inside its free-tier daily token budget; the rest waits for tomorrow.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import trafilatura
import yaml

import db as dbmod
import fetcher
import reporter
from evaluator import Evaluator, make_provider, strip_html

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Kept dependency-free on purpose. Existing environment variables win
    (`setdefault`), so the shell or Task Scheduler can still override a key
    per-run. Secrets live here, never in config.yaml (which is committed).
    """
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def extract_text(url: str, fallback: str) -> str:
    """Pull readable article text; fall back to the abstract/snippet on failure."""
    if not url:
        return fallback
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False, favor_recall=True)
            if text and len(text) > len(fallback):
                return text
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        log.debug("extract failed for %s: %s", url, exc)
    return fallback


def deep_eval(database: dbmod.Database, evaluator: Evaluator, budget: int, provider) -> int:
    """Full read + summary for up to `budget` TRIAGED items, best scores first.

    Returns how many items were actually evaluated. No sleep here: the Groq
    provider paces itself, and pacing a local Ollama call would be pointless.

    If the eval model's DAILY budget dies mid-pass, we stop instead of grinding
    the remaining full-article prompts through the local CPU model (~6 min
    each). Nothing is lost: the items stay TRIAGED and tomorrow's run, with a
    fresh Groq budget, picks them up first.
    """
    if budget <= 0 or getattr(provider, "latched", False):
        return 0
    survivors = database.items_for_deep_eval(limit=budget)
    if not survivors:
        return 0
    log.info("Deep eval: %d items (highest triage scores first)...", len(survivors))
    done = 0
    for item in survivors:
        try:
            text = extract_text(item.url, strip_html(item.raw_text) or item.title)
            result = evaluator.evaluate(item, text)
            database.set_evaluation(item.id, result, provider.name)
            done += 1
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the batch
            log.warning("eval failed for #%s (%s): %s", item.id, item.title[:60], exc)
        if getattr(provider, "latched", False):
            log.warning(
                "Eval model's daily budget is spent; stopping the deep-eval pass "
                "(%d done). The rest stays TRIAGED for tomorrow's fresh budget.", done,
            )
            break
    return done


def _fmt_elapsed(seconds: float) -> str:
    """Human-friendly runtime: '4m 32s' or '1h 12m'."""
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def run() -> None:
    run_start_mono = monotonic()
    run_start_dt = datetime.now(timezone.utc)  # used to scope the PDF digest

    load_dotenv()  # pull API keys from .env into the environment first
    config = load_yaml("config.yaml")
    resources = load_yaml("resources.yaml")
    preferences = load_text(config["prompts"]["preferences"])
    rubric = load_text(config["prompts"]["rubric"])

    pipe = config["pipeline"]
    settings = {
        "user_agent": config["http"]["user_agent"],
        "reddit_budget_seconds": pipe.get("reddit_budget_minutes", 22) * 60,
    }

    database = dbmod.Database(config["db"]["path"])

    # 1-3. fetch -> dedup -> store
    t = monotonic()
    log.info("Fetching sources...")
    raw_items = fetcher.fetch_all(resources, settings, pipe["lookback_hours"])
    inserted = database.insert_items(raw_items)
    log.info("Inserted %d new items (rest were duplicates). (%.1fs)", inserted, monotonic() - t)

    # Separate providers per pass: Groq's free-tier limits are per model, so
    # triage (cheap 8B) and deep eval (70B) each get their own daily budget.
    triage_provider = make_provider(config["llm"], "triage")
    eval_provider = make_provider(config["llm"], "eval")
    evaluator = Evaluator(
        triage_provider, eval_provider, preferences, rubric, pipe["max_text_chars"]
    )
    threshold = pipe["triage_threshold"]
    reject_cap = pipe.get("reject_score_cap", 25)
    batch_size = pipe.get("triage_batch_size", 20)
    eval_budget = pipe.get("max_deep_evals_per_run", 30)

    # 4. deep-eval FIRST: clear as much of the TRIAGED backlog as the budget
    #    allows, so summaries land even if this run dies later.
    t = monotonic()
    eval_budget -= deep_eval(database, evaluator, eval_budget, eval_provider)
    log.info("Backlog deep-eval pass done. (%.1fs)", monotonic() - t)

    # 5. pass 1 - batched triage on every NEW item
    t = monotonic()
    new_items = database.get_by_status(dbmod.NEW)
    log.info(
        "Pass 1 (triage): %d items in batches of %d...", len(new_items), batch_size
    )
    for start in range(0, len(new_items), batch_size):
        chunk = new_items[start : start + batch_size]
        try:
            scores = evaluator.triage_batch(chunk)
            passed, rejected = database.set_triage_many(
                scores, threshold, triage_provider.name, reject_cap
            )
            unscored = len(chunk) - len(scores)
            log.info(
                "  batch %d-%d: %d passed, %d rejected%s",
                start + 1, start + len(chunk), passed, rejected,
                f", {unscored} unscored (stay NEW, retried next run)" if unscored else "",
            )
        except Exception as exc:  # noqa: BLE001 - one bad batch must not stop the run
            log.warning("triage batch at offset %d failed: %s", start, exc)
        if getattr(triage_provider, "latched", False):
            log.warning(
                "Triage model's daily budget is spent; stopping triage (the "
                "remaining %d items stay NEW for tomorrow's fresh budget).",
                max(0, len(new_items) - start - batch_size),
            )
            break
    log.info("Triage pass done. (%.1fs)", monotonic() - t)

    # 6. deep-eval today's survivors with whatever budget is left
    t = monotonic()
    deep_eval(database, evaluator, eval_budget, eval_provider)
    log.info("Survivor deep-eval pass done. (%.1fs)", monotonic() - t)

    # 7. report + runtime log
    counts = database.status_counts()
    elapsed = monotonic() - run_start_mono
    log.info("Done. Status counts: %s", counts)
    log.info("Run finished in %.1fs (%s).", elapsed, _fmt_elapsed(elapsed))
    if counts.get(dbmod.TRIAGED):
        log.info(
            "%d triaged items still await a deep eval; tomorrow's run picks up "
            "the best-scored ones first (raise max_deep_evals_per_run to drain "
            "faster).", counts[dbmod.TRIAGED],
        )

    # Persist this run's runtime + counts for the Streamlit "last run" view.
    try:
        database.record_run(run_start_dt, elapsed, counts, inserted)
    except Exception as exc:  # noqa: BLE001 - logging must never fail the run
        log.warning("Could not record run history: %s", exc)

    # 8. PDF digest of this run's score>=50 winners -> Google Drive.
    try:
        reporter.maybe_generate_and_upload(database, run_start_dt, config)
    except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
        log.warning("PDF/Drive digest step failed: %s", exc)

    log.info("Open the backlog with:  streamlit run app.py")


if __name__ == "__main__":
    run()
