# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# See LICENSE for license information.

"""Check whether prompts activate the expected marketplace skill.

Each case installs the full published skill catalog in a temporary workspace,
runs Claude, and stops as soon as a Skill tool call makes the routing decision
observable. Results are written as JSON and as a short Markdown summary.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
EVAL_DIR = REPO_ROOT / "eval"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PROMPTS_FILE = HERE / "prompts.json"

sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(EVAL_DIR / "behavioral"))
from claude_eval import SKILLS_DIR  # noqa: E402
from harness import AUTOMATED_MODEL, check_api_reachable  # noqa: E402

SKILL_TOOLS = {"skill", "slashcommand"}
BOOKKEEPING_TOOLS = {"todowrite", "todoread", "exitplanmode"}
VERDICTS = (
    "correct_trigger",
    "true_negative",
    "missed_trigger",
    "wrong_skill",
    "false_trigger",
    "error",
)
PASSING_VERDICTS = {"correct_trigger", "true_negative"}


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    expect: str | None
    category: str


@dataclass
class Outcome:
    id: str
    category: str
    prompt: str
    expect: str | None
    observed: str | None
    verdict: str
    passed: bool
    stop_reason: str
    elapsed_s: float
    tool_calls: int
    error: str | None = None


def enforce_model_policy(model: str) -> str:
    automated = any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
        for name in ("CI", "GITHUB_ACTIONS")
    )
    if not automated or "opus" in model.lower():
        return model
    print(f"[routing] automated run: coercing model '{model}' -> '{AUTOMATED_MODEL}'.")
    return AUTOMATED_MODEL


def marketplace_skills() -> list[str]:
    manifest = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    skills = list(
        dict.fromkeys(
            str(entry).rstrip("/").split("/")[-1]
            for plugin in manifest.get("plugins", [])
            for entry in plugin.get("skills", [])
        )
    )
    missing = [name for name in skills if not (SKILLS_DIR / name / "SKILL.md").is_file()]
    if missing:
        raise SystemExit(
            f"error: marketplace lists skills with no SKILL.md: {', '.join(missing)}"
        )
    if not skills:
        raise SystemExit(f"error: no skills listed in {MARKETPLACE}")
    return skills


def load_cases(path: Path) -> list[Case]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        Case(
            id=str(item["id"]),
            prompt=str(item["prompt"]),
            expect=item.get("expect") or None,
            category=str(item.get("category") or "positive"),
        )
        for item in payload["cases"]
    ]
    duplicates = [case_id for case_id, count in Counter(c.id for c in cases).items() if count > 1]
    if duplicates:
        raise SystemExit(f"error: duplicate case ids in {path}: {', '.join(duplicates)}")
    return cases


def filter_cases(cases: list[Case], only: str) -> list[Case]:
    if not only:
        return cases
    wanted = {token.strip() for token in only.split(",") if token.strip()}
    selected = [case for case in cases if case.id in wanted or case.expect in wanted]
    if not selected:
        raise SystemExit(f"error: --only '{only}' matched no cases")
    return selected


def stage_workspace(skills: list[str]) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="routing-"))
    destination = workspace / ".claude" / "skills"
    destination.mkdir(parents=True)
    for skill in skills:
        shutil.copytree(SKILLS_DIR / skill, destination / skill)
    return workspace


def iter_tool_uses(node: object) -> Iterator[tuple[str, object]]:
    if isinstance(node, dict):
        if node.get("type") == "tool_use":
            yield str(node.get("name", "")), node.get("input", {})
        for value in node.values():
            yield from iter_tool_uses(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_tool_uses(value)


def detect_activation(event: dict, skills: list[str]) -> str | None:
    """Return the skill invoked by a Skill tool call in this event."""
    ordered_skills = sorted(skills, key=len, reverse=True)
    for tool_name, tool_input in iter_tool_uses(event):
        if tool_name.lower() not in SKILL_TOOLS:
            continue

        text = json.dumps(tool_input, ensure_ascii=False).lower()
        for skill in ordered_skills:
            if skill.lower() in text:
                return skill

        invoked = ""
        if isinstance(tool_input, dict):
            invoked = next(
                (
                    value.strip().lstrip("/")
                    for key in ("skill", "command", "name", "skill_name")
                    if isinstance((value := tool_input.get(key)), str) and value.strip()
                ),
                "",
            )
        return f"other:{invoked or 'unknown'}"
    return None


def classify(expect: str | None, observed: str | None) -> str:
    if observed is None:
        return "true_negative" if expect is None else "missed_trigger"
    if expect is None:
        return "false_trigger"
    return "correct_trigger" if observed == expect else "wrong_skill"


def claude_env(config_dir: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("CLAUDE_CODE_MAX_RETRIES", "0")
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def pump(stream, sink: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            sink.put(line)
    finally:
        sink.put(None)


def terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
        proc.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def run_case(case: Case, skills: list[str], args: argparse.Namespace) -> Outcome:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise SystemExit("error: 'claude' CLI not found on PATH")

    workspace = stage_workspace(skills)
    config_dir = (
        Path(tempfile.mkdtemp(prefix="routing-config-")) if args.isolate_config else None
    )
    command = [
        claude_bin,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--add-dir",
        str(workspace),
        "--model",
        args.model,
        "--effort",
        args.effort,
    ]
    if args.max_budget_usd > 0:
        command += ["--max-budget-usd", str(args.max_budget_usd)]

    spawn = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    events: list[dict] = []
    stderr_lines: list[str] = []
    observed: str | None = None
    error: str | None = None
    stop_reason = "completed"
    tool_calls = 0
    started = time.perf_counter()

    proc = subprocess.Popen(
        command,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=claude_env(config_dir),
        **spawn,
    )
    try:
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(case.prompt)
        proc.stdin.close()

        stdout_lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=pump, args=(proc.stdout, stdout_lines), daemon=True).start()
        threading.Thread(
            target=lambda: stderr_lines.extend(proc.stderr.readlines()), daemon=True
        ).start()

        deadline = time.perf_counter() + args.timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                stop_reason = "timeout"
                break
            try:
                line = stdout_lines.get(timeout=min(1.0, remaining))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)

            observed = detect_activation(event, skills)
            if observed:
                stop_reason = "skill_activated"
                break
            if event.get("type") == "result":
                stop_reason = "result"
                if event.get("is_error"):
                    error = str(event.get("result") or "Claude reported an error")[:400]
                break

            tool_calls += sum(
                1
                for name, _ in iter_tool_uses(event)
                if name.lower() not in BOOKKEEPING_TOOLS | SKILL_TOOLS
            )
            if tool_calls >= args.max_tool_calls:
                stop_reason = "tool_budget"
                break
    finally:
        terminate_process_tree(proc)
        elapsed = time.perf_counter() - started
        if args.keep_logs:
            logs_dir = Path(args.keep_logs)
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / f"{case.id}.jsonl").write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
                encoding="utf-8",
            )
        shutil.rmtree(workspace, ignore_errors=True)
        if config_dir:
            shutil.rmtree(config_dir, ignore_errors=True)

    if not events:
        error = ("".join(stderr_lines).strip() or "Claude produced no JSON events")[:400]
    if observed is None and (error or stop_reason in {"completed", "timeout"}):
        verdict = "error"
        error = error or f"run ended without a routing decision ({stop_reason})"
    else:
        verdict = classify(case.expect, observed)

    outcome = Outcome(
        id=case.id,
        category=case.category,
        prompt=case.prompt,
        expect=case.expect,
        observed=observed,
        verdict=verdict,
        passed=verdict in PASSING_VERDICTS,
        stop_reason=stop_reason,
        elapsed_s=round(elapsed, 2),
        tool_calls=tool_calls,
        error=error,
    )
    print(
        f"  [{'PASS' if outcome.passed else 'FAIL'}] {case.id}: "
        f"expected {case.expect or 'no skill'} -> got {observed or 'no skill'} "
        f"({verdict}, {stop_reason}, {outcome.elapsed_s}s)",
        flush=True,
    )
    return outcome


def summarize(outcomes: list[Outcome], meta: dict) -> dict:
    verdicts = Counter(outcome.verdict for outcome in outcomes)
    graded = [outcome for outcome in outcomes if outcome.verdict != "error"]
    passed = sum(outcome.passed for outcome in graded)
    return {
        "meta": meta,
        "totals": {
            "cases": len(outcomes),
            "graded": len(graded),
            "passed": passed,
            "errors": verdicts["error"],
            "accuracy": round(passed / len(graded), 3) if graded else None,
            "activations": sum(outcome.observed is not None for outcome in graded),
            "activations_expected": sum(outcome.expect is not None for outcome in graded),
        },
        "verdicts": {name: verdicts[name] for name in VERDICTS},
        "cases": [asdict(outcome) for outcome in outcomes],
    }


def render_markdown(summary: dict) -> str:
    totals = summary["totals"]
    accuracy = totals["accuracy"]
    lines = [
        "## Skill routing eval",
        "",
        f"**{totals['passed']}/{totals['graded']} correct "
        f"({'n/a' if accuracy is None else f'{accuracy:.1%}'})** across "
        f"{totals['cases']} prompts.",
        "",
        "| Verdict | Count |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| {verdict} | {summary['verdicts'][verdict]} |" for verdict in VERDICTS
    )

    failures = [case for case in summary["cases"] if not case["passed"]]
    lines += ["", "### Failures", ""]
    if not failures:
        lines.append("None.")
    else:
        lines += [
            "| Case | Expected | Observed | Verdict | Detail |",
            "| --- | --- | --- | --- | --- |",
        ]
        for case in failures:
            detail = (case["error"] or case["stop_reason"]).replace("|", "\\|")
            lines.append(
                f"| `{case['id']}` | {case['expect'] or '(none)'} | "
                f"{case['observed'] or '(none)'} | {case['verdict']} | {detail[:160]} |"
            )

    if totals["activations"] == 0 and totals["activations_expected"]:
        lines += [
            "",
            "> No skill activated in a case that expected one. Treat this run as "
            "invalid and inspect the raw logs.",
        ]
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="opus")
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "max"])
    parser.add_argument("--prompts", default=str(PROMPTS_FILE))
    parser.add_argument("--only", default="", help="Comma-separated case ids or skill names.")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--max-tool-calls", type=int, default=4)
    parser.add_argument("--max-budget-usd", type=float, default=0.75)
    parser.add_argument("--output", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--keep-logs", default="")
    parser.add_argument("--min-accuracy", type=float, default=0.0)
    parser.add_argument("--skip-preflight", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.model = enforce_model_policy(args.model)
    args.isolate_config = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    skills = marketplace_skills()
    cases = filter_cases(load_cases(Path(args.prompts)), args.only)
    if not args.isolate_config:
        print("[routing] warning: user-level Claude config is not isolated.")
    if not args.skip_preflight:
        ok, detail = check_api_reachable(args.model)
        if not ok:
            raise SystemExit(f"error: Claude API not reachable -- {detail}")

    print(f"[routing] {len(cases)} cases, {len(skills)} skills, model={args.model}")
    started = time.time()
    if args.jobs > 1 and len(cases) > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            outcomes = list(pool.map(lambda case: run_case(case, skills, args), cases))
    else:
        outcomes = [run_case(case, skills, args) for case in cases]

    summary = summarize(
        outcomes,
        {
            "model": args.model,
            "effort": args.effort,
            "skills": skills,
            "prompts": Path(args.prompts).name,
            "wall_time_s": round(time.time() - started, 1),
            "max_tool_calls": args.max_tool_calls,
            "max_budget_usd": args.max_budget_usd,
            "isolated_config_dir": args.isolate_config,
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        },
    )
    report = render_markdown(summary)
    output = (
        Path(args.output)
        if args.output
        else EVAL_DIR / "runs" / f"routing-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\n{report}[routing] JSON report: {output}")
    summary_path = args.summary or os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(report)

    totals = summary["totals"]
    if totals["graded"] == 0:
        return 1
    if totals["activations"] == 0 and totals["activations_expected"]:
        return 1
    if args.min_accuracy > 0 and (totals["accuracy"] or 0) < args.min_accuracy:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
