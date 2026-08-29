# Contributing to AMD Skills

We welcome contributions from AMD engineers and selected partners.

> **Only federated submissions are accepted.** Your skill lives in an AMD-owned
> product repo, which stays the source of truth, and this catalog vendors a
> pinned copy. We no longer accept skills authored directly under `skills/` in
> this repository. Skills already there continue to ship.

Federation keeps each skill owned and versioned by the team that owns the product it describes, on that product's release cadence, while users still get everything from one install.

Three companion guides hold the detail:

| Guide | Read it for |
| --- | --- |
| [docs/skill-requirements.md](docs/skill-requirements.md) | The rules CI enforces: required files, frontmatter limits, skill cards, the pre-PR checklist |
| [docs/best-practices.md](docs/best-practices.md) | How to write a skill agents actually reach for: fit, descriptions, body structure, scripts, AMD specifics |
| [docs/evals.md](docs/evals.md) | How structure, routing, and behavior are graded, and where `evals/evals.json` is documented |

For repository structure and the broader catalog model, see the
[README](README.md).

## Eligibility

The source repo must be under an AMD GitHub org (e.g. `AMD-AGI/...`). Non-AMD
repos are not accepted at this time.

## 1. Author the skill in your repo

Each skill is a folder holding a valid `SKILL.md`, a `skill-card.md`, and an
`evals/evals.json` dataset. Put the folders anywhere in your repo, commonly
`skills/` or `.agents/skills/`.

The catalog always tracks your **`main`** branch. That is deliberate: the
catalog cannot be pointed at a side branch, so what reaches users is what your
own review process has already merged. Land skill changes on `main` and the
catalog follows.

Everything in the folder ships, so the requirements are yours to maintain
upstream alongside the skill. See
[docs/skill-requirements.md](docs/skill-requirements.md) for what a valid skill
must contain and [docs/best-practices.md](docs/best-practices.md) for how to
make it good.

## 2. Register your source

Add your repo to [`.github/federation.json`](.github/federation.json). Every
skill is declared individually, by the full path of its folder in your repo, so
one repo can federate as many skills as it likes from wherever they live:

```json
{
  "sources": [
    {
      "repo": "AMD-Org/MyProject",
      "license": "MIT",
      "skills": [
        {
          "path": "skills/my-skill",
          "as": "myproject-my-skill"
        },
        {
          "path": "tools/agents/skills/other-skill",
          "as": "myproject-other-skill"
        }
      ]
    }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `repo` | GitHub `<owner>/<repo>`, must be AMD-owned. Always tracked at `main` |
| `license` | SPDX id, carried into each vendored copy's marker file |
| `skills[].path` | Path of the skill folder inside your repo, from the repo root |
| `skills[].as` | Optional local catalog name; use it to namespace as `<project>-<skill>` so names stay unique |

There is no ref, branch, or commit field. Federation is `main`-only by design.

## 3. Vendor and validate locally

The scripts read `.github/federation.json` from your working tree.

```bash
uv run .github/scripts/federate_skills.py --check-catalog  # schema only, no clone
uv run .github/scripts/federate_skills.py                  # vendor into skills/<name>/
./.github/scripts/publish.sh                               # regenerate the manifests
./.github/scripts/check.sh                                 # validate (same command CI runs)
```

The importer also adds your skill to the published bundle, so there is no
manifest to edit by hand.

## 4. Open a pull request

Commit `.github/federation.json`, `skills/**`, and the regenerated manifests. A
maintainer reviews and merges once CI passes. The `validate` workflow runs
`check.sh`; the `evals` workflow runs your prompts against a real agent.

Never hand-edit vendored skills under `skills/`. Changes must come from your
repo via re-import, or they will be overwritten.

## Run the same tests in your repo

The catalog grades your skills on routing (does it fire when it should, and stay
quiet when it should not?) and behavior (once it has fired, does it do the job?).
Run the identical pipeline in your own repo and you catch a break while you are
writing the change, instead of after it has been imported here.

Three steps, once:

1. **Write the dataset.** `evals/evals.json` beside each skill, per
   [skillscope's authoring guide](https://github.com/amd/skillscope/blob/main/docs/authoring-evals.md).
   Start from `skillscope template`.
2. **Call the pipeline.** In `.github/workflows/skill-evals.yml`:

   ```yaml
   name: skill-evals
   on:
     pull_request:
     workflow_dispatch:
   jobs:
     skill-evals:
       uses: amd/skillscope/.github/workflows/skill-evals.yml@bootstrap
       secrets: inherit
       with:
         # The skills a routing run installs side by side. Yours plus the ones
         # closest to it: routing is about who wins a prompt, so a skill graded
         # alone wins everything by walkover.
         routing_skills: my-skill,its-neighbour
         infra_paths: .github/workflows/skill-evals.yml
         version: main
         api_key_secret: YOUR_MODEL_API_KEY_SECRET
   ```

   That block is the whole configuration -- there is no config file. If your
   skills are not under `skills/*`, or your runners are not GitHub-hosted, that
   is where you say so; see
   [skillscope's README](https://github.com/amd/skillscope#configuring-the-repo-under-test).
3. **Register the skill.** Open a pull request adding your entry to
   `.github/federation.json`, as above.

Three honest caveats. The harness version is the `version` input above, not the
`uses:` ref -- `@bootstrap` is a launcher that never changes -- and a skill can
pin its own with `skillscope_version` in its `evals.json`. Your routing run
grades your skill against the skills you list, which is not the same room as the
catalog's published bundle, so a green run at home is evidence rather than a
guarantee. And until `evals/` becomes part of what federation carries, the
catalog keeps its own copy of each dataset: your repo's copy is what your CI
grades, the catalog's copy is what the catalog's CI grades, and the two have to
be kept in step by hand.

The catalog also checks external links in your markdown, which is worth running
in your repo for the same reason:

```yaml
  external-references:
    uses: amd/skills/.github/workflows/external-reference-check.yml@main
    permissions:
      contents: read
      issues: write
```

## Update or remove

Merge the change to `main` in your repo and the catalog picks it up on its own.
The `federate-skills` workflow runs nightly (and on demand), re-vendors any
skill whose upstream folder contents changed, and opens a pull request titled
`Bump <skill> to <short commit>`. A night with no upstream change produces no
pull request, so the only ones you see are real bumps.

To remove a skill, open a pull request that drops its entry from
`.github/federation.json`, deletes the vendored `skills/<name>/` folder, and
regenerates the manifests. Federation never deletes anything on its own: a
nightly run only reports a vendored copy that no source declares any more.
