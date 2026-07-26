# NL → router policy examples

Each pair shows a user's natural-language request and the exact JSON the skill
should produce. Note what was defaulted: names, ids, thresholds, `on_error`,
`default_label`, and `model_name` all come from the defaults table in
`SKILL.md` when the user doesn't specify them — including `model_name`, which
is derived from the default candidate rather than a fixed literal, so the
four examples below each get a distinct name.

## 1. Pure intent, no concrete signals → LLM-as-router

> I want to route my sensitive queries to Gemma-3-4b-it-GGUF and everything
> else to Gemma-4-E4B-it-GGUF

"Sensitive" is meaning, not a mechanical signal → Mode A. The small candidate
doubles as the router model and as `default_model` (fail-safe: on any router
hiccup, requests stay on the model the user trusts with sensitive data).

```json
{
  "version": "1",
  "model_name": "user.Gemma-3-4b-it-GGUF-Router",
  "recipe": "collection.router",
  "components": ["Gemma-3-4b-it-GGUF", "Gemma-4-E4B-it-GGUF"],
  "routing": {
    "candidates": ["Gemma-3-4b-it-GGUF", "Gemma-4-E4B-it-GGUF"],
    "default_model": "Gemma-3-4b-it-GGUF",
    "router": {
      "type": "llm",
      "model": "Gemma-3-4b-it-GGUF",
      "prompt": "You route user requests to the best model. Prompts with sensitive information route to Gemma-3-4b-it-GGUF and everything else to Gemma-4-E4B-it-GGUF."
    }
  }
}
```

Note what's *not* in the prompt: no "reply with only the model name" or any
other reply-format instruction. The engine appends its own strict JSON
contract (`{"model": ..., "rationale": ...}`, listing the exact candidate
names) after whatever the author writes — an authored format instruction
would only contradict it. See `reference.md`'s
[Validation checklist](#validation-checklist) item 12.

## 2. Concrete signals, no classifiers → deterministic rules

> Send coding questions or anything longer than 4000 characters to
> Qwen3-32B-GGUF, requests with images stay on Qwen3-8B-GGUF, default to
> Qwen3-8B-GGUF

Keywords/length/images are mechanical → Mode B, no classifiers needed. The
images rule is placed first (more specific; keeps image requests local even
when they are long or mention code).

```json
{
  "version": "1",
  "model_name": "user.Qwen3-8B-GGUF-Router",
  "recipe": "collection.router",
  "components": ["Qwen3-8B-GGUF", "Qwen3-32B-GGUF"],
  "routing": {
    "candidates": ["Qwen3-8B-GGUF", "Qwen3-32B-GGUF"],
    "default_model": "Qwen3-8B-GGUF",
    "rules": [
      {
        "id": "rule-1",
        "match": { "has_images": true },
        "route_to": "Qwen3-8B-GGUF",
        "outputs": { "reason": "images-stay-local" }
      },
      {
        "id": "rule-2",
        "match": {
          "any": [
            { "keywords_any": ["def ", "function", "stack trace", "compile"] },
            { "min_chars": 4000 }
          ]
        },
        "route_to": "Qwen3-32B-GGUF",
        "outputs": { "reason": "coding-or-long" }
      }
    ]
  }
}
```

## 3. Classifiers + nested conditions → full rules mode

> Route requests containing personally identifiable information (PII), such as
> Social Security numbers or email addresses, to Gemma-3-4b-it-GGUF. Detect
> PII using both classification and pattern matching where appropriate. Also
> send requests that involve tools or images together with PII to the same
> model. Use Bert-Phishing-ONNX to identify PII and jailbreak attempts. Use
> nomic-embed-text-v2-moe-GGUF to recognize shopping- and apparel-related
> requests through semantic similarity. For requests related to clothing or
> apparel that include images, use the semantic classifier together with
> additional context, such as an LLM safety assessment or the length of the
> request, before routing. Use Gemma-3-4b-it-GGUF as the LLM classifier for
> SAFE and RISKY classifications. If none of the routing rules apply, or if a
> classifier fails, fall back to Gemma-3-4b-it-GGUF.

All three classifier types, nested `any` inside `all`, an SSN regex, and
"classifier fails → fall back" mapping to `on_error: match_false` +
`default_model`. Components include the classifier models even though they
never answer requests.

```json
{
  "version": "1",
  "model_name": "user.Gemma-3-4b-it-GGUF-PII-Router",
  "recipe": "collection.router",
  "components": [
    "Gemma-3-4b-it-GGUF",
    "Gemma-4-E4B-it-GGUF",
    "Bert-Phishing-ONNX",
    "nomic-embed-text-v2-moe-GGUF"
  ],
  "routing": {
    "candidates": ["Gemma-3-4b-it-GGUF", "Gemma-4-E4B-it-GGUF"],
    "default_model": "Gemma-3-4b-it-GGUF",
    "classifiers": [
      {
        "id": "clf-1",
        "type": "classifier",
        "model": "Bert-Phishing-ONNX",
        "labels": ["PII", "Jailbreak"],
        "default_label": "PII",
        "on_error": "match_false"
      },
      {
        "id": "clf-2",
        "type": "semantic_similarity",
        "model": "nomic-embed-text-v2-moe-GGUF",
        "reference_phrases": {
          "shopping": [
            "I want to shop for pants",
            "find me a jacket in medium",
            "add these shoes to my cart"
          ]
        },
        "default_label": "shopping",
        "on_error": "match_false"
      },
      {
        "id": "clf-3",
        "type": "llm",
        "model": "Gemma-3-4b-it-GGUF",
        "prompt": "Classify the request into only labels SAFE, RISKY",
        "labels": ["SAFE", "RISKY"],
        "default_label": "SAFE",
        "on_error": "match_false"
      }
    ],
    "rules": [
      {
        "id": "rule-1",
        "match": {
          "all": [
            { "classifier": "clf-1", "min_score": 0.5 },
            { "keywords_any": ["SSN", "Email"] },
            { "has_tools": true },
            {
              "any": [
                { "regex": "\\b\\d{3}-?\\d{2}-?\\d{4}\\b" },
                { "has_images": true }
              ]
            }
          ]
        },
        "route_to": "Gemma-3-4b-it-GGUF"
      },
      {
        "id": "rule-2",
        "match": {
          "all": [
            { "classifier": "clf-2", "min_score": 0.5 },
            { "keywords_all": ["apparel", "clothes"] },
            { "has_images": true },
            {
              "any": [
                { "classifier": "clf-3", "min_score": 0.5 },
                { "min_chars": 500 }
              ]
            }
          ]
        },
        "route_to": "Gemma-3-4b-it-GGUF"
      }
    ]
  }
}
```

Caveats worth repeating to the user with this output: the `labels` on a
`type: "classifier"` entry must match the model's real output labels
(`Bert-Phishing-ONNX` actually emits phishing-detection labels — verify with
`GET /api/v1/models/Bert-Phishing-ONNX` or a `/v1/classify` probe before
relying on `"PII"`/`"Jailbreak"`), and classifier leaves without an explicit
`"label"` use the classifier's `default_label`.

## 4. Negation and metadata opt-out

> Anything without tool calls goes to Phi-4-mini-GGUF; if the request metadata
> has consent=denied it must stay on Phi-4-mini-GGUF no matter what; the rest
> to Qwen3-32B-GGUF

```json
{
  "version": "1",
  "model_name": "user.Phi-4-mini-GGUF-Router",
  "recipe": "collection.router",
  "components": ["Phi-4-mini-GGUF", "Qwen3-32B-GGUF"],
  "routing": {
    "candidates": ["Phi-4-mini-GGUF", "Qwen3-32B-GGUF"],
    "default_model": "Qwen3-32B-GGUF",
    "rules": [
      {
        "id": "rule-1",
        "match": { "metadata": { "key": "consent", "equals": "denied" } },
        "route_to": "Phi-4-mini-GGUF",
        "outputs": { "reason": "privacy" }
      },
      {
        "id": "rule-2",
        "match": { "not": { "has_tools": true } },
        "route_to": "Phi-4-mini-GGUF"
      }
    ]
  }
}
```

Tell the user: the metadata rule is honored by the server but is not yet
editable in the desktop Hybrid Router editor (it will warn about a lossy edit
if they open this policy there).
