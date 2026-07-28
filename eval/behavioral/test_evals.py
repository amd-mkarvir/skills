# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
#
# See LICENSE for license information.

"""Data-driven behavioral loader.

Most skills describe their behavioral coverage declaratively in
``skills/<skill>/evals/evals.json`` instead of hand-written pytest. This module
is the *only* code needed to run them: it discovers those datasets, stages the
skill workspace, runs the agent once per case, and applies the case's checks.

Skill selection:

  * ``BEHAVIORAL_SKILL=<name>`` (set per matrix job in CI) -> run only that
    skill's ``evals.json``.
  * unset (local full run) -> run every skill that has an ``evals.json``.

Dataset shape (``evals.json``)::

    {
      "skill": "local-ai-use",     # optional, informational
      "model": "opus",             # optional default model for all cases
      "effort": "high",            # optional default effort for all cases
      "cases": [
        {
          "id": "generate_image_of_a_cat",   # required, unique within the file
          "prompt": "…",                      # required, the agent instruction
          "model": "opus",                    # optional, overrides file default
          "effort": "high",                   # optional, overrides file default
          "workspace_files": {                # optional seed files, path -> text
            "main.py": "from openai import OpenAI\nclient = OpenAI()\n"
          },
          "logs_contains": ["local-ai-use"],  # deterministic substring checks
          "workspace_contains": ["out.png"],  # deterministic file-exists checks
          "should": ["Install Lemonade…"],    # LLM-judged positive expectations
          "should_not": ["Use a cloud API"]   # LLM-judged negative expectations
        }
      ]
    }

Each ``logs_contains`` / ``workspace_contains`` / ``should`` / ``should_not``
value may be a single string or a list of strings. Anything a case needs beyond
this (custom setup, code-graded output) belongs in a hand-written ``evals.py``
for that skill instead -- both are collected side by side.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness import DEFAULT_EFFORT, SKILLS_DIR, claude

EVAL_JSON = Path("evals") / "evals.json"


def _target_skills() -> list[str]:
    """Skills to load cases for: the pinned one in CI, else every JSON skill."""
    selected = os.environ.get("BEHAVIORAL_SKILL", "").strip()
    if selected:
        return [selected]
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in SKILLS_DIR.iterdir() if (p / EVAL_JSON).is_file()
    )


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _load_cases() -> list[tuple[str, dict]]:
    """Flatten every target skill's ``evals.json`` into (skill, case) pairs."""
    pairs: list[tuple[str, dict]] = []
    for skill in _target_skills():
        dataset_path = SKILLS_DIR / skill / EVAL_JSON
        if not dataset_path.is_file():
            continue
        data = json.loads(dataset_path.read_text(encoding="utf-8"))
        file_model = data.get("model", "opus")
        file_effort = data.get("effort", DEFAULT_EFFORT)
        for case in data.get("cases", []):
            case.setdefault("_model", case.get("model", file_model))
            case.setdefault("_effort", case.get("effort", file_effort))
            pairs.append((skill, case))
    return pairs


_CASES = _load_cases()
_IDS = [f"{skill}::{case.get('id', 'case')}" for skill, case in _CASES]


@pytest.mark.parametrize(("skill", "case"), _CASES, ids=_IDS)
def test_behavioral(skill: str, case: dict) -> None:
    with claude(case["_model"], skill=skill, effort=case["_effort"]) as agent:
        for rel_path, content in (case.get("workspace_files") or {}).items():
            target = agent.workspace / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        run = agent.prompt(case["prompt"])

        for text in _as_list(case.get("logs_contains")):
            run.logs_contains(text)
        for path in _as_list(case.get("workspace_contains")):
            run.workspace_contains(path)
        for statement in _as_list(case.get("should")):
            run.should(statement)
        for statement in _as_list(case.get("should_not")):
            run.should_not(statement)
