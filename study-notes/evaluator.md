# How `evaluator.py` was built

Study notes on the LLM layer: the provider abstraction, the two-pass design, and
why the JSON parsing is paranoid.

---

## 1. The problem it solves

This is the only part of the app that calls an LLM — so it's the only part that
costs money / hits rate limits, and the only part whose output is unpredictable
(models occasionally wrap JSON in prose or markdown). The file has two
responsibilities:

1. **Hide which LLM we're using** behind one interface, so switching Groq ↔
   Ollama is a one-line config change.
2. **Turn a messy article into a structured judgement** (score, summary, reasons,
   read time, tags) — reliably, even when the model misbehaves.

---

## 2. The provider abstraction

Two small classes, same shape:

```python
class GroqProvider:
    def complete(self, system, user) -> str: ...
    @property
    def name(self) -> str: ...   # "groq:llama-3.3-70b-versatile"

class OllamaProvider:
    def complete(self, system, user) -> str: ...
    @property
    def name(self) -> str: ...   # "ollama:qwen3:8b"
```

This is **duck typing** as a strategy pattern: both expose `.complete()` and
`.name`, so the `Evaluator` never knows or cares which one it holds. `.name` gets
stored in the DB (`model_used`) so you can later see which model judged what —
handy after you switch backends.

Key details:
- **Lazy imports.** `from groq import Groq` lives *inside* `GroqProvider.__init__`,
  not at the top of the file. So if you only ever run Ollama, you don't need the
  `groq` package installed (and vice versa). The unused backend never loads.
- **Native JSON mode.** Groq gets `response_format={"type": "json_object"}`;
  Ollama gets `format="json"`. Both providers can be *told* to emit JSON, which
  dramatically cuts malformed replies. (We still don't trust it — see §5.)
- **Low temperature (0.2).** This is judging, not creative writing — we want
  stable, repeatable scores, not variety.

### The factory: `make_provider`

```python
def make_provider(llm_cfg):
    which = llm_cfg["provider"]      # "groq" or "ollama"
    ...read model + API key from env...
```

It reads the `llm:` block from config.yaml, pulls the **API key from an
environment variable** (the config stores the *name* of the env var, never the
secret itself — keys stay out of the repo), and returns the right provider. This
function is the single switch point for the whole app.

---

## 3. Schemas live in code, prompts live in files

```python
TRIAGE_SCHEMA = '{"score": <integer 0-100>}'
EVAL_SCHEMA   = '{"score": ..., "summary": ..., "reasons": ...,
                  "read_time_minutes": <integer>, "tags": [...]}'
```

Deliberate separation of concerns:
- **`prompts/preferences.md` and `prompts/rubric.md`** = *what to judge and how*
  (you edit these constantly).
- **The JSON schema** = *the output contract* (stable, belongs with the code that
  parses it).

Keeping the schema out of the prompt files means you can rewrite your preferences
freely without ever risking the machine-readable format the DB depends on.

---

## 4. The two-pass design (the cost lever)

Full article text is the expensive input. So evaluation is split:

- **`triage(item)`** — pass 1. Sends only **title + a 600-char snippet** and asks
  for *just a score*. Cheap, runs on every fresh item. Output: `{"score": n}`.
- **`evaluate(item, full_text)`** — pass 2. Runs **only** for items that cleared
  the triage threshold (decided in `main.py`/`db.py`). Sends the full text
  (truncated to `max_text_chars`) and asks for the score, 2–3 sentence summary,
  one-line reasons, **read-time estimate**, and tags.

The payoff: you only pay full-text tokens on the ~20% of items worth a real read.
On a busy news day that's what keeps you inside Groq's free tier.

Both passes call `_system(schema)`, which assembles the system prompt:

```
role intro
## Reader preferences   <- preferences.md
## Scoring rubric        <- rubric.md
## Output                <- the JSON schema for this pass
```

Same preferences + rubric, different schema per pass.

---

## 5. Paranoid JSON parsing (`_parse_json`)

Even with JSON mode, models occasionally return ```json fences``` or a stray
sentence. If a single bad reply threw an exception, one weird article could abort
the whole nightly run. So parsing degrades gracefully, in order:

1. Strip leading/trailing ``` fences if present.
2. `json.loads` the text.
3. If that fails, grab the substring between the first `{` and last `}` and try
   again.
4. If *that* fails, log a warning and return `{}`.

Because it returns `{}` on total failure, the callers use `.get(...)` with
defaults — so a broken response becomes a score-0 item, not a crash.

---

## 6. Defensive output shaping

The model's output is also clamped before it touches the DB:

- **`_clamp_score`** forces score into 0–100 (a model returning `"95%"` or `120`
  is coerced sanely).
- **`_clamp_int(read_time, 1, 600)`** keeps read-time a sane positive integer.
- **`tags`** is forced into a list, stringified, and capped at 6, then stored as
  a JSON string (`tags_json`) — matching what `db.set_evaluation` expects.

The rule of thumb: **never let raw model output reach the database unchecked.**

---

## 7. Why it's shaped this way (summary)

| Choice | Payoff |
|---|---|
| Two providers, same interface | switch Groq ↔ Ollama in one config line |
| Lazy imports | only install the backend you use |
| Native JSON mode + low temp | fewer malformed, more stable judgements |
| Schema in code, prompts in files | edit preferences freely, format stays safe |
| Two-pass triage → deep eval | pay full-text tokens only when it's worth it |
| Tolerant parse + clamping | one bad reply can't crash or corrupt the run |
