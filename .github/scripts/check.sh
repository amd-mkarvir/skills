#!/usr/bin/env bash
# Validate the federation file, every SKILL.md and eval dataset, and that
# generated plugin manifests are up to date. Runs no agent, clones nothing,
# and spends no tokens. The dataset check runs the same build of skillscope CI
# grades with, so that one step needs network access the first time; set
# SKILLSCOPE_VERSION to try a different build.
#
# Usage:
#   ./.github/scripts/check.sh              Validate every skill, dataset, and manifest.
#   ./.github/scripts/check.sh -h|--help    Print this help.
#
# Requires `uv` (https://github.com/astral-sh/uv).

set -euo pipefail

# Keep these two in step with .github/workflows/evals.yml, which is where the
# harness version and the layout of this catalog are configured. There is no
# config file to read them from: the workflow is the only config surface, and
# duplicating a value a human can see beats parsing YAML in bash.
SKILLSCOPE_PIN="main"
SKILL_GLOBS="skills/*"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

usage() {
  sed -n 's/^# \{0,1\}//p' "${BASH_SOURCE[0]}" | sed -n '/^Usage:/,/^Requires/p'
}

case "${1:-}" in
  "")
    uv run .github/scripts/federate_skills.py --check-catalog
    uv run .github/scripts/validate_skills.py
    uvx --from "git+https://github.com/amd/skillscope@${SKILLSCOPE_VERSION:-$SKILLSCOPE_PIN}" \
      skillscope validate --skills "$SKILL_GLOBS"
    uv run .github/scripts/generate_cursor_marketplace.py --check
    uv run .github/scripts/generate_codex_plugin.py --check
    ;;
  -h|--help)
    usage
    ;;
  *)
    echo "Unknown option: $1" >&2
    echo "Run with --help for usage." >&2
    exit 2
    ;;
esac
