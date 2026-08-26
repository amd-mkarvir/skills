#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Tests for the federation machinery. No network, no clones, no tokens.

    uv run .github/scripts/test_federate_skills.py

Two behaviors carry the whole mechanism and both fail silently when broken,
so they are guarded here:

  - Change detection. The nightly workflow opens a pull request whenever the
    work tree is dirty, so a content hash that moves for the wrong reason
    turns every quiet night into a no-op pull request.
  - `main`-only tracking. A source entry that manages to name a ref must
    fail the run, not get silently coerced to `main`.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import federate_skills as fed  # noqa: E402


def parse(payload: dict) -> list[fed.Source]:
    with tempfile.TemporaryDirectory() as tmp:
        catalog = Path(tmp) / "federation.json"
        catalog.write_text(json.dumps(payload), encoding="utf-8")
        return fed.parse_federation(catalog)


def one_source(**overrides) -> dict:
    source = {
        "repo": "AMD-Org/MyProject",
        "license": "MIT",
        "skills": [{"path": "skills/my-skill", "as": "myproject-my-skill"}],
    }
    source.update(overrides)
    return {"sources": [source]}


def write_skill(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class TestFederationFile(unittest.TestCase):
    def test_declares_each_skill_by_its_full_path(self):
        sources = parse(
            one_source(
                skills=[
                    {"path": "skills/first"},
                    {"path": "tools/agents/skills/second", "as": "myproject-second"},
                ]
            )
        )
        self.assertEqual(len(sources), 1)
        first, second = sources[0].skills
        self.assertEqual(first.path, "skills/first")
        self.assertEqual(first.folder, "first")
        self.assertEqual(first.dest_name, "first")
        self.assertEqual(second.folder, "second")
        self.assertEqual(second.dest_name, "myproject-second")

    def test_source_slug_derives_from_repo(self):
        self.assertEqual(parse(one_source())[0].name, "amd-org-myproject")

    def test_tracks_main_and_rejects_any_other_ref(self):
        self.assertEqual(parse(one_source())[0].ref, "main")
        for key in ("ref", "branch", "tag", "commit"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as ctx:
                    parse(one_source(**{key: "release/1.0"}))
                self.assertIn("main", str(ctx.exception))

    def test_rejects_the_previous_yaml_schema(self):
        # A source-level `path` plus skills named by folder is how the old
        # catalog addressed skills. Reject it so a stale entry can't resolve
        # to the wrong folder.
        with self.assertRaises(ValueError):
            parse(one_source(name="amd-myproject", path="skills"))
        with self.assertRaises(ValueError):
            parse(one_source(skills=[{"name": "my-skill"}]))
        with self.assertRaises(ValueError):
            parse(one_source(skills=["my-skill"]))

    def test_rejects_malformed_entries(self):
        for payload in (
            {},
            {"sources": []},
            one_source(repo="not-a-repo"),
            one_source(repo=None),
            one_source(skills=[]),
            one_source(skills=[{"path": ""}]),
            one_source(skills=[{"path": "skills/../../etc"}]),
            one_source(skills=[{"path": "skills/x", "as": "nested/name"}]),
            one_source(skills=[{"path": "skills/x", "typo": 1}]),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse(payload)

    def test_accepts_windows_style_separators_in_paths(self):
        skills = parse(
            one_source(skills=[{"path": r"TraceLens\Agent\skills\orchestrator"}])
        )[0].skills
        self.assertEqual(skills[0].path, "TraceLens/Agent/skills/orchestrator")


class TestChangeDetection(unittest.TestCase):
    def test_hash_covers_contents_and_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = write_skill(
                Path(tmp) / "a", {"SKILL.md": "body", "agents/one.md": "x"}
            )
            same = write_skill(
                Path(tmp) / "b", {"SKILL.md": "body", "agents/one.md": "x"}
            )
            edited = write_skill(
                Path(tmp) / "c", {"SKILL.md": "body!", "agents/one.md": "x"}
            )
            renamed = write_skill(
                Path(tmp) / "d", {"SKILL.md": "body", "agents/two.md": "x"}
            )
            added = write_skill(
                Path(tmp) / "e",
                {"SKILL.md": "body", "agents/one.md": "x", "extra.md": ""},
            )
            digest = fed.content_hash(base)
            self.assertEqual(digest, fed.content_hash(same))
            for other in (edited, renamed, added):
                self.assertNotEqual(digest, fed.content_hash(other))

    def test_up_to_date_only_when_hash_repo_path_and_ref_all_match(self):
        source = parse(one_source())[0]
        spec = source.skills[0]
        marker = {
            "repo": "AMD-Org/MyProject",
            "ref": "main",
            "path": "skills/my-skill",
            "content_hash": "abc",
        }
        self.assertTrue(fed.is_up_to_date(marker, source, spec, "abc"))
        # A marker written before content hashing existed re-vendors once.
        legacy = {k: v for k, v in marker.items() if k != "content_hash"}
        self.assertFalse(fed.is_up_to_date(legacy, source, spec, "abc"))
        for key, value in (
            ("content_hash", "def"),
            ("repo", "AMD-Org/Other"),
            ("path", "skills/elsewhere"),
            ("ref", "release/1.0"),
        ):
            with self.subTest(key=key):
                self.assertFalse(
                    fed.is_up_to_date({**marker, key: value}, source, spec, "abc")
                )


class TestPullRequestSummary(unittest.TestCase):
    def result(self, folder: str, commit: str, updated: bool = True):
        source = fed.Source(repo="AMD-Org/MyProject", license="MIT")
        return fed.ImportResult(
            source=source,
            folder=folder,
            path=f"skills/{folder}",
            commit=commit,
            updated=updated,
        )

    def test_single_bump_names_the_skill_and_short_commit(self):
        summary = fed.build_summary([self.result("my-skill", "f4af496dbe9c")], [])
        self.assertTrue(summary["changed"])
        self.assertEqual(summary["title"], "Bump my-skill to f4af496")
        self.assertIn("f4af496", summary["body"])

    def test_nothing_changed_means_no_pull_request(self):
        summary = fed.build_summary(
            [self.result("my-skill", "f4af496dbe9c", updated=False)], []
        )
        self.assertFalse(summary["changed"])
        self.assertEqual(summary["title"], "")
        self.assertEqual(summary["unchanged"], ["my-skill"])

    def test_titles_for_multi_skill_and_removal_runs(self):
        two = [self.result("a", "1111111"), self.result("b", "2222222")]
        self.assertEqual(
            fed.build_summary(two, [])["title"], "Bump 2 federated skills"
        )
        self.assertEqual(
            fed.build_summary([], ["gone"])["title"],
            "Remove federated skill gone",
        )
        self.assertEqual(
            fed.build_summary(two[:1], ["gone"])["title"],
            "Update federated skills (1 bumped, 1 removed)",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
