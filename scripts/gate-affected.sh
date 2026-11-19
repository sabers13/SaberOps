#!/usr/bin/env bash
# Per-task gate: ruff + mypy --strict (whole repo) and only the pytest tests the
# diff affects. scripts/gate-full.sh stays the authoritative gate for milestones
# and merge.
#
# Usage:
#
#   bash scripts/gate-affected.sh [--base REF] [--strategy {auto,coverage,static}]
#       [--map PATH] [extra pytest args | PATH_OR_NODE ...]
#
# This script and scripts/lane-check.sh share ONE selection implementation:
# scripts/gate_tools.py's lane runner, which calls scripts/select_tests.py.  The
# selected tests and the reasons they were selected are printed first, so the
# saved output is self-auditing.  Selected node IDs are resolved against the
# current tree (pytest --collect-only): ghosts from a stale map are dropped, and
# a non-empty selection that resolves to nothing FAILS instead of passing with
# zero tests.  Exit 0 with pytest skipped means the diff affects no test
# (docs-only change).
#
# Coverage map: scripts/build_test_map.py writes executed-code reachability to
# $(git rev-parse --git-common-dir)/saberops-test-map/ (shared by every worktree,
# never committed).  Without a fresh map the selector uses the narrower static
# direct-importer rule for automatic selection; --strategy static asks for the
# legacy transitive closure explicitly.
#
# Memory: the lane runner acquires the same host-wide memory ledger and cgroup
# ceiling as the full gate; there is no separate unguarded pytest path.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

gate_python=python
if [[ -x .venv/bin/python ]]; then
    gate_python=.venv/bin/python
fi

lane_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base | --strategy | --map)
            if [[ $# -lt 2 ]]; then
                echo "gate-affected: $1 needs a value" >&2
                exit 2
            fi
            lane_args+=("$1" "$2")
            shift 2
            ;;
        --base=* | --strategy=* | --map=*)
            lane_args+=("$1")
            shift
            ;;
        -*)
            lane_args+=(--pytest-arg "$1")
            shift
            ;;
        *)
            # Positional arguments are explicit extra targets, matching the
            # historical gate-affected.sh contract (e.g. a test node ID).
            lane_args+=(--also "$1")
            shift
            ;;
    esac
done

exec "$gate_python" scripts/gate_tools.py lane "${lane_args[@]}"
