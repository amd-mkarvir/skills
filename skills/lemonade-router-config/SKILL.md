---
name: lemonade-router-config
description: >-
  Turns a natural-language description of routing intent into a valid Lemonade
  `collection.router` policy JSON, ready to register via `POST /api/v1/pull`.
  Use when the user wants to route requests between models ("route sensitive
  queries to X and everything else to Y"), generate a router/hybrid-router
  config or policy, author a collection.router JSON, split traffic between a
  small local model and a big/cloud model, add PII/jailbreak/topic classifiers
  to routing, or mentions Lemonade Router, routing rules, routing.router,
  candidates/default_model, keywords_any, semantic_similarity, or LLM-as-router.
  Fills every field the user did not specify with safe defaults.
---

# Lemonade Router Config Generator

Generate a **`collection.router` policy JSON** from a plain-English description
of how requests should be routed. The output registers against a Lemonade
server (v10.1.0+) with one `POST /api/v1/pull` call, is accepted by the strict
server-side parser on the first try, and stays editable in the desktop app's
Hybrid Router editor.

The router picks one **candidate** model per request. Two authoring modes
exist, and choosing the right one is the first decision:

| Mode | JSON shape | When |
|------|-----------|------|
| **LLM-as-router** | `routing.router` block | The user describes intent only by *meaning* ("sensitive", "hard questions", "creative writing") with no concrete signals. A small LLM reads each prompt and picks the candidate. |
| **Rules** | `routing.rules` (+ optional `routing.classifiers`) | The user names any concrete signal: keywords, regex, length, tools, images, metadata, PII/topic classifiers, thresholds, "first match", fallback logic. Deterministic, no extra LLM call for simple conditions. |

`routing.router` is **mutually exclusive** with `routing.rules` and
`routing.classifiers` — never emit both.

## Step 1 — Extract from the user's words

- **Candidates**: the models that may *answer* requests. Verbatim model names
  (e.g. `Gemma-3-4b-it-GGUF`). If the user names none, ask — never invent
  model names. `lemonade list` or `GET /api/v1/models` shows what's available.
- **Default / fallback**: which candidate gets everything that matches nothing.
  If unstated, use the model the user framed as "local", "small", or "safe";
  otherwise the first candidate mentioned.
- **Signals**: every condition mentioned (keywords, patterns, length, images,
  tools, topics, PII, safety) and which model each one routes to.
- **Classifier models**: models named for *detection* rather than answering
  (BERT-style encoders, embedding models, an LLM used as judge).

## Step 2 — Scaffold

Always exactly this envelope (the parser rejects unknown or missing keys):

```json
{
  "version": "1",
  "model_name": "user.MyHybridRouter",
  "recipe": "collection.router",
  "components": [],
  "routing": { }
}
```

- `version` is the literal string `"1"`.
- `model_name` must start with `user.`; slug from the user's description if
  they gave a name (`user.<Name>` using only `[A-Za-z0-9._-]`). If they didn't
  name it, derive one from context instead of a fixed literal — e.g.
  `user.<slug-of-default-candidate>-Router` — so two different policies don't
  collide by default. **`/pull` is idempotent per `model_name`: registering a
  second policy under the same name silently overwrites the first.** If this
  conversation already produced an unnamed router, don't reuse the same
  derived name for the next one — ask, or pick a visibly different name.

## Step 3 — Candidates and default

```json
"candidates": ["<answering models>"],
"default_model": "<one of candidates>"
```

`default_model` MUST be listed in `candidates`. Candidates should be
chat-capable LLMs — not embedding, classification, or image models.

## Step 4 — Mode A: LLM-as-router

```json
"router": {
  "type": "llm",
  "model": "<small chat LLM>",
  "prompt": "You route user requests to the best model. <one sentence per candidate: when to pick it, using the exact model name>."
}
```

- `model` defaults to the smallest candidate (it may be a candidate; it also
  works as a separate small model).
- **Write intent only — never specify a reply format.** The engine
  unconditionally appends its own contract after your prompt: it lists the
  candidate names and demands a strict JSON reply `{"model": "<name>",
  "rationale": "<one sentence>"}`, then falls back to `default_model` on any
  deviation. A prompt that also says "reply with ONLY the model name" (or
  similar) is not just redundant, it's wrong about the wire format and can
  confuse weaker judge models into replying with a bare string that then
  fails to parse — silently falling back to `default_model` on every request,
  with no visible error. Only describe *when to pick which candidate*.
- Emit **no** `rules` and **no** `classifiers` key in this mode.

## Step 5 — Mode B: classifiers

Only declare classifiers the rules actually reference. Three types:

```json
{ "id": "clf-1", "type": "classifier", "model": "<classification model>",
  "labels": ["PII", "Jailbreak"], "default_label": "PII", "on_error": "match_false" }

{ "id": "clf-2", "type": "semantic_similarity", "model": "<embedding model>",
  "reference_phrases": { "shopping": ["I want to shop for pants", "add to cart"] },
  "default_label": "shopping", "on_error": "match_false" }

{ "id": "clf-3", "type": "llm", "model": "<chat LLM>",
  "prompt": "Classify the request into only labels SAFE, RISKY",
  "labels": ["SAFE", "RISKY"], "default_label": "SAFE", "on_error": "match_false" }
```

Hard constraints (parser-enforced — see `reference.md` for the full matrix):

- `classifier` type: model should be a text-classification model (an
  `onnxruntime` encoder like `Bert-Phishing-ONNX`); `labels` must match the
  model's actual output labels. A chat LLM here is legal (LLM-as-classifier
  via chat) but prefer `type: "llm"` for that — it is explicit and prompted.
- `semantic_similarity`: `reference_phrases` is `{concept: [phrases...]}`,
  at least one concept, each with at least one phrase. Concept names ARE the
  labels — a `labels` key is **rejected** for this type. Model must be an
  embedding model. Give 3–5 varied phrases per concept when inventing them.
- `llm`: `prompt` AND non-empty `labels` are both required.
- `default_label`, when present, must be one of the labels/concepts.
- Defaults when unspecified: `id` = `clf-1`, `clf-2`, …; `on_error` =
  `"match_false"` (fail-open: a broken classifier doesn't match, so requests
  fall through — use `"match_true"` only when the user wants fail-closed
  safety); `default_label` = the first label.

## Step 6 — Mode B: rules

```json
"rules": [
  { "id": "rule-1", "match": { ... }, "route_to": "<candidate>",
    "outputs": { "reason": "<optional free-form>" } }
]
```

- **Order matters — first match wins.** Put the most specific /
  privacy-critical rules first (a "sensitive stays local" rule must precede a
  "code goes to the big model" rule, or coding prompts with PII leak).
- `route_to` MUST be a candidate. `id` uses only `[A-Za-z0-9._-]`; default
  `rule-1`, `rule-2`, ….
- No rule for the "everything else" case — that is `default_model`.

**Match conditions** — combine with `all` (AND), `any` (OR), `not`; one
condition per leaf object; nesting is allowed:

| Leaf | Example | Notes |
|------|---------|-------|
| `keywords_any` / `keywords_all` | `{ "keywords_any": ["SSN", "Email"] }` | case-insensitive substring |
| `regex` | `{ "regex": "\\b\\d{3}-?\\d{2}-?\\d{4}\\b" }` | ECMAScript flavor |
| `min_chars` / `max_chars` | `{ "min_chars": 4000 }` | input length, UTF-8 bytes, non-negative integer |
| `has_tools` / `has_images` | `{ "has_images": true }` | booleans |
| `classifier` | `{ "classifier": "clf-1", "label": "PII", "min_score": 0.5 }` | band test; `min_score`/`max_score` in [0,1]; default `min_score` 0.5; omit `label` only if the classifier has `default_label` |
| `metadata` | `{ "metadata": { "key": "consent", "equals": "denied" } }` | exactly one of `equals` / `any` / `exists`; note: not editable in the desktop UI yet — use only when the user asks for metadata routing |

## Step 7 — Components

`components` = union of: all `candidates` + every classifier `model` + the
`router.model` (Mode A). Deduplicate, keep order stable. The parser rejects
any referenced model that is not declared here.

## Step 8 — Validate, output, verify

Write the JSON to a file, then **run the bundled offline validator** before
presenting anything to the user — it mechanically re-checks every rule in
`reference.md`'s validation checklist (score ranges, non-negative integer
char counts, mode exclusivity, label/classifier references, the `model_name`
collision, the router-prompt-contract mistake above, etc.) so you don't have
to re-derive them by eye every time:

```bash
python3 scripts/validate.py router.json
```

It exits 0 with `"ready": true` when there are no errors (warnings/advisories
may still be worth mentioning to the user). If it reports errors, fix the
JSON and re-run — don't present JSON that fails this check. It is pure
offline structural/numeric validation; it cannot verify a named model
actually exists or has the right capability (chat/embedding/classification)
— that needs a live server (see below).

Then output the JSON in a fenced block, plus how to use it:

```bash
curl -X POST http://localhost:13305/api/v1/pull \
     -H "Content-Type: application/json" --data-binary @router.json
```

Point any OpenAI client's `model` field at the collection name to route. To
show the user *why* a request went where it did, add `"route_trace": true` to a
request body and read the `x_lemonade_route` object (and the
`x-lemonade-route` response header). If a Lemonade server is reachable in this
session, offer to also verify each named model with `GET
/api/v1/models/{id}` (catches a plausible-but-nonexistent name before it
wastes a `/pull` round-trip), then register the policy and run one trace
request as a smoke test.

## Defaults summary

| Field | Default when the user doesn't say |
|-------|-----------------------------------|
| `model_name` | `user.<default-candidate-slug>-Router` (never reuse a name already used earlier in this conversation) |
| `default_model` | the "small/local/safe" candidate, else first mentioned |
| mode | rules if any concrete signal is named, else LLM-as-router |
| classifier `id` / rule `id` | `clf-N` / `rule-N` |
| `on_error` | `match_false` |
| `default_label` | first label / concept |
| `min_score` | `0.5` |
| `outputs` | omit |
| router prompt | intent only — no reply-format instruction (Step 4) |

Worked NL → JSON pairs live in `examples.md`; the full schema, parser error
matrix, and model-capability table live in `reference.md`; the offline
validator is `scripts/validate.py` (run it — see Step 8).
