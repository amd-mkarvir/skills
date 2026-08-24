# Skill Evaluation

## Testing Pipeline Overview

A skill reaches the catalog after passing three review stages: an eligibility and compliance check, structural screening, and multi-stage agentic testing.

* **Stage 1: Eligibility and compliance** (*maintainer review, on first submission*)
  * Is the skill eligible, and does the submission follow the contribution guide and its recommendations? An AMD-owned source repo, registered in `sources.yml`, the skill authored upstream rather than hand-edited here, and the writing guidance in [best-practices.md](best-practices.md) applied. See [CONTRIBUTING.md](../CONTRIBUTING.md).
* **Stage 2: Structural screening** (*CI, on every pull request*)
  * Are the files well-formed? Required files, frontmatter, skill-card sections, eval schema, unique case ids, internal links, and manifests in sync. See [skill-requirements.md](skill-requirements.md).
* **Stage 3: Agentic testing** (*CI, on every pull request*)
  * **Routing Testing**: Does the skill trigger when it should, and stay quiet when it shouldn't? Prompts run with the published bundle installed side by side, so a skill only wins the ones it owns. You cannot test this alone: a skill tested by itself will happily answer prompts that belong to its neighbour.
  * **Behavioral Testing**: Once the skill has triggered, does it do the job? The prompt runs to completion with the skill loaded, and what the agent actually did is graded against the expectations in the dataset.

The rest of this document is the dataset that structural screening and agentic testing read. You write it as JSON inside your skill folder, where it ships with the skill from your repo and is vendored into the catalog along with everything else. Copy [`eval/TEMPLATE.json`](../eval/TEMPLATE.json) to start.

## Which file a prompt goes in

Your skill can have a larger suite than this catalog runs, split across two files that sit side by side and hold exactly the same shape:

| File | Run by | Holds |
| --- | --- | --- |
| `evals/evals.json` | this catalog, on every pull request | the catalog contract: every routing prompt, plus the behavior cases that fit the budget |
| `evals/extended_evals.json` | your repo, on your CI, with a copy of this harness | the expensive long tail: deep behavior cases, heavy fixtures, product-specific scoring |

The filename is the whole declaration. There is no `scope` field and no manifest to keep in sync, and because both files take the same fields, moving a case between them is a cut and paste.

Two rules decide which file a prompt belongs in.

**Every routing prompt goes in `evals.json`.** That means all of them: the ones that should fire your skill and, especially, the `skill_should_trigger: false` near misses. A near miss asserts that *nothing in the published bundle* grabs the prompt, so it can only be graded where the whole bundle is installed — which is here. Run anywhere else it proves nothing, and left in the extended file it is a prompt nobody grades. Routing prompts are also the cheapest thing in the suite, since each one is killed the instant its routing decision is visible, and they pay for themselves twice over: your positive prompt is automatically a negative for every other skill in the catalog.

**Behavior cases go wherever they fit.** Each skill's catalog suite gets **15 minutes of wall clock**, enforced by the runner. Keep the handful that pin down the behavior your skill exists for, and put the rest in `extended_evals.json`. When a case runs out of budget it fails with a message naming that file, so you find out from a test result rather than from a CI job that timed out for no stated reason.

Nothing counts your cases, because a case's cost is how long it takes and no field in the dataset knows that. If the suite fits, it fits.

## What skill owners write

One file, `skills/<your-skill>/evals/evals.json`, holding an `evaluations` array:

```json
{
  "evaluations": [
    {
      "id": "images-cost",
      "skill_should_trigger": true,
      "prompt": "I'm burning too much money on image generation APIs. Generate images on my own machine instead."
    },
    {
      "id": "generate-cat-image",
      "skill_should_trigger": true,
      "prompt": "Learn how to generate images locally, then save an image of a cat to out.png.",
      "expected_behavior": ["Install Lemonade Server if it is not already installed"],
      "unexpected_behavior": ["Reach for a cloud image path instead of local Lemonade"],
      "files_exist": ["AGENTS.md", "out.png"]
    },
    {
      "id": "finetune-on-laptop",
      "skill_should_trigger": false,
      "note": "Local, on-device, and model-shaped, but training is nobody's job here.",
      "prompt": "Fine-tune a small language model on my own dataset using my laptop GPU."
    }
  ]
}
```

Every evaluation is a prompt plus `skill_should_trigger`: `true` if your skill should fire for it, `false` if it shoudn't.

**When a prompt is all you provide, the evaluation only checks whether the skill was triggered.** Understanding whether your skill is being correctly triggered (both in isolation as well as when other skills are present) is essential and cheap to check for.

**When you add expectations, the prompt also runs end to end** and what the agent did is grated (pass fail) based on the generated logs and workspace.

### Requirements

These are the bar for `evals.json`, the catalog contract. `extended_evals.json` sits on top of it and has no ceiling.

- At least **3** evaluations with `skill_should_trigger: true`
- At least **2** evaluations with `skill_should_trigger: false`
- At least **1** of the `true` evaluations carries `expected_behavior` or
  `unexpected_behavior`, so something beyond triggering is graded

### Evaluation criteria and optional fields

Four optional fields, all arrays, all valid only on a `true` evaluation:

| Field | Graded by | Use it for |
| --- | --- | --- |
| `expected_behavior` | an LLM judge | a step the agent must take, in plain language |
| `unexpected_behavior` | an LLM judge | the mistake this skill exists to prevent |
| `logs_contain` | substring match | a literal that must appear: a script name, a flag, a pinned image tag |
| `files_exist` | the filesystem | an artifact the run must produce |

The bottom two are instant and free where a judged expectation costs a second agent call, so reach for them when the thing you want is literal. Never assert your own skill's name in `logs_contain`; triggering is already graded properly.

A `files_exist` entry matches whole path segments anywhere in the workspace, so `plan.md` is satisfied by `examples/plan.md` and `out/report.md` by `run-1/out/report.md`. Name the artifact rather than the directory you hope the agent picks: where a file lands is usually the agent's call, and a plan written beside the fixture it describes should not fail the run. If the location matters, ask for it in the prompt and grade it with `expected_behavior`.

The full field reference is
[`eval/schema/evals.schema.json`](../eval/schema/evals.schema.json), enforced by
`python eval/run_evals.py --validate`.

### Enabling more complex tests

Two optional files sit beside the dataset when JSON is not enough.

**`evals/machine.yml`** — needed only if the default Linux and Windows runners
are wrong for your skill. Both keys are optional:

```yaml
runner_type: instinct    # `default` (assumed) or `instinct`
os: [Linux]              # defaults to every platform that runner type has
```

Name the kind of machine and the rest follows: `runner_type: instinct` implies the runner labels, the Linux-only constraint, the `enable_mi_ci` pull-request label that rations that scarce pool, and the scoped credentials. Most skills that need this file need only `os: [Linux]`, to drop a Windows leg that would just exercise the failure path of Linux-only tooling.

**`evals/hooks.py`** — setup a dataset cannot express: cloning a repo, tearing down a container, running an external scoring script. Every function is optional:

```python
def setup_session(cache_dir): ...     # once per run; returns {name: value} for {placeholders} in prompts
def setup(workspace, case, ctx): ...  # before each case; may return more placeholders
def teardown(workspace, case, ctx): ...
def check(run, case, ctx): ...        # after each case; raise AssertionError to fail it
```

Keep prompts and expectations in the dataset even when you use hooks, so what is being asserted stays readable without opening Python. See [`skills/serving-llms-on-instinct/evals/hooks.py`](../skills/serving-llms-on-instinct/evals/hooks.py) for a simple example and [`skills/tracelens-analysis-orchestrator/evals/hooks.py`](../skills/tracelens-analysis-orchestrator/evals/hooks.py) for an involved one.

### Running tests locally

```bash
python eval/run_evals.py --validate              # structure only: no agent, no tokens, instant
python eval/run_evals.py --skill <your-skill>    # routing and behavior for your skill
python eval/run_evals.py --mode routing          # the published bundle
python eval/run_evals.py --only <case-id> --keep-logs logs   # one case, keeping the transcript
python eval/run_evals.py --skill <your-skill> --budget-minutes 0   # no time limit, while iterating
```

Everything but `--validate` needs the `claude` CLI authenticated, plus whatever your own cases need. No `pip install`: the runner is standard library only.

In CI, the `evals` workflow runs routing when a change can move a routing decision (a published description, any dataset, or the bundle itself), and runs behavior for the skills a change touches. A change confined to `extended_evals.json` schedules nothing, since this repo never reads that file.

### The two budgets

Behavior and routing are both held to 15 minutes, but only one of them can be enforced per skill.

Behavior is one CI job per skill, so its budget is per skill and stays that way as the catalog grows — a new skill brings its own runner rather than minutes to yours. Overruns are a hard failure, because the fix is yours to make: move a case to `extended_evals.json`.

Routing is a single job holding every skill's prompts, so no one pull request owns its size. Its budget is measured after the fact and reported, never failed. When it stops fitting, the fix is concurrency (`--jobs`) rather than fewer prompts, since each prompt is another skill's negative. The `discover` job prints the forecast — how many prompts, at what concurrency, and the concurrency that would guarantee the budget — so the catalog's growth is visible before it becomes a timeout. Note that publishing fewer skills does not bound this: an unpublished skill's near misses are still graded, because they assert that nothing fires.
