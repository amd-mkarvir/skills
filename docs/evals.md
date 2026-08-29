# Skill Evaluation

## Testing pipeline overview

A skill reaches the catalog after passing three review stages: an eligibility and compliance check, structural screening, and multi-stage agentic testing.

* **Stage 1: Eligibility and compliance** (*maintainer review, on first submission*)
  * Is the skill eligible, and does the submission follow the contribution guide and its recommendations? An AMD-owned source repo, registered in `.github/federation.json`, the skill authored upstream rather than hand-edited here, and the writing guidance in [best-practices.md](best-practices.md) applied. See [CONTRIBUTING.md](../CONTRIBUTING.md).
* **Stage 2: Structural screening** (*CI, on every pull request*)
  * Are the files well-formed? Required files, frontmatter, skill-card sections, eval schema, unique case ids, internal links, and manifests in sync. See [skill-requirements.md](skill-requirements.md).
* **Stage 3: Agentic testing** (*CI, on every pull request*)
  * **Routing testing**: Does the skill trigger when it should, and stay quiet when it shouldn't? Prompts run with the published bundle installed side by side, so a skill only wins the ones it owns. You cannot test this alone: a skill tested by itself will happily answer prompts that belong to its neighbour. Which skills go in that room is listed in [.github/workflows/evals.yml](../.github/workflows/evals.yml).
  * **Behavioral testing**: Once the skill has triggered, does it do the job? The prompt runs to completion with the skill loaded, and what the agent actually did is graded against the expectations in the dataset.

## Where the harness lives

Stages 2 and 3 are run by [amd/skillscope](https://github.com/amd/skillscope), a harness that is no part of this catalog. That separation is the point: your own repo can run the identical pipeline against your own tree, so a change is graded where it is authored rather than after it has been imported here. See [CONTRIBUTING.md](../CONTRIBUTING.md#run-the-same-tests-in-your-repo).

**How to write the dataset — the format, the coverage bar, the optional fields, `machine.yml`, `hooks.py` — is documented once, in [skillscope's authoring guide](https://github.com/amd/skillscope/blob/main/docs/authoring-evals.md).** Start from `skillscope template`.

What this catalog adds on top:

| | |
| --- | --- |
| Where a dataset lives | `skills/<your-skill>/evals/evals.json`. For now `evals/` is the one folder federation does not carry, so unlike the rest of a federated skill the dataset is authored and edited here, and a re-import never overwrites it. |
| Coverage required to ship | At least 3 evaluations with `skill_should_trigger: true`, at least 2 with `false`, and at least 1 of the `true` ones carrying `expected_behavior` or `unexpected_behavior`, so something beyond triggering is graded. |
| Which prompts run here | `evals.json` only. A skill may ship `evals/extended_evals.json` beside it, in the same format and with no coverage bar; the catalog validates it but does not run it, because a product repo's extra prompts are that repo's bill to pay. |
| What routing installs | The skills listed in `routing_skills` in [.github/workflows/evals.yml](../.github/workflows/evals.yml), which is the published bundle from `.claude-plugin/marketplace.json`. A skill that is not listed still gets validated and its behavior cases run; it just does not move anyone's routing score until it is added, which is a reviewable line rather than a silent side effect of publishing. |
| Runners | Behavior cases run on the shared Strix Halo pool, on Linux and Windows. A skill that needs an AMD Instinct GPU asks for it with `labels:` in its `evals/machine.yml` and lands on the Instinct pool instead: Linux only, gated on the `enable_mi_ci` pull-request label, and run with its own scoped credentials. What those pools are labelled, who may spend them, and which key pays is in [.github/workflows/evals.yml](../.github/workflows/evals.yml). |
| Harness version | The `version` input in [.github/workflows/evals.yml](../.github/workflows/evals.yml). A skill can pin its own with `skillscope_version` in its `evals.json`, which governs that skill's behavior run. |

## Running the tests locally

```bash
skillscope validate                                  # structure only: no agent, no tokens, instant
skillscope run --mode behavior --skill <your-skill>  # your skill, end to end
skillscope run --mode routing --routing-skills all   # every skill here, in one room
skillscope run --only <case-id> --keep-logs logs     # one case, keeping the transcript
```

`--routing-skills all` is the local shorthand; CI lists the published bundle explicitly, so a local routing score can differ from CI's by however far the two sets differ.

Without installing anything, run the build CI grades with — the `version` input in [.github/workflows/evals.yml](../.github/workflows/evals.yml), `main` today:

```bash
uvx --from git+https://github.com/amd/skillscope@main skillscope validate
```

`./.github/scripts/check.sh` does that for you as part of the pre-PR sweep. Everything but `validate` needs the `claude` CLI authenticated, plus whatever your own cases need.

In CI, the `evals` workflow runs routing when a change can move a routing decision (a listed skill's description, its dataset, or the workflow itself), and runs behavior for the skills a change touches.
