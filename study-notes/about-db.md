### TL;DR
This document outlines the architecture of a single-table SQLite database design optimized for an LLM-driven content pipeline. It leverages a strict status lifecycle state machine, SHA-256 content hashing for absolute idempotency, WAL mode for safe concurrent UI/backend access, and a clean abstraction layer to completely isolate SQL logic from the application.

---

## The Big Idea

This file serves two primary purposes:
* **Defines the Schema:** A single centralized table (`items`) where every piece of content resides throughout its entire lifecycle.
* **Wraps DB Access:** Encapsulates all database operations within a `Database` class. This ensures `main.py` and `app.py` never interact with raw SQL directly, relying instead on clean methods like `insert_items()` or `query_items()`.

> **Architecture Design Choice:** Using a single table with a `status` column—rather than separate tables for "inbox", "backlog", or "read"—ensures an item remains in the exact same row its entire life. This makes the pipeline fully resumable and idempotent: re-running processes will never duplicate data because every item occupies exactly one known state at any given time.

---

## 1. The Status Lifecycle (The Heart of It)

NEW ──> TRIAGED ──> EVALUATED ──> READ / ARCHIVED
│
└──> REJECTED

These statuses are managed as string constants (`NEW = "NEW"`, etc.). Each item's `status` column reflects one of the following states:

* **`NEW`** — Fetched and stored, but not yet evaluated or judged.
* **`TRIAGED`** — Passed the initial cheap pass-1 score filter ($\ge \text{threshold}$) and is waiting for a full content read.
* **`REJECTED`** — Failed the pass-1 score threshold. The row is explicitly retained to prevent re-evaluation but remains hidden from the main UI view.
* **`EVALUATED`** — Completed the full content read; contains generated summaries, reasons, tags, and estimated read time. This functions as your active backlog.
* **`READ / ARCHIVED`** — Handled and acted upon by the user within the UI.

**Why this matters:** `main.py` queries "give me everything that's `NEW`" to execute pass 1, then requests "give me everything `TRIAGED`" to run pass 2. The database state inherently drives the application's to-do list.

---

## 2. Deduplication — `content_hash()`

```python
def content_hash(url, title):
    basis = (url or title or "").strip().lower()
    return sha256(basis).hexdigest()
```

This function provides your absolute idempotency guarantee.

* **Preventing Duplication:** The same article frequently appears across multiple feeds (e.g., an arXiv paper shared on both Hacker News and Reddit). By hashing the URL into a 64-character fingerprint and applying a UNIQUE constraint to that column, re-running the fetcher silently skips existing items.
* **Cost Efficiency:** This mechanism guarantees you never pay for duplicate LLM API tokens on identical content.
* **Fallback Behavior:** If a URL is entirely missing, the system gracefully falls back to hashing the title string.

---

## 3. The Item Table

Columns are logically organized by the specific pipeline phase in which they are populated:

| Group | Columns | Filled By |
| --- | --- | --- |
| Provenance | source, source_type, url, content_hash | fetcher |
| Content | title, author, published_at, fetched_at, raw_text | fetcher |
| State | status | every stage |
| Evaluation | score, summary, reasons, tags, read_time_minutes, model_used, evaluated_at | evaluator |

### Key Structural Decisions

* **`published_at`:** Set as nullable and timezone-aware. This field drives your 24-hour time filters, but handles sources that fail to provide clean date metadata.
* **`tags`:** Stored directly as a JSON string because SQLite lacks a native array data type. Serialization and deserialization occur smoothly at the application edges.
* **`model_used`:** Tracks the specific LLM that evaluated the item. This is critical for auditing performance when switching models (e.g., Groq $\leftrightarrow$ Ollama).
* **`read_time_minutes`:** An integer field representing the LLM's estimated reading duration.

---

## 4. The Database Class

The constructor enables Write-Ahead Logging (WAL) mode:

```sql
PRAGMA journal_mode=WAL;
```

* **Concurrency Management:** This configuration allows a 7:00 AM background batch job to execute write operations while a Streamlit UI concurrently reads data, preventing database lock errors.
* **Safe Operations:** Standard SQLite configurations would cause access conflicts; this single line makes a simultaneous "background job + live UI" architecture entirely safe on a single file.

### Core Class Methods

#### Ingestion (Used by fetcher)

* **`insert_items(raw_items)`:** Computes hashes, filters out in-batch duplicates, runs a single bulk query to identify pre-existing hashes, and inserts only genuinely new records. This bulk-checking approach is significantly faster and cleaner than catching individual row exceptions.

#### Pipeline Reads (Used by main.py)

* **`get_by_status(status)`:** Fetches all data items currently matching a specific state.
* **`items_for_deep_eval()`:** A clean convenience wrapper that pulls all items marked as `TRIAGED`.

#### Pipeline Writes (Used by main.py)

* **`set_triage(id, score, threshold, model)`:** Records the pass-1 score and routes the item state to either `TRIAGED` or `REJECTED` based on the threshold.
* **`set_evaluation(id, result, model)`:** Stores the full generated summary, reasoning, tags, and read-time metrics, updating the state to `EVALUATED`.
* **`set_status(id, status)`:** A generic state transition method used primarily by the UI to shift items to `READ` or `ARCHIVED`.

#### UI Reads (Used by app.py)

* **`query_items(statuses, sources, min_score, search, limit)`:** Exposes the backlog query, natively sorted by score descending, complete with dynamic optional filters.
* **`distinct_sources()`:** Dynamically populates your UI sidebar filter choices.
* **`status_counts()`:** Returns high-level operational metrics displayed at the top of the dashboard (e.g., "12 in backlog, 3 read").