# The speed redesign: why one run took 5+ hours, and how it became ~15 minutes

Study notes on the July 2026 refactor. This documents the *diagnosis method* as
much as the fix — the bug was invisible in the code and only visible in the DB.

---

## 1. The symptom vs. the disease

Three complaints, seemingly unrelated:

1. the run took 5+ hours and had to be killed;
2. "only 9 of 200 items were interesting";
3. no summaries / "why this matters" texts appeared.

All three were **one failure chain**. The DB told the story:

| status | count | meaning |
|---|---|---|
| REJECTED | 200 | triage said no |
| TRIAGED | 157 | **passed triage, never got the deep eval** |
| EVALUATED | 9 | the only items with summaries |
| NEW | 252 | never even triaged (run killed) |

166 items scored ≥ 50 — the scoring was fine. But pass 2 (summaries) only ran
*after* pass 1 finished **all** items, and pass 1 stalled for hours, so almost
nothing ever reached pass 2. Complaints 2 and 3 were just complaint 1 wearing
different hats.

**Lesson: when a pipeline "gives bad results", first check whether it actually
*ran*. Query the state store before touching the model or the prompt.**

---

## 2. Finding the stall: read the `model_used` column

Every item records which model judged it:

```
groq:llama-3.3-70b-versatile                                72 items
groq:llama-3.3-70b-versatile (fallback: ollama:qwen3:8b)   294 items
```

72 items on pure Groq, then everything on the fallback path. Why 72?

Every call re-sent the full system prompt — preferences (~420 tokens) + rubric
(~590 tokens) + boilerplate ≈ **1,100 tokens of fixed overhead** to score one
~200-token Reddit title. About 1,400 tokens/call total, and Groq's free tier
caps `llama-3.3-70b-versatile` at **100K tokens per DAY**:

```
100,000 TPD ÷ ~1,400 tokens/call ≈ 70 calls   ← observed: 72. Case closed.
```

After call ~72, every item fell back to `qwen3:8b` on the local CPU. Qwen3 is a
*thinking* model: it silently generates hundreds of chain-of-thought tokens
before emitting `{"score": 55}`. On a CPU that's ~1–6 minutes per item.
294 items × minutes each = the missing 5 hours. Plus a flat `time.sleep(6)`
after every item — even the local ones — ≈ 62 minutes of pure sleep.

**Lesson: a "fallback" that is 50× slower than the primary isn't a fallback,
it's a trap door. Always ask what the system does *after* the failover.**

---

## 3. The fix, part 1: pay the fixed cost once per batch

The 1,100-token system prompt is identical for every triage call, so the fix is
arithmetic, not cleverness — **amortize it**. `triage_batch()` sends ~20 items
per call and gets back `{"scores": [{"id": ..., "score": ...}, ...]}`:

```
before: 618 items × ~1,400 tokens = ~865K tokens, 618 calls
after:   31 calls × ~2,500 tokens = ~78K tokens,   31 calls   (~11× fewer tokens)
```

Details that make batching safe:

- Items are keyed by **DB id** in the prompt; the response maps back by id.
  An id the model forgets simply stays `NEW` and retries next run — a partial
  answer degrades gracefully instead of crashing.
- Ids not in the batch (model hallucinating) are dropped on parse.
- `set_triage_many()` applies a whole batch in one SQLite transaction and
  refuses to touch items that are no longer `NEW` (idempotent re-runs).
- The triage system prompt is slimmed: the rubric's summary/read-time sections
  only matter in pass 2, so triage splits them off (`rubric.split("## Summary
  instructions")[0]`) — the rubric file stays one file you edit freely.

---

## 4. The fix, part 2: per-model budgets (the free-tier cheat code)

Groq's free-tier limits are **per model**. Two passes on two models = two
independent daily budgets:

| pass | model | TPM | TPD | per-run spend |
|---|---|---|---|---|
| triage | `llama-3.1-8b-instant` | 6K | **500K** | ~31 batches × 2.5K ≈ 78K |
| deep eval | `llama-3.3-70b-versatile` | 12K | **100K** | ~30 evals × 3K ≈ 90K |

The cheap 8B is plenty for "is this relevant?" against a rubric; the 70B's
entire budget is reserved for what actually needs quality: the summaries.

Two knobs keep each pass inside its lane:

- **Pacing lives in the provider** (`triage_interval_seconds: 30`,
  `eval_interval_seconds: 20`), sized against the TPM caps above. It replaced
  the flat 6-second sleep in `main.py`, which had been delaying even the local
  Ollama calls where pacing is meaningless.
- **`max_deep_evals_per_run: 30`** caps pass 2, taking the *highest triage
  scores first*. Whatever doesn't fit stays `TRIAGED` and is drained by
  tomorrow's run — which now does deep-eval **first**, so summaries land even
  if a run later dies.

---

## 5. The fix, part 3: a fallback that knows minute from day

`FallbackProvider` used to treat every 429 the same. Now it reads Groq's error
message and splits two very different situations:

- **Minute limit (TPM/RPM)** — transient. Wait what Groq asked for
  (`Retry-After` header, or `"try again in 7.66s"` parsed from the message,
  capped at 90s), retry the primary twice, and only then use Ollama *for that
  one call*.
- **Daily limit (TPD/RPD)** — gone for hours. **Latch**: stop probing Groq for
  the rest of the run. And rather than grinding 30 full-article prompts through
  the CPU (measured: ~6 min each even with thinking off), the pipeline loops
  check `provider.latched` and **stop the pass** — unprocessed items stay
  `NEW`/`TRIAGED` and meet a fresh budget tomorrow.

Ollama itself got two speedups for the calls it *does* serve: `think: false`
(kills qwen3's hidden chain-of-thought — the single biggest local cost) and
`num_predict` caps on output length.

**Lesson: "retry", "fail over", and "stop and resume later" are three different
strategies. Pick per error class, not per exception type.**

---

## 6. Small fixes that mattered

- **HTML stripping** (`strip_html`): Reddit/RSS "summaries" arrive as HTML, so
  the model was reading `<!-- SC_OFF --><div class="md">…` — most of a 300-char
  snippet wasted on markup. Now tags are stripped and entities unescaped before
  any prompt.
- **Triage-note in the prompt**: the rubric says "lean lower when text is
  thin", which is right for a full article but was systematically depressing
  *every* triage score (triage input is thin by design). Triage now carries a
  note: judge potential, don't punish snippet brevity.
- **Reddit `hot` + lookback exemption**: sources switched from `new` (a noise
  firehose) to `hot`. But hot posts can be days old, and the 24h publish-date
  filter would have dropped them — so `RawItem.always_fresh` marks hot/top
  items as "relevant now" and exempts them. Dedup (content hash) still
  guarantees each post is only ever analysed once.
- **UI default**: the score slider defaulted to 75, hiding most of the (50–74)
  backlog — one more reason "only 9 items" appeared. Now defaults to the
  triage threshold.

---

## 7. The run, before and after

```
BEFORE  fetch ≤22 min → triage 618 items, one call each
        └─ call #73+: Groq budget dead → local thinking-model, ~minutes/item
        └─ +6s flat sleep per item  →  5+ hours, killed, 0 new summaries

AFTER   fetch ≤22 min
        → deep-eval ≤30 best TRIAGED leftovers (summaries land FIRST)
        → triage NEW in ~20-item batches on the 8B (seconds per batch)
        → deep-eval today's best survivors with the remaining budget
        →  ~15–30 min total, ~30 fresh summaries/day, $0
```

Measured on the live system: one 20-item triage batch = **1.7s** (vs ~2.5 min
for the same 20 items before); one Groq deep eval ≈ 2s + article fetch.

## 8. Why it's shaped this way (summary)

| Choice | Payoff |
|---|---|
| Batch triage keyed by DB id | 11× fewer tokens; partial answers degrade safely |
| Different Groq model per pass | two free daily budgets instead of one |
| Pacing inside the provider | Groq calls spaced, local calls never delayed |
| Minute-vs-day 429 handling | waits when waiting helps, stops when it can't |
| Deep-eval first + per-run cap | summaries appear even on a bad day; budget never blown |
| `always_fresh` for reddit hot | "hot now" beats "posted in the last 24h"; dedup prevents repeats |
