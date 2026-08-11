#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Select which behavioral tests to run, by skill name and runner pool.

Behavioral tests live with each skill (see CONTRIBUTING.md) at:

    skills/<skill>/evals/evals.py

Skill names are lowercase-with-hyphens. Keeping each test beside the skill
makes the behavioral coverage part of the skill's own source tree.

This script maps a set of changed files (read from stdin, one path per line)
to the skills whose behavioral test should run, and is also used to enumerate
every testable skill for manual / full runs.

Output is a JSON array of skill names on stdout, suitable for a GitHub Actions
matrix:

    uv run .github/scripts/select_behavioral.py --all
    uv run .github/scripts/select_behavioral.py --names "local-ai-use,rocm-doctor"
    git diff --name-only BASE HEAD | uv run .github/scripts/select_behavioral.py --changed

A skill is "testable" only when both its test file and its skill folder exist;
that keeps the matrix honest if a test is added before its skill (or vice
versa).

Runner pools
------------

Most skills run on the default pool, but some need specific hardware. Which
pool a skill belongs to is data, not code: it lives in runner_pools.json
beside this script, which is the single place to audit who gets on scarce or
privileged hardware. `--plan` groups the selected skills by pool:

    uv run .github/scripts/select_behavioral.py --all --plan
    uv run .github/scripts/select_behavioral.py --changed --plan --github-output

A pool may require an opt-in PR label (the MI300X pool does, because the
hardware is scarce and its job can read a secret the default pool cannot).
Pass the PR's labels with `--labels` so `<pool>_run` accounts for it, or
`--dispatch` for a manual run, which is already explicit human intent. Note
the distinction the plan draws, since the gate depends on it:

    <pool>_affected  the change touches a skill in this pool
    <pool>_run       ...and the pool's opt-in requirement is satisfied

runner_pools.json deliberately does not carry runner labels or secret names.
GitHub Actions cannot create jobs dynamically, so each pool is implemented by
a hand-written job in behavioral.yml (named in the pool's `job` field) and
those live there, where a reviewer expects to read them. Adding a skill to an
existing pool is a registry-only change; adding a whole new pool needs a new
job.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

EVAL_PATH = Path("evals") / "evals.py"

REGISTRY_PATH = Path(__file__).resolve().parent / "runner_pools.json"

# Pool ids become GitHub Actions output names (`<pool>_skills`), so keep them
# to the characters that are safe there.
POOL_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Touching any of these means the shared harness (not one skill) changed, so we
# re-run every behavioral test rather than trying to guess the blast radius.
# Paths are repo-root-relative and use forward slashes to match `git diff`.
INFRA_FILES = {
    "eval/behavioral/harness.py",
    "eval/behavioral/conftest.py",
    "eval/behavioral/pytest.ini",
    "eval/behavioral/requirements.txt",
    "eval/claude_eval.py",
    ".github/scripts/select_behavioral.py",
    ".github/scripts/runner_pools.json",
    ".github/workflows/behavioral.yml",
}


def is_testable(skill: str) -> bool:
    """A skill is testable when both its test file and skill folder exist."""
    skill_dir = SKILLS_DIR / skill
    has_test = (skill_dir / EVAL_PATH).is_file()
    has_skill = (skill_dir / "SKILL.md").is_file()
    return has_test and has_skill


def all_testable_skills() -> list[str]:
    """Every skill that currently has a behavioral test and a skill folder."""
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(
        path.name
        for path in SKILLS_DIR.iterdir()
        if path.is_dir() and is_testable(path.name)
    )


def select_from_changes(changed: list[str]) -> list[str]:
    """Map changed file paths to the testable skills they affect."""
    normalized = {p.strip().replace("\\", "/") for p in changed if p.strip()}

    # Shared-harness change: run the whole suite.
    if normalized & INFRA_FILES:
        return all_testable_skills()

    selected = set()
    for path in normalized:
        # A change inside skills/<name>/...
        if path.startswith("skills/"):
            parts = path.split("/")
            if len(parts) >= 2 and is_testable(parts[1]):
                selected.add(parts[1])
    return sorted(selected)


def select_from_names(names: str) -> list[str]:
    """Filter an explicit, comma-separated skill list down to testable ones."""
    requested = [n.strip() for n in names.split(",") if n.strip()]
    return sorted({n for n in requested if is_testable(n)})


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    """Read the runner pool registry."""
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def check_registry(registry: dict) -> list[str]:
    """Return the registry's problems, empty when it is well-formed.

    Catches the mistakes that would otherwise surface as a confusing CI
    failure: a pool id that can't be a GitHub output name, a skill assigned to
    a pool that doesn't exist, or a typo'd skill name that would silently fall
    back to the default pool.
    """
    problems: list[str] = []
    pools = registry.get("pools", {})
    default = registry.get("default_pool")

    if not pools:
        problems.append("no pools are defined")
    if default not in pools:
        problems.append(f"default_pool {default!r} is not one of the defined pools")

    for pool, config in pools.items():
        if not POOL_ID_RE.match(pool):
            problems.append(
                f"pool id {pool!r} must be lowercase alphanumeric with underscores"
            )
        if not config.get("os"):
            problems.append(f"pool {pool!r} does not list any operating systems")

    for skill, pool in registry.get("skills", {}).items():
        if pool not in pools:
            problems.append(f"skill {skill!r} is assigned to unknown pool {pool!r}")
        if not (SKILLS_DIR / skill).is_dir():
            problems.append(f"skill {skill!r} is assigned to a pool but does not exist")

    return problems


def pool_for(skill: str, registry: dict) -> str:
    """The pool a skill runs on, defaulting when it is not assigned one."""
    return registry.get("skills", {}).get(skill, registry["default_pool"])


def plan(
    skills: list[str],
    registry: dict,
    labels: set[str] | None = None,
    dispatch: bool = False,
) -> dict[str, dict]:
    """Group selected skills by pool and decide whether each pool should run.

    Every pool appears in the result, including the ones with nothing to do,
    so the workflow can read a value for each of its jobs unconditionally.
    """
    labels = labels or set()
    result: dict[str, dict] = {}

    for pool, config in registry["pools"].items():
        selected = [s for s in skills if pool_for(s, registry) == pool]
        opt_in = config.get("opt_in_label")
        # A pool with no opt-in label runs whenever it is affected. One with a
        # label also needs that label on the PR, unless a human dispatched the
        # run by hand.
        satisfied = opt_in is None or dispatch or opt_in in labels
        result[pool] = {
            "skills": selected,
            "os": config["os"],
            "affected": bool(selected),
            "run": bool(selected) and satisfied,
        }

    return result


def format_github_output(pools: dict[str, dict]) -> str:
    """Render a plan as GitHub Actions `key=value` output lines."""
    lines: list[str] = []
    for pool, entry in pools.items():
        lines.append(f"{pool}_skills={json.dumps(entry['skills'])}")
        lines.append(f"{pool}_os={json.dumps(entry['os'])}")
        lines.append(f"{pool}_affected={str(entry['affected']).lower()}")
        lines.append(f"{pool}_run={str(entry['run']).lower()}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--all",
        action="store_true",
        help="Print every skill that has a behavioral test.",
    )
    mode.add_argument(
        "--changed",
        action="store_true",
        help="Read changed file paths from stdin and print the affected skills.",
    )
    mode.add_argument(
        "--names",
        metavar="A,B,C",
        help="Print the testable subset of this comma-separated skill list.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate runner_pools.json and print any problems.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Group the selected skills by runner pool instead of listing them.",
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="With --plan, emit GitHub Actions `key=value` lines.",
    )
    parser.add_argument(
        "--labels",
        default="",
        metavar="A,B,C",
        help="Comma-separated PR labels, used to satisfy a pool's opt-in label.",
    )
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="Treat this as a manual run, which satisfies every opt-in label.",
    )
    args = parser.parse_args(argv)

    registry = load_registry()

    if args.check:
        problems = check_registry(registry)
        for problem in problems:
            print(f"{REGISTRY_PATH.name}: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"{REGISTRY_PATH.name} is valid.")
        return 0

    if args.all:
        skills = all_testable_skills()
    elif args.names is not None:
        skills = select_from_names(args.names)
    else:
        skills = select_from_changes(sys.stdin.read().splitlines())

    if not args.plan:
        print(json.dumps(skills))
        return 0

    # A malformed registry would silently route skills to the default pool, so
    # refuse to plan against one.
    problems = check_registry(registry)
    if problems:
        for problem in problems:
            print(f"{REGISTRY_PATH.name}: {problem}", file=sys.stderr)
        return 1

    labels = {label.strip() for label in args.labels.split(",") if label.strip()}
    pools = plan(skills, registry, labels=labels, dispatch=args.dispatch)
    if args.github_output:
        print(format_github_output(pools))
    else:
        print(json.dumps(pools, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
