# How `fetcher.py` was built

Study notes on the ingestion layer: what problem it solves, the design choices,
and a walkthrough of every section.

---

## 1. The problem it solves

We pull from very different sources — arXiv's Atom API, Hugging Face's JSON API,
Hacker News via Algolia, Reddit's JSON, GitHub Trending (HTML scrape), and
arbitrary RSS feeds. Each speaks a different protocol and returns a different
shape.

If the rest of the app had to know about all of those, every new source would
ripple through `db.py`, `main.py`, and `app.py`. So the whole job of this file is
**translation**: turn N messy formats into **one clean shape** and hand that
upward. Nothing downstream knows or cares where an item came from.

---

## 2. The core idea: one normalized record (`RawItem`)

```python
@dataclass
class RawItem:
    source: str            # display name from resources.yaml ("Hacker News")
    source_type: str       # which adapter produced it ("hackernews")
    url: str
    title: str
    author: str = ""
    published_at: datetime | None = None   # tz-aware UTC, drives the 24h filter
    raw_text: str = ""     # abstract / selftext / snippet (may be empty)
```

This is a **contract**. Every adapter — no matter the source — must return a list
of these. A dataclass (not a dict) makes the contract explicit: misspell a field
and Python complains immediately, instead of a silent `None` three files later.

`published_at` is intentionally `Optional` because not every source gives a clean
date, and `raw_text` is intentionally allowed to be empty because some sources
(e.g. a bare link) have no body yet — the full text gets fetched later, in
`main.py`, only for items worth it.

---

## 3. The adapter pattern + a registry

Each source type is one function with the **same signature**:

```python
def fetch_xxx(cfg: dict, settings: dict) -> list[RawItem]
```

- `cfg`  = that source's block from resources.yaml (its url, keywords, limits…)
- `settings` = global stuff (the shared User-Agent string)

Then they're wired up in a plain dict:

```python
FETCHERS = {
    "rss": fetch_rss,
    "arxiv": fetch_arxiv,
    "huggingface": fetch_huggingface,
    "hackernews": fetch_hackernews,
    "reddit": fetch_reddit,
    "github_trending": fetch_github_trending,
}
```

This registry is the trick that makes the orchestrator a tiny loop instead of a
giant `if/elif`. The `type:` field in resources.yaml is literally the dict key.
**Adding a source type = write one function + add one line here.** Adding another
*RSS* feed needs no code at all — just a YAML entry.

---

## 4. Date handling (the fiddly part)

Different sources express time differently, so there are two helpers that both
normalize to **timezone-aware UTC** (mixing naive and aware datetimes is a
classic Python bug — comparisons throw):

- `_from_struct(struct_time)` — feedparser hands back a `time.struct_time`
  (used by RSS and arXiv). Converted via `mktime` → UTC datetime.
- `_parse_date(value)` — everything else:
  - epoch seconds as `int/float` (Reddit's `created_utc`, HN's `created_at_i`),
  - ISO strings (Hugging Face), parsed with `dateutil`.
  - It always returns UTC: if the parsed value is naive, we *assume* UTC; if
    aware, we *convert* to UTC.

Every parse is wrapped so a weird date string returns `None` instead of crashing.

---

## 5. The adapters, one by one

- **`fetch_rss`** — the simplest; `feedparser.parse(url)` does the network call
  and parsing. Maps `link/title/author/summary` and reads the published or
  updated time. Used by every blog, Substack, Zenn/Qiita.

- **`fetch_arxiv`** — builds an API query (`cat:cs.AI OR cat:cs.LG …`, newest
  first), fetches with `httpx` for timeout control, then feeds the **Atom**
  response back into `feedparser` (arXiv returns Atom, so we reuse the RSS
  parser). `raw_text` is the abstract.

- **`fetch_huggingface`** — uses HF's JSON API (`/api/daily_papers`) instead of
  RSS, because it's more reliable and structured. Reaches into `row["paper"]`
  for title/summary/authors and builds the `huggingface.co/papers/<id>` URL.

- **`fetch_hackernews`** — loops your keywords, calling Algolia's
  `search_by_date` with a `points>=N` numeric filter so low-signal stories never
  arrive. Falls back to the HN discussion link when a story has no external URL.

- **`fetch_reddit`** — hits `reddit.com/r/<sub>/<listing>.json`. The one gotcha:
  Reddit **rejects requests without a real User-Agent** (you'd get 429s), so we
  pass `settings["user_agent"]`. Body comes from `selftext`.

- **`fetch_github_trending`** — Trending has **no stable API**, so we scrape with
  BeautifulSoup (`article.Box-row` → repo link + description). Trending pages
  carry no per-repo date, so we stamp `published_at = now` (it's trending *today*
  by definition) which keeps it inside the 24h window.

---

## 6. Orchestration + resilience (`fetch_all`)

```python
for src in resources["sources"]:
    if not src.get("enabled", True): continue
    adapter = FETCHERS.get(src["type"])
    try:
        collected.extend(adapter(src, settings))
    except Exception as exc:
        log.warning("%s FAILED: %s", src["name"], exc)   # skip, don't crash
```

Two deliberate properties:

1. **`enabled` flag** — toggle a source off in YAML without deleting it.
2. **Per-source isolation** — each adapter runs in its own try/except. A dead
   feed URL or a flaky API logs one warning and the run continues. With ~15
   sources, you never want one 404 to throw away the other 14.

Finally, `_within_lookback` applies the **24h filter centrally** — one place, not
scattered across six adapters. Note the deliberate choice: items with **no date
are kept** (treated as fresh) rather than silently dropped, so a source with bad
date metadata doesn't vanish.

---

## 7. Why it's shaped this way (summary)

| Choice | Payoff |
|---|---|
| One `RawItem` contract | downstream code is source-agnostic |
| Adapter + registry | new source = one function + one line |
| RSS handled by config | new feed = zero code |
| Central UTC date helpers | no naive/aware datetime bugs |
| Per-source try/except | one broken source can't kill the run |
| Central 24h filter | single source of truth for "fresh" |
