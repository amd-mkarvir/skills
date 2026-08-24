# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# See LICENSE for license information.

"""The one entry point for skill evals.

Every skill owns a single dataset at ``skills/<name>/evals/evals.json``. This
runner reads those datasets and grades them in two modes:

  * ``routing``  -- installs the whole catalog and checks that the right skill
    fires (and that nothing fires when nothing should). Cheap, no hardware,
    runs every case in the repo including every other skill's.
  * ``behavior`` -- installs one skill, runs the prompt to completion, and
    grades ``expected_behavior`` / ``unexpected_behavior`` / ``logs_contain``
    / ``files_exist``. Only runs evaluations that assert something beyond the
    routing decision.

The prompt is written once and both modes read it, which is the whole point:
the old split had a central routing prompt set and a separate per-skill pytest
file that re-asserted routing with a substring match on the transcript.

``evals.json`` is the catalog contract, not a skill's whole suite. Each skill's
behavior cases get a wall-clock budget (``--budget-minutes``), and a case that
does not fit fails with a pointer to ``evals/extended_evals.json`` -- the file
this runner never reads, holding the deep tests the repo that owns the skill
runs on its own CI. Time is the only cap: what a case costs is how long it
takes, which no field in a dataset knows.

Usage::

    # everything a skill owner needs before opening a PR
    python eval/run_evals.py --skill serving-llms-on-epyc

    # structural checks only: no agent, no tokens, instant
    python eval/run_evals.py --validate

    # what CI runs
    python eval/run_evals.py --mode routing --min-accuracy 0.9
    python eval/run_evals.py --mode behavior --skill local-ai-use

    # one case, keeping the raw transcript
    python eval/run_evals.py --only qwen-on-mi300x --keep-logs eval-logs

Reports go to stdout as markdown, to ``$GITHUB_STEP_SUMMARY`` under Actions,
and to a JSON artifact under ``eval/runs/``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))

import datasets  # noqa: E402
import routing  # noqa: E402
from agent import (  # noqa: E402
    CaseTimeout,
    Check,
    check_api_reachable,
    claude,
    enforce_model_policy,
)
from datasets import Case  # noqa: E402

# Said whenever a case runs out of budget, and the only place this runner names
# the other file. The budget's whole purpose is that the person who broke it
# learns what to do about it, and "move a case" is the answer -- a suite that
# no longer fits has outgrown the catalog contract, not the budget.
BUDGET_HINT = (
    "The catalog suite has a wall-clock budget per skill. Move a case to "
    f"{datasets.EXTENDED_DATASET_RELPATH.as_posix()}, which the repo that owns "
    "this skill runs on its own CI."
)


@dataclass
class BehaviorOutcome:
    """One behavior-mode case: what was asserted and what happened."""

    id: str
    skill: str
    prompt: str
    passed: bool
    elapsed_s: float
    checks: list[dict] = field(default_factory=list)
    error: str | None = None
    # Never started, as opposed to started and failed. Both are failures, but
    # reporting the first as the second sends whoever reads it looking for a
    # bug in a run that does not exist.
    not_run: bool = False


# --------------------------------------------------------------------------
# Hooks: the escape hatch for setup a JSON file cannot express.
# --------------------------------------------------------------------------


def _load_hooks(skill: str) -> ModuleType | None:
    """Import ``skills/<skill>/evals/hooks.py`` if it exists.

    A hook module holds environment plumbing only -- cloning a repo, tearing
    down a container, running an external scoring script. Prompts and
    expectations stay in the dataset, so the thing being asserted is always
    readable without opening Python.

    Recognized (all optional)::

        setup_session(cache_dir)      -> dict of template vars, once per skill
        setup(workspace, case, ctx)   -> dict of extra template vars, per case
        teardown(workspace, case, ctx)
        check(run, case, ctx)         -> raise AssertionError to fail the case
    """
    path = datasets.hooks_path(skill)
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"evalhooks_{skill.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"error: could not import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expand(text: str, ctx: dict) -> str:
    """Substitute ``{name}`` placeholders from `ctx`.

    Plain replacement rather than ``str.format`` because prompts routinely
    contain literal braces (JSON snippets, regex quantifiers) that would
    otherwise raise or be swallowed.
    """
    for key, value in ctx.items():
        text = text.replace("{" + key + "}", str(value))
    return text


# --------------------------------------------------------------------------
# Behavior mode
# --------------------------------------------------------------------------


def run_behavior_case(
    case: Case,
    ctx: dict,
    hooks: ModuleType | None,
    model: str,
    effort: str,
    timeout: float | None = None,
) -> BehaviorOutcome:
    """Stage one skill, run the prompt to completion, grade what happened.

    ``timeout`` is what is left of the skill's budget. Passing it down means a
    single hung run costs that budget rather than the whole CI job, and fails
    with an explanation instead of as an unattributed job timeout.
    """
    assert case.skill is not None
    seed = (datasets.SKILLS_DIR / case.skill / case.workspace) if case.workspace else None
    started = time.perf_counter()
    case_ctx = dict(ctx)
    checks: list[Check] = []
    error: str | None = None

    try:
        with claude(model, skill=case.skill, effort=effort, seed=seed) as session:
            workspace = session.workspace
            assert workspace is not None
            if hooks is not None and hasattr(hooks, "setup"):
                case_ctx.update(hooks.setup(workspace, case, case_ctx) or {})
            try:
                run = session.prompt(_expand(case.prompt, case_ctx), timeout=timeout)
                checks = run.evaluate(
                    logs_contain=[_expand(t, case_ctx) for t in case.logs_contain],
                    files_exist=[_expand(p, case_ctx) for p in case.files_exist],
                    expected_behavior=case.expected_behavior,
                    unexpected_behavior=case.unexpected_behavior,
                )
                if hooks is not None and hasattr(hooks, "check"):
                    try:
                        hooks.check(run, case, case_ctx)
                        checks.append(Check("hook", "evals/hooks.py check()", True))
                    except AssertionError as exc:
                        checks.append(Check("hook", "evals/hooks.py check()", False, str(exc)[:400]))
            finally:
                if hooks is not None and hasattr(hooks, "teardown"):
                    hooks.teardown(workspace, case, case_ctx)
    except CaseTimeout as exc:
        # Not an infra failure, so no traceback: the run was working, it was
        # just taking longer than this suite is allowed to take.
        error = f"{exc}. {BUDGET_HINT}"
    except Exception as exc:  # noqa: BLE001 -- an infra failure is a result too
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()

    elapsed = round(time.perf_counter() - started, 2)
    passed = error is None and all(c.passed for c in checks) and bool(checks)
    if error is None and not checks:
        error = "case has no behavioral assertions to grade"
    print(
        f"  [{'PASS' if passed else 'FAIL'}] {case.id}: "
        f"{sum(1 for c in checks if c.passed)}/{len(checks)} checks in {elapsed}s"
        + (f" -- {error}" if error else ""),
        flush=True,
    )
    return BehaviorOutcome(
        id=case.id,
        skill=case.skill,
        prompt=case.prompt,
        passed=passed,
        elapsed_s=elapsed,
        checks=[asdict(c) for c in checks],
        error=error,
    )


def _not_run(case: Case, spent_s: float, budget_s: float) -> BehaviorOutcome:
    """A case the budget never reached, recorded as a failure.

    Not a skip: an expectation nobody checked is not an expectation that held,
    and reporting it as passed-by-omission is how a suite quietly stops testing
    the thing it was written for.
    """
    return BehaviorOutcome(
        id=case.id,
        skill=case.skill or "",
        prompt=case.prompt,
        passed=False,
        elapsed_s=0.0,
        error=(
            f"this skill's suite had spent {spent_s / 60:.1f} of its "
            f"{budget_s / 60:.0f}-minute budget before reaching this case. {BUDGET_HINT}"
        ),
        not_run=True,
    )


def run_behavior(
    skills: list[str],
    cases: list[Case],
    model: str,
    effort: str,
    budget_s: float = 0.0,
) -> list[BehaviorOutcome]:
    """Run every behavior case, grouped by skill so session setup happens once.

    ``budget_s`` caps each skill separately rather than the run as a whole,
    because behavior is one CI job per skill: a skill added to the catalog
    brings its own runner, so a per-skill number keeps meaning the same thing
    however far federation goes, where a shared one would shrink everybody's
    allowance every time a skill arrived.

    Session setup counts against it -- hooks that clone a repo or start a
    container spend real job time -- and each case is handed what is left, so
    one long run cannot silently consume the cases behind it.
    """
    outcomes: list[BehaviorOutcome] = []
    for skill in skills:
        skill_cases = [c for c in cases if c.skill == skill and c.has_behavior]
        if not skill_cases:
            continue

        hooks = _load_hooks(skill)
        ctx: dict = {}
        cache_dir: Path | None = None
        started = time.perf_counter()
        if hooks is not None and hasattr(hooks, "setup_session"):
            cache_dir = Path(tempfile.mkdtemp(prefix=f"evalcache-{skill}-"))
            print(f"[behavior] {skill}: running evals/hooks.py setup_session()", flush=True)
            ctx.update(hooks.setup_session(cache_dir) or {})
        try:
            budget_note = f", budget {budget_s / 60:.0f}min" if budget_s > 0 else ""
            print(f"[behavior] {skill}: {len(skill_cases)} case(s){budget_note}", flush=True)
            for index, case in enumerate(skill_cases):
                spent = time.perf_counter() - started
                remaining = (budget_s - spent) if budget_s > 0 else None
                if remaining is not None and remaining <= 0:
                    unreached = skill_cases[index:]
                    print(
                        f"  [FAIL] {skill}: budget spent after {index} of "
                        f"{len(skill_cases)} case(s); {len(unreached)} not run. "
                        f"{BUDGET_HINT}",
                        flush=True,
                    )
                    outcomes.extend(_not_run(c, spent, budget_s) for c in unreached)
                    break
                outcomes.append(
                    run_behavior_case(case, ctx, hooks, model, effort, remaining)
                )
        finally:
            if cache_dir is not None:
                shutil.rmtree(cache_dir, ignore_errors=True)
    return outcomes


def summarize_behavior(outcomes: list[BehaviorOutcome], meta: dict) -> dict:
    per_skill: dict[str, dict] = {}
    for skill in sorted({o.skill for o in outcomes}):
        subset = [o for o in outcomes if o.skill == skill]
        per_skill[skill] = {
            "cases": len(subset),
            "passed": sum(1 for o in subset if o.passed),
            "checks": sum(len(o.checks) for o in subset),
            "checks_passed": sum(1 for o in subset for c in o.checks if c["passed"]),
            # What the suite cost, which is the number to look at when deciding
            # whether a case belongs in the catalog file at all.
            "seconds": round(sum(o.elapsed_s for o in subset), 1),
        }
    return {
        "meta": meta,
        "totals": {
            "cases": len(outcomes),
            "passed": sum(1 for o in outcomes if o.passed),
            "checks": sum(len(o.checks) for o in outcomes),
            "checks_passed": sum(1 for o in outcomes for c in o.checks if c["passed"]),
            "errors": sum(1 for o in outcomes if o.error),
        },
        "per_skill": per_skill,
        "cases": [asdict(o) for o in outcomes],
    }


def render_behavior_markdown(summary: dict) -> str:
    totals = summary["totals"]
    meta = summary["meta"]
    lines = [
        "## Skill behavior",
        "",
        f"**{totals['passed']}/{totals['cases']} cases passed** "
        f"({totals['checks_passed']}/{totals['checks']} individual expectations) "
        f"on `{meta['model']}` (effort `{meta['effort']}`).",
        "",
    ]
    budget_s = meta.get("budget_s") or 0
    if budget_s:
        lines += [
            f"Each skill's suite has {budget_s / 60:.0f} minutes. Time is the "
            f"only cap on a catalog dataset, so a suite that no longer fits has "
            f"a case to move to "
            f"`{datasets.EXTENDED_DATASET_RELPATH.as_posix()}`.",
            "",
        ]
    lines += [
        "| Skill | Cases | Passed | Expectations | Met | Seconds |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for skill, stats in summary["per_skill"].items():
        over = " (over budget)" if budget_s and stats["seconds"] > budget_s else ""
        lines.append(
            f"| `{skill}` | {stats['cases']} | {stats['passed']} | "
            f"{stats['checks']} | {stats['checks_passed']} | "
            f"{stats['seconds']}{over} |"
        )

    failures = [c for c in summary["cases"] if not c["passed"]]
    lines += ["", "### Unmet expectations", ""]
    if not failures:
        lines.append("None. Every behavior case met every expectation.")
    else:
        lines += ["| Case | Kind | Expectation | Detail |", "| --- | --- | --- | --- |"]
        for case in failures:
            if case["error"]:
                detail = case["error"].replace("|", "\\|").replace("\n", " ")
                # Roomier than a check detail on purpose: this column is where
                # a budget breach says what to do about it, and truncating to
                # the diagnosis leaves the reader with only bad news.
                unreached = case.get("not_run")
                lines.append(
                    f"| `{case['id']}` | {'not run' if unreached else 'error'} | "
                    f"{'(never started)' if unreached else '(run failed)'} | "
                    f"{detail[:300]} |"
                )
            for check in case["checks"]:
                if check["passed"]:
                    continue
                expectation = check["expectation"].replace("|", "\\|")
                detail = (check["detail"] or "").replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| `{case['id']}` | {check['kind']} | {expectation[:120]} | {detail[:160]} |"
                )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _selected_skills(args: argparse.Namespace) -> list[str]:
    available = datasets.skills_with_datasets()
    if not args.skill:
        return available
    wanted = [s.strip() for s in args.skill.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in available]
    if unknown:
        raise SystemExit(
            f"error: no eval dataset for {', '.join(unknown)}. Expected "
            f"skills/<name>/{datasets.DATASET_RELPATH.as_posix()}."
        )
    return wanted


def _write_report(summary: dict, report: str, args: argparse.Namespace, label: str) -> Path:
    # `--output` names one file, so it only applies when one mode is running;
    # under `--mode both` the second report would otherwise clobber the first.
    if args.output and args.mode != "both":
        output = Path(args.output)
    elif args.output:
        named = Path(args.output)
        output = named.with_name(f"{named.stem}-{label}{named.suffix}")
    else:
        # eval/runs/ is gitignored; claude_eval.py writes there too.
        output = EVAL_DIR / "runs" / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print()
    print(report)
    print(f"[evals] JSON report: {output}")

    summary_path = args.summary or os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(report)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--mode",
        default="both",
        choices=["routing", "behavior", "both"],
        help="Which grader to run. Default: both.",
    )
    parser.add_argument(
        "--skill",
        default="",
        help="Comma-separated skill names. Default: every skill with a dataset.",
    )
    parser.add_argument("--only", default="", help="Comma-separated case ids to run.")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Check every dataset structurally and exit. No agent, no tokens.",
    )
    parser.add_argument("--list-skills", action="store_true", help="Print skills that have a dataset, as JSON.")
    parser.add_argument("--model", default="opus", help="Model alias. CI pins this to opus. Default: opus.")
    parser.add_argument(
        "--effort", default="high", choices=["low", "medium", "high", "max"],
        help="Reasoning effort. Default: high.",
    )
    parser.add_argument(
        "--jobs", type=int, default=datasets.ROUTING_JOBS,
        help=(
            "Routing cases to run concurrently, each in its own workspace. "
            "The lever for keeping routing inside its budget as the catalog "
            f"grows. Default: {datasets.ROUTING_JOBS}."
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=datasets.ROUTING_TIMEOUT_S,
        help=(
            "Seconds before a routing case is abandoned. Generous because it "
            "only bites when the agent neither activates a skill nor answers. "
            f"Default: {datasets.ROUTING_TIMEOUT_S:.0f}."
        ),
    )
    parser.add_argument(
        "--budget-minutes", type=float, default=datasets.CATALOG_BUDGET_S / 60,
        help=(
            "Wall-clock budget for each skill's behavior suite. A case that "
            "does not fit fails and says so, rather than being discovered as an "
            f"unattributed CI job timeout. 0 disables. Default: "
            f"{datasets.CATALOG_BUDGET_S / 60:.0f}."
        ),
    )
    parser.add_argument(
        "--max-tool-calls", type=int, default=4,
        help=(
            "Stop a routing case after this many non-skill tool calls. Agents "
            "often look around before invoking a skill, so this is not 1; it is "
            "small enough to cut the run off long before real work starts. "
            "Default: 4."
        ),
    )
    parser.add_argument(
        "--max-inspection-calls", type=int, default=8,
        help=(
            "Separate allowance for tool calls that only read the installed "
            "skills tree. Surveying the catalog is part of the routing "
            "decision, so it does not spend --max-tool-calls, but it is capped "
            "so a run cannot idle to timeout. Default: 8."
        ),
    )
    parser.add_argument(
        "--max-budget-usd", type=float, default=0.75,
        help="Per-case routing spend cap enforced by the CLI. 0 disables. Default: 0.75.",
    )
    parser.add_argument("--output", default="", help="Write the JSON report here. Default: eval/runs/<mode>-<timestamp>.json.")
    parser.add_argument("--summary", default="", help="Write the markdown report here (defaults to $GITHUB_STEP_SUMMARY when set).")
    parser.add_argument("--keep-logs", default="", help="Directory for raw per-case stream-json transcripts.")
    parser.add_argument(
        "--min-accuracy", type=float, default=0.0,
        help="Exit non-zero when routing accuracy falls below this (0-1). Default: 0 (report only).",
    )
    parser.add_argument("--skip-preflight", action="store_true", help="Skip the API reachability check.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_skills:
        print(json.dumps(datasets.skills_with_datasets(), separators=(",", ":")))
        return 0

    # Structural problems are found before any tokens are spent, so a malformed
    # dataset costs seconds rather than failing halfway through a paid run.
    errors = datasets.validate_all()
    if errors:
        print("Dataset validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    if args.validate:
        skills = datasets.skills_with_datasets()
        cases = datasets.load_all_cases()
        print(
            f"[evals] OK: {len(cases)} case(s) across {len(skills)} skill(s) "
            f"plus {len(datasets.load_shared_negatives())} shared negative(s)."
        )
        return 0

    args.model = enforce_model_policy(args.model) or args.model
    skills = _selected_skills(args)
    catalog = datasets.routing_catalog()

    if not args.skip_preflight:
        ok, detail = check_api_reachable(args.model)
        if not ok:
            raise SystemExit(f"error: claude API not reachable -- {detail}")

    failed = False
    started = time.time()

    if args.mode in ("routing", "both"):
        if not catalog:
            raise SystemExit(
                "error: the marketplace bundle lists no skills, so there is "
                "nothing to install for a routing run. Check "
                ".claude-plugin/marketplace.json."
            )

        # Pool every published skill's cases: skill Y's positives are skill X's
        # negatives, which is where most of the false-trigger coverage comes
        # from. --skill narrows what is *reported on*, not what is installed.
        cases = datasets.load_all_cases()
        held_out = sorted(
            {c.skill for c in cases if c.skill and c.skill not in set(catalog)}
        )
        cases = datasets.routing_cases(cases, catalog)
        if args.only:
            cases = datasets.filter_cases(cases, args.only)
        elif args.skill:
            cases = datasets.filter_cases(cases, args.skill)
        if not cases:
            raise SystemExit(
                "error: no routing cases left to run. Prompts that expect an "
                "unpublished skill are held out, because that skill is not "
                "installed and so could never win them."
            )

        config = routing.RoutingConfig(
            model=args.model,
            effort=args.effort,
            timeout=args.timeout,
            max_tool_calls=args.max_tool_calls,
            max_inspection_calls=args.max_inspection_calls,
            max_budget_usd=args.max_budget_usd,
            keep_logs=args.keep_logs,
            available_flags=routing.supported_flags(
                ["--no-session-persistence", "--max-budget-usd"]
            ),
            isolate_config=routing.can_isolate_config(),
        )
        if not config.isolate_config:
            print(
                "[routing] warning: ANTHROPIC_API_KEY is not set, so the runner's "
                "own config dir is used and any user-level skill in it joins the "
                "catalog for every case. The report flags what was registered."
            )

        print(f"[routing] skills installed per case: {', '.join(catalog)}")
        if held_out:
            print(
                f"[routing] unpublished, so their prompts are not graded here: "
                f"{', '.join(held_out)}"
            )
        print(f"[routing] {len(cases)} cases, model={args.model}, jobs={args.jobs}")
        if args.jobs > 1 and len(cases) > 1:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                outcomes = list(pool.map(lambda c: routing.run_case(c, catalog, config), cases))
        else:
            outcomes = [routing.run_case(case, catalog, config) for case in cases]

        summary = routing.summarize(
            outcomes,
            catalog,
            {
                "model": args.model,
                "effort": args.effort,
                "skills": catalog,
                "held_out_skills": held_out,
                "budget_s": max(0.0, args.budget_minutes * 60),
                "jobs": args.jobs,
                "wall_time_s": round(time.time() - started, 1),
                "max_tool_calls": args.max_tool_calls,
                "max_inspection_calls": args.max_inspection_calls,
                "isolated_config_dir": config.isolate_config,
                "max_budget_usd": args.max_budget_usd,
                "optional_cli_flags_used": sorted(config.available_flags),
                "github_run_id": os.environ.get("GITHUB_RUN_ID"),
            },
        )
        _write_report(summary, routing.render_markdown(summary), args, "routing")

        totals = summary["totals"]
        if totals["graded"] == 0:
            print("[routing] every case errored; treating the run as a failure.", file=sys.stderr)
            failed = True
        elif totals["activations"] == 0 and totals["activations_expected"]:
            print(
                "[routing] no skill activated in any case -- the skills were not "
                "installed, or activation detection is broken. Failing rather than "
                "reporting a 0% routing rate as if it were real.",
                file=sys.stderr,
            )
            failed = True
        elif args.min_accuracy > 0 and (totals["accuracy"] or 0) < args.min_accuracy:
            print(
                f"[routing] accuracy {totals['accuracy']} is below the "
                f"--min-accuracy bar of {args.min_accuracy}.",
                file=sys.stderr,
            )
            failed = True

    if args.mode in ("behavior", "both"):
        cases = []
        for skill in skills:
            cases.extend(datasets.load_dataset(skill))
        if args.only:
            cases = datasets.filter_cases(cases, args.only)
        gradable = [c for c in cases if c.has_behavior]

        if not gradable:
            print(
                "[behavior] no evaluation in the selected skill(s) asserts "
                "anything beyond routing, so there is nothing to grade. Add "
                "`expected_behavior` / `unexpected_behavior` / `logs_contain` / "
                "`files_exist` to a triggering evaluation."
            )
        else:
            budget_s = max(0.0, args.budget_minutes * 60)
            outcomes = run_behavior(
                skills, gradable, args.model, args.effort, budget_s
            )
            summary = summarize_behavior(
                outcomes,
                {
                    "model": args.model,
                    "effort": args.effort,
                    "skills": skills,
                    "budget_s": budget_s,
                    "wall_time_s": round(time.time() - started, 1),
                    "github_run_id": os.environ.get("GITHUB_RUN_ID"),
                },
            )
            _write_report(summary, render_behavior_markdown(summary), args, "behavior")
            if summary["totals"]["passed"] != summary["totals"]["cases"]:
                failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
