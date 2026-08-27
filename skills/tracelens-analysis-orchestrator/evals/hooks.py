# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# See LICENSE for license information.

"""TraceLens setup and scoring for the `gemm-01-repeatability` behavior case.

Prompts and expectations live in ``evals.json``; this file holds only what the
dataset format cannot express -- a virtualenv and a scoring script that lives
in the repo this skill is authored in.

Which TraceLens tree to grade against is deliberately not decided here. The
runner resolves it and passes it in as ``ctx["source_dir"]``: the pull
request's own checkout when TraceLens runs these evals, the commit recorded in
``.federated.json`` when the catalog runs them. A hook that cloned TraceLens
itself could only name a branch, so it would grade whatever ``main`` held --
never the change under review. Set ``SKILL_SOURCE_DIR`` to point the run at a
different checkout.

The case mirrors what ``run_repeatability_parallel.sh`` schedules first: the
Phase-1 agent workflow on the first standalone case in
``combined_traces_standalone.csv``, then the first Phase-2 eval,
``workflow_scripted_evals.py``, over whatever the agent produced.

The runner calls, in order:

  * ``setup_session(cache_dir, ctx)`` -- once per run. Installs TraceLens from
    ``ctx["source_dir"]`` into a virtualenv outside any agent workspace, and
    returns the paths the prompt interpolates.
  * ``setup(workspace, case, ctx)`` -- per case. Creates the output directory
    inside the agent's workspace and hands back its absolute path.
  * ``check(run, case, ctx)``     -- per case, after grading. Runs TraceLens's
    own scorer; anything it flags fails the case.
"""

from __future__ import annotations

import csv
import subprocess
import sys
import tarfile
from pathlib import Path

UNIT_TESTS_ARCHIVE = "unit_tests_standalone.tar.gz"
ANALYSIS_TESTS = "agent_evals/Analysis/analysis_tests"
COMBINED_TRACES_CSV = f"{ANALYSIS_TESTS}/combined_traces_standalone.csv"
WORKFLOW_EVALS = "agent_evals/Analysis/eval_utils/workflow_scripted_evals.py"

# An analysis.md this short is a stub, not a report.
MIN_ANALYSIS_BYTES = 100


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def _extract_unit_tests(tracelens_dir: Path) -> None:
    archive = tracelens_dir / ANALYSIS_TESTS / UNIT_TESTS_ARCHIVE
    if not archive.is_file():
        raise FileNotFoundError(f"unit test archive not found: {archive}")
    if (tracelens_dir / ANALYSIS_TESTS / "unit_tests_standalone").is_dir():
        return
    # Archive members are rooted at agent_evals/Analysis/analysis_tests/...
    # under the repo, so extract at tracelens_dir rather than at that subtree.
    # In place, because the Phase-2 scorer resolves fixtures relative to it.
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(path=tracelens_dir)


def _install_tracelens_venv(cache_dir: Path, tracelens_dir: Path) -> Path:
    venv_dir = cache_dir / ".venv"
    if not venv_dir.exists():
        _run([sys.executable, "-m", "venv", str(venv_dir)], cwd=cache_dir)
    pip = venv_dir / "bin" / "pip"
    python = venv_dir / "bin" / "python"
    _run([str(pip), "install", "--upgrade", "pip"], cwd=cache_dir)
    _run([str(pip), "install", "-e", str(tracelens_dir)], cwd=cache_dir)
    _run([str(python), "-c", "import TraceLens"], cwd=cache_dir)
    return venv_dir


def setup_session(cache_dir: Path, ctx: dict) -> dict:
    """Install TraceLens from the runner-supplied checkout, once per run."""
    tracelens_dir = Path(ctx["source_dir"]).resolve()
    if not (tracelens_dir / ANALYSIS_TESTS).is_dir():
        raise RuntimeError(
            f"{tracelens_dir} does not look like a TraceLens checkout: expected "
            f"{ANALYSIS_TESTS}/ beneath it. The runner resolves this from "
            "SKILL_SOURCE_DIR, then .federated.json, then the enclosing git repo."
        )

    print(f"  [setup] installing TraceLens from {tracelens_dir} (slow, once per run)", flush=True)
    _extract_unit_tests(tracelens_dir)

    with (tracelens_dir / COMBINED_TRACES_CSV).open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    # Whichever case upstream schedules first is the one graded; logged rather
    # than asserted, because the tree is pinned to a commit and a pull request
    # is allowed to reorder its own fixtures.
    print(f"  [setup] grading the first standalone repeatability case: {row['id']}", flush=True)

    trace_path = (tracelens_dir / row["trace_path"]).resolve()
    if not trace_path.is_file():
        raise FileNotFoundError(f"trace file missing after extract: {trace_path}")

    venv_dir = _install_tracelens_venv(cache_dir, tracelens_dir).resolve()
    return {
        "venv_path": venv_dir,
        "trace_path": trace_path,
        "platform": row["platform"],
    }


def setup(workspace: Path, case, ctx: dict) -> dict:
    """Create the output directory the prompt points the agent at."""
    output_dir = workspace / "analysis_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return {"output_dir": output_dir}


def check(run, case, ctx: dict) -> None:
    """Score the agent's report with TraceLens's own Phase-2 eval."""
    output_dir = Path(ctx["output_dir"])
    tracelens_dir = Path(ctx["source_dir"])
    venv_python = Path(ctx["venv_path"]) / "bin" / "python"

    analysis_md = output_dir / "analysis.md"
    assert analysis_md.stat().st_size >= MIN_ANALYSIS_BYTES, (
        f"analysis.md is only {analysis_md.stat().st_size} bytes; expected at "
        f"least {MIN_ANALYSIS_BYTES}"
    )

    results_csv = output_dir / "workflow_scripted_results.csv"
    _run(
        [
            str(venv_python),
            str(tracelens_dir / WORKFLOW_EVALS),
            "--output-dir", str(output_dir),
            "--results", str(results_csv),
            "--comparison-scope", "standalone",
        ],
        cwd=tracelens_dir,
    )

    with results_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows, f"workflow eval produced no rows: {results_csv}"

    failures = [row for row in rows if row.get("result") != "PASS"]
    assert not failures, "workflow_scripted_evals.py reported failures:\n" + "\n".join(
        f"  - {row.get('issue_summary')}: {row.get('details')}" for row in failures[:10]
    )
