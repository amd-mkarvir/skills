# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# See LICENSE for license information.

"""TraceLens fixtures and scoring for the `gemm-01-repeatability` behavior case.

Prompts and expectations live in ``evals.json``; this file holds only what the
dataset format cannot express -- an upstream fixture trace and a scoring script
that lives in someone else's repo.

The case mirrors what ``run_repeatability_parallel.sh`` schedules first: the
Phase-1 agent workflow on ``gemm_01_compute_few_tiles`` from
``combined_traces_standalone.csv``, then the first Phase-2 eval,
``workflow_scripted_evals.py``, over whatever the agent produced.

Nothing here clones or installs. The three artifacts the case needs are ~1 MB
in total and are fetched individually over HTTPS, because a checkout of
AMD-AGI/TraceLens costs well over 100 MB -- its HEAD tree carries ~133 MB of
end-to-end fixtures this case never opens -- to yield one 981 KB archive:

  * ``combined_traces_standalone.csv`` -- names the case and its platform.
  * ``unit_tests_standalone.tar.gz``   -- holds the trace itself.
  * ``workflow_scripted_evals.py``     -- upstream's Phase-2 scorer, which
    imports nothing but the standard library and so runs under this process's
    interpreter with no environment of its own.

Putting TraceLens itself on the machine is left to the skill. reference.md
Step 0 discovers the install, or asks for one, and refuses to advance until
``import TraceLens`` succeeds; staging it here would skip the part of the
workflow the case is meant to exercise.

The runner calls, in order:

  * ``setup_session(cache_dir)`` -- once per run. Stages the fixture outside
    any agent workspace and returns the paths the prompt interpolates.
  * ``setup(workspace, case, ctx)`` -- per case. Creates the output directory
    inside the agent's workspace and hands back its absolute path.
  * ``check(run, case, ctx)``     -- per case, after grading. Runs TraceLens's
    own scorer; anything it flags fails the case.

Environment overrides: ``TRACELENS_REPO`` and ``TRACELENS_REF``.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

TRACELENS_REPO = os.environ.get("TRACELENS_REPO", "AMD-AGI/TraceLens")
# A branch floats: the fixture, the scorer, and whatever TraceLens the agent
# installs can drift apart between runs. Pin a commit here to make a green run
# reproducible and an upstream bump a reviewable diff.
TRACELENS_REF = os.environ.get("TRACELENS_REF", "").strip() or "main"

ANALYSIS_TESTS = "agent_evals/Analysis/analysis_tests"
COMBINED_TRACES_CSV = f"{ANALYSIS_TESTS}/combined_traces_standalone.csv"
UNIT_TESTS_ARCHIVE = f"{ANALYSIS_TESTS}/unit_tests_standalone.tar.gz"
WORKFLOW_EVALS = "agent_evals/Analysis/eval_utils/workflow_scripted_evals.py"

FETCH_TIMEOUT_S = 120

# The default repeatability order starts here. Asserted rather than assumed, so
# an upstream reordering surfaces as a clear failure instead of silently
# scoring a different case than the one this file documents.
EXPECTED_CASE_ID = "gemm_01_compute_few_tiles"

# An analysis.md this short is a stub, not a report.
MIN_ANALYSIS_BYTES = 100


def _raw_url(repo_path: str) -> str:
    return f"https://raw.githubusercontent.com/{TRACELENS_REPO}/{TRACELENS_REF}/{repo_path}"


def _fetch(repo_path: str) -> bytes:
    url = _raw_url(repo_path)
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as response:
            return response.read()
    except OSError as exc:
        raise RuntimeError(f"could not fetch {url}: {exc}") from exc


def _extract_unit_tests(cache_dir: Path) -> None:
    """Unpack the fixture archive into `cache_dir`.

    Members are rooted at ``agent_evals/Analysis/analysis_tests/...`` as they
    are in the repo, so extracting at `cache_dir` makes the CSV's repo-relative
    ``trace_path`` resolve against it unchanged.
    """
    payload = _fetch(UNIT_TESTS_ARCHIVE)
    # `data` rejects absolute paths, traversal, and special files. Default from
    # 3.14, a warning before that, and unsupported in 3.11 and older.
    guard = {"filter": "data"} if sys.version_info >= (3, 12) else {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        tar.extractall(path=cache_dir, **guard)


def setup_session(cache_dir: Path) -> dict:
    """Stage the fixture trace and the scorer, outside any agent workspace."""
    print(f"  [setup] fetching TraceLens fixtures from {TRACELENS_REPO}@{TRACELENS_REF}", flush=True)

    manifest = _fetch(COMBINED_TRACES_CSV).decode("utf-8")
    row = next(csv.DictReader(io.StringIO(manifest)))
    if row["id"] != EXPECTED_CASE_ID:
        raise RuntimeError(
            f"expected the first standalone repeatability case to be "
            f"{EXPECTED_CASE_ID}, found {row['id']}; upstream reordered the CSV."
        )

    _extract_unit_tests(cache_dir)
    trace_path = (cache_dir / row["trace_path"]).resolve()
    if not trace_path.is_file():
        raise FileNotFoundError(f"trace file missing after extract: {trace_path}")

    scorer = (cache_dir / "workflow_scripted_evals.py").resolve()
    scorer.write_bytes(_fetch(WORKFLOW_EVALS))

    return {
        "trace_path": trace_path,
        "platform": row["platform"],
        "scorer_path": scorer,
    }


def setup(workspace: Path, case, ctx: dict) -> dict:
    """Create the output directory the prompt points the agent at."""
    output_dir = workspace / "analysis_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return {"output_dir": output_dir}


def check(run, case, ctx: dict) -> None:
    """Score the agent's report with TraceLens's own Phase-2 eval."""
    output_dir = Path(ctx["output_dir"])
    scorer = Path(ctx["scorer_path"])

    analysis_md = output_dir / "analysis.md"
    assert analysis_md.is_file(), f"no report written to {analysis_md}"
    size = analysis_md.stat().st_size
    assert size >= MIN_ANALYSIS_BYTES, (
        f"analysis.md is only {size} bytes; expected at least {MIN_ANALYSIS_BYTES}"
    )

    results_csv = output_dir / "workflow_scripted_results.csv"
    # The scorer exits non-zero whenever a row fails, which is the outcome this
    # hook exists to report. Read the CSV it wrote rather than the exit code, so
    # a graded failure arrives as a legible assertion instead of a crash.
    proc = subprocess.run(
        [
            sys.executable,
            str(scorer),
            "--output-dir", str(output_dir),
            "--results", str(results_csv),
            "--comparison-scope", "standalone",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert results_csv.is_file(), (
        f"workflow_scripted_evals.py wrote no results ({proc.returncode}):\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )

    with results_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows, f"workflow eval produced no rows: {results_csv}"

    failures = [row for row in rows if row.get("result") != "PASS"]
    assert not failures, "workflow_scripted_evals.py reported failures:\n" + "\n".join(
        f"  - {row.get('issue_summary')}: {row.get('details')}" for row in failures[:10]
    )
