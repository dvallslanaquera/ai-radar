"""LLM evaluation for AI Radar.

Two things live here:

1. A provider abstraction so you can switch backends from config with one key:
       - GroqProvider   (primary; free tier, paced to stay under its TPM limit)
       - OllamaProvider (fallback; local Qwen 3 8B with thinking disabled)
   Both expose `.complete(system, user, max_tokens=None) -> str` and a `.name`.

   Groq's free-tier limits are PER MODEL, so the two passes run on different
   models to get two separate daily token budgets:
       - triage: llama-3.1-8b-instant    (500K tokens/day - the cheap filter)
       - eval:   llama-3.3-70b-versatile (100K tokens/day - the quality pass)

2. The Evaluator, which builds prompts from your editable prompt files plus a
   JSON output schema, and runs the two passes:
       - triage_batch(): one call scores ~20 items (title + snippet each), so
         the ~1K-token system prompt is paid once per batch, not once per item
       - evaluate():     full text -> score, summary, reasons, tags, read time
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import os
import re
from time import monotonic, sleep

log = logging.getLogger("evaluator")

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Reddit/RSS 'summaries' arrive as HTML; the model should see plain text.

    Feeding `<!-- SC_OFF --><div class="md">...` into a 300-char snippet wastes
    most of it on markup and measurably hurts triage scores.
    """
    if not text:
        return ""
    if "<" not in text and "&" not in text:
        return text.strip()
    cleaned = _TAG_RE.sub(" ", text)
    cleaned = htmllib.unescape(cleaned)
    return " ".join(cleaned.split())


# =====================================================================
# Providers
# =====================================================================
class GroqProvider:
    def __init__(
        self,
        model: str,
        api_key: str,
        min_interval: float = 0.0,
        max_retries: int | None = None,
    ):
        from groq import Groq  # imported lazily so the other backend isn't required

        if not api_key:
            raise RuntimeError("GROQ API key is empty - set it in your environment.")
        self.model = model
        # Self-pacing: wait `min_interval` seconds between calls so the free
        # tier's tokens-per-minute cap isn't hit. Living here (not a flat sleep
        # in main.py) means Ollama calls are never pointlessly delayed.
        self.min_interval = min_interval
        self._next_ok = 0.0
        # max_retries=None keeps the SDK default (it backoffs on 429s). Set 0 when
        # a fallback is configured so FallbackProvider owns the retry decision.
        kwargs = {"api_key": api_key}
        if max_retries is not None:
            kwargs["max_retries"] = max_retries
        self._client = Groq(**kwargs)

    def complete(self, system: str, user: str, max_tokens: int | None = None) -> str:
        wait = self._next_ok - monotonic()
        if wait > 0:
            sleep(wait)
        try:
            extra = {"max_completion_tokens": max_tokens} if max_tokens else {}
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},  # ask Groq for strict JSON
                **extra,
            )
        finally:
            # Set even on failure so a 429'd call still spaces the next attempt.
            self._next_ok = monotonic() + self.min_interval
        return resp.choices[0].message.content

    @property
    def name(self) -> str:
        return f"groq:{self.model}"


class OllamaProvider:
    def __init__(self, model: str, host: str, api_key: str | None = None, think: bool | None = None):
        import ollama  # lazy import

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self.model = model
        # think=False turns off qwen3's chain-of-thought. On CPU the hidden
        # <think> block is what turns a 10s call into a 1-2 minute one.
        # None -> don't send the flag at all (non-thinking models reject it).
        self.think = think
        self._client = ollama.Client(host=host, headers=headers)

    def complete(self, system: str, user: str, max_tokens: int | None = None) -> str:
        options = {"temperature": 0.2}
        if max_tokens:
            options["num_predict"] = max_tokens
        kwargs = {}
        if self.think is not None:
            kwargs["think"] = self.think
        resp = self._client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format="json",  # ask Ollama for strict JSON
            options=options,
            **kwargs,
        )
        return resp["message"]["content"]

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"


# =====================================================================
# Rate-limit classification
# =====================================================================
def _is_rate_limit(exc) -> bool:
    """True if exc looks like a token/rate-limit error (429 / quota / too-many-requests)."""
    if getattr(exc, "status_code", None) == 429:
        return True
    if "RateLimit" in type(exc).__name__:
        return True
    msg = str(exc).lower()
    return any(k in msg for k in ("rate limit", "rate_limit", "quota", "too many requests", "429"))


def _is_daily_limit(exc) -> bool:
    """A DAILY cap (TPD/RPD) won't reset for hours - retrying Groq is pointless.

    Groq's 429 message names the limit, e.g. "... on tokens per day (TPD):
    Limit 100000, Used 99035 ...".
    """
    msg = str(exc).lower()
    return any(k in msg for k in ("per day", "(tpd)", "(rpd)", "daily"))


_WAIT_RE = re.compile(r"try again in (?:(\d+)m)?([\d.]+)s", re.IGNORECASE)


def _retry_after_seconds(exc) -> float | None:
    """How long Groq asked us to wait: Retry-After header, else parse the message."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            value = headers.get("retry-after")
            if value:
                return float(value)
        except (TypeError, ValueError):
            pass
    m = _WAIT_RE.search(str(exc))
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return None


class FallbackProvider:
    """Primary provider with a rate-limit-aware escape hatch.

    - MINUTE limit (TPM/RPM): transient. Wait what Groq asked (capped) and
      retry the primary a couple of times; only then use the fallback for
      *this call*.
    - DAILY limit (TPD/RPD): gone for hours. Latch onto the fallback for the
      rest of the run instead of re-probing Groq on every item.
    - Anything else (auth, network, malformed response) re-raises so the
      caller can skip the item instead of silently masking a real failure.
    """

    MINUTE_RETRIES = 2
    MAX_WAIT = 90.0

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.latched = False  # True once the primary's daily budget is spent

    @property
    def name(self) -> str:
        return f"{self.primary.name} (fallback: {self.fallback.name})"

    def complete(self, system: str, user: str, max_tokens: int | None = None) -> str:
        if self.latched:
            return self.fallback.complete(system, user, max_tokens)
        attempts = 0
        while True:
            try:
                return self.primary.complete(system, user, max_tokens)
            except Exception as exc:  # noqa: BLE001 - classify rate-limit vs real failure
                if not _is_rate_limit(exc):
                    raise
                if _is_daily_limit(exc):
                    self.latched = True
                    log.warning(
                        "%s hit its DAILY limit; using %s for the rest of the run",
                        self.primary.name, self.fallback.name,
                    )
                    return self.fallback.complete(system, user, max_tokens)
                attempts += 1
                if attempts > self.MINUTE_RETRIES:
                    log.warning(
                        "%s still minute-limited after %d retries; using %s for this call",
                        self.primary.name, self.MINUTE_RETRIES, self.fallback.name,
                    )
                    return self.fallback.complete(system, user, max_tokens)
                wait = min(_retry_after_seconds(exc) or 20.0, self.MAX_WAIT)
                log.info("%s minute-limited; waiting %.0fs then retrying", self.primary.name, wait)
                sleep(wait)


# =====================================================================
# Provider factory
# =====================================================================
def _build_single(llm_cfg: dict, which: str, purpose: str, max_retries: int | None = None):
    """Build one provider by name ('groq' or 'ollama') for one pass ('triage'/'eval')."""
    which = which.lower()
    if which == "groq":
        c = llm_cfg["groq"]
        return GroqProvider(
            model=c[f"{purpose}_model"],
            api_key=os.environ.get(c["api_key_env"], ""),
            min_interval=float(c.get(f"{purpose}_interval_seconds", 0)),
            max_retries=max_retries,
        )
    if which == "ollama":
        c = llm_cfg["ollama"]
        host = c.get("host", "http://localhost:11434")
        api_key = os.environ.get(c.get("api_key_env", ""), "") or None
        # A remote host (Ollama Cloud) requires a key; without one every call
        # 401s silently. Fail loudly up front instead of mid-batch.
        if api_key is None and not any(h in host for h in ("localhost", "127.0.0.1")):
            raise RuntimeError(
                f"Ollama host {host!r} needs an API key, but env var "
                f"{c.get('api_key_env')!r} is empty. Set it in .env."
            )
        return OllamaProvider(model=c["model"], host=host, api_key=api_key, think=c.get("think"))
    raise ValueError(f"Unknown llm.provider: {which!r} (expected 'groq' or 'ollama')")


def make_provider(llm_cfg: dict, purpose: str):
    """Build the provider for one pass ('triage' or 'eval').

    The two passes get separate provider instances because Groq's free-tier
    limits are per model: each instance paces itself and latches onto the
    fallback independently, so triage burning through the 8B's budget never
    touches the 70B's.
    """
    primary_which = llm_cfg.get("provider", "groq").lower()
    fb_which = llm_cfg.get("fallback")
    if not fb_which:
        return _build_single(llm_cfg, primary_which, purpose)
    # Disable the Groq SDK's own 429 backoff: FallbackProvider decides whether
    # to wait (minute limit) or switch to the fallback (daily limit).
    retries = 0 if primary_which == "groq" else None
    primary = _build_single(llm_cfg, primary_which, purpose, max_retries=retries)
    fallback = _build_single(llm_cfg, fb_which, purpose)
    log.info("LLM %s pass: %s -> %s on rate limit", purpose, primary.name, fallback.name)
    return FallbackProvider(primary, fallback)


# =====================================================================
# Output schemas (kept in code so the prompt files stay about *content*)
# =====================================================================
TRIAGE_BATCH_SCHEMA = '{"scores": [{"id": <item id>, "score": <integer 0-100>}, ...]}'
EVAL_SCHEMA = (
    '{"score": <integer 0-100>, '
    '"summary": "<2-3 sentence what-and-why>", '
    '"reasons": "<one line, addressed to you: why this matches your interests>", '
    '"read_time_minutes": <integer>, '
    '"tags": ["<short topic tag>", "..."]}'
)

# Counters the rubric's "lean lower when text is thin" rule, which is right for
# the deep pass but would systematically depress EVERY triage score (triage
# input is thin by design).
TRIAGE_NOTE = (
    "This is a fast first-pass filter over titles and short snippets. Judge each "
    "item's likely value from what is shown; do not penalize an item merely "
    "because the snippet itself is short."
)

# Generous ceilings so a truncated reply never corrupts a batch; output tokens
# count against the daily budget, so don't leave them unbounded either.
_EVAL_MAX_TOKENS = 700


def _parse_json(raw: str) -> dict:
    """Tolerant JSON parse: strips code fences, falls back to the first {...}."""
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    log.warning("Could not parse LLM JSON: %.120s", raw)
    return {}


class Evaluator:
    def __init__(
        self,
        triage_provider,
        eval_provider,
        preferences: str,
        rubric: str,
        max_text_chars: int = 6000,
    ):
        self.triage_provider = triage_provider
        self.eval_provider = eval_provider
        self.preferences = preferences
        self.rubric = rubric
        # The rubric's summary/read-time sections only apply to the deep pass;
        # sending them at triage would waste ~40% of every triage call.
        self.triage_rubric = rubric.split("## Summary instructions")[0].strip()
        self.max_text_chars = max_text_chars

    # --- prompt assembly ---------------------------------------------
    def _system(self, schema: str, rubric: str, note: str = "", second_person: bool = False) -> str:
        parts = [
            "You are a personal research assistant that curates AI content for "
            "one specific reader. Use the reader's preferences and the scoring "
            "rubric below to judge each item.",
            f"## Reader preferences\n{self.preferences}",
            f"## Scoring rubric\n{rubric}",
        ]
        if note:
            parts.append(f"## This pass\n{note}")
        output = ""
        if second_person:
            output += (
                "In any 'summary' and 'reasons' text, address the reader directly "
                "in the second person ('you', 'your'). Never use the first person "
                "('I', 'me', 'my').\n"
            )
        output += (
            f"Respond with ONLY valid JSON matching this shape: {schema}\n"
            "No markdown, no commentary, no extra keys."
        )
        parts.append(f"## Output\n{output}")
        return "\n\n".join(parts)

    # --- pass 1: batched triage --------------------------------------
    def triage_batch(self, items) -> dict[int, int]:
        """Score a whole batch of items in ONE call. Returns {item_id: score}.

        Items the model failed to score are simply absent from the result;
        they stay NEW in the DB and are retried on the next run.
        """
        blocks = []
        for it in items:
            block = f"id={it.id} | source: {it.source}\ntitle: {strip_html(it.title)}"
            snippet = strip_html(it.raw_text)[:300]
            if snippet:
                block += f"\nsnippet: {snippet}"
            blocks.append(block)
        user = (
            f"Score the relevance of each of these {len(items)} items. "
            "Return one entry per item, keyed by its id.\n\n" + "\n\n".join(blocks)
        )
        system = self._system(TRIAGE_BATCH_SCHEMA, self.triage_rubric, note=TRIAGE_NOTE)
        max_tokens = 100 + 40 * len(items)
        data = _parse_json(self.triage_provider.complete(system, user, max_tokens))

        rows = data.get("scores", data if isinstance(data, list) else [])
        valid_ids = {it.id for it in items}
        out: dict[int, int] = {}
        for row in rows if isinstance(rows, list) else []:
            try:
                item_id = int(row["id"])
            except (TypeError, ValueError, KeyError):
                continue
            if item_id in valid_ids:
                out[item_id] = _clamp_score(row.get("score"))
        return out

    # --- pass 2: full evaluation -------------------------------------
    def evaluate(self, item, full_text: str) -> dict:
        text = (full_text or strip_html(item.raw_text) or item.title)[: self.max_text_chars]
        user = (
            f"Source: {item.source}\n"
            f"Title: {item.title}\n"
            f"URL: {item.url}\n"
            f"Content:\n{text}\n\n"
            "Score it, summarize it, give your one-line reasons, estimate the "
            "read time in minutes, and add a few topic tags."
        )
        system = self._system(EVAL_SCHEMA, self.rubric, second_person=True)
        data = _parse_json(self.eval_provider.complete(system, user, _EVAL_MAX_TOKENS))
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        return {
            "score": _clamp_score(data.get("score")),
            "summary": str(data.get("summary", "")).strip(),
            "reasons": str(data.get("reasons", "")).strip(),
            "read_time_minutes": _clamp_int(data.get("read_time_minutes"), lo=1, hi=600),
            "tags_json": json.dumps([str(t) for t in tags][:6]),
        }


def _clamp_score(value) -> int:
    return _clamp_int(value, lo=0, hi=100)


def _clamp_int(value, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, n))
