#!/usr/bin/env bash
# Per-task gate: ruff + mypy --strict (whole repo, unchanged) and only the pytest
# tests the diff affects. scripts/gate-full.sh stays the authoritative gate for
# milestones and merge. Usage:
#
#   bash scripts/gate-affected.sh [--base REF] [--strategy {auto,coverage,static}]
#       [--map PATH] [extra pytest args]
#
# The default strategy is coverage when a fresh map exists (see below), else the
# legacy static import graph. Any argument that is not --base/--strategy/--map is
# forwarded to pytest, e.g.
#   bash scripts/gate-affected.sh --base main -x
# The selected tests and the reasons they were selected are printed first, so the
# saved output is self-auditing. Exit 0 with pytest skipped means the diff touches
# no test (docs-only or a presentation-free change).
#
# Coverage map: scripts/build_test_map.py runs the full suite once under
# coverage.py and writes per-line test reachability as sharded JSON under
# $(git rev-parse --git-common-dir)/saberops-test-map/ (meta.json plus one shard
# per src file; shared by all worktrees, never committed). The header below prints
# the map commit and age; a map whose build commit is not an ancestor of HEAD is
# stale and the selector falls back to static selection. With no map at all, the
# header says so and the selector uses the static graph.
#
# Parallel pytest: when more than 50 tests are selected (or the FULL suite is
# required) and pytest-xdist is installed, pytest runs with -n auto.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

gate_python=python
if [[ -x .venv/bin/python ]]; then
    gate_python=.venv/bin/python
fi

base=""
strategy="auto"
map_path=""
pytest_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base)
            if [[ $# -lt 2 ]]; then
                echo "gate-affected: --base needs a git ref" >&2
                exit 2
            fi
            base="$2"
            shift 2
            ;;
        --base=*)
            base="${1#--base=}"
            shift
            ;;
        --strategy)
            if [[ $# -lt 2 ]]; then
                echo "gate-affected: --strategy needs auto, coverage or static" >&2
                exit 2
            fi
            strategy="$2"
            shift 2
            ;;
        --strategy=*)
            strategy="${1#--strategy=}"
            shift
            ;;
        --map)
            if [[ $# -lt 2 ]]; then
                echo "gate-affected: --map needs a path" >&2
                exit 2
            fi
            map_path="$2"
            shift 2
            ;;
        --map=*)
            map_path="${1#--map=}"
            shift
            ;;
        *)
            pytest_args+=("$1")
            shift
            ;;
    esac
done

explain_file=$(mktemp "${TMPDIR:-/tmp}/saberops-gate-affected.XXXXXX")
targets_file=$(mktemp "${TMPDIR:-/tmp}/saberops-gate-targets.XXXXXX")
trap 'rm -f -- "$explain_file" "$targets_file"' EXIT

selector_args=(--base "$base" --strategy "$strategy")
if [[ -n "$map_path" ]]; then
    selector_args+=(--map "$map_path")
fi
if ! selection=$("$gate_python" scripts/select_tests.py "${selector_args[@]}" --explain 2>"$explain_file"); then
    echo "gate-affected: test selection failed" >&2
    cat -- "$explain_file" >&2
    exit 2
fi

echo "=== affected-test gate (per task) ==="
echo "repository: $(pwd)"
if [[ -z "$map_path" ]]; then
    map_path="$(git rev-parse --git-common-dir)/saberops-test-map"
fi
if [[ -d "$map_path" ]]; then
    map_path="$map_path/meta.json"
fi
MAP_PATH="$map_path" "$gate_python" -c '
import json, os
from datetime import datetime, timezone
path = os.environ["MAP_PATH"]
try:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    built = payload.get("built_at", "?")
    try:
        age_s = int((datetime.now(timezone.utc) - datetime.fromisoformat(built)).total_seconds())
        if age_s < 3600:
            age = f"{age_s // 60}m"
        elif age_s < 86400:
            age = f"{age_s // 3600}h"
        else:
            age = f"{age_s // 86400}d"
    except ValueError:
        age = "?"
    commit = payload.get("commit", "?")
    test_count = payload.get("test_count", "?")
    blind = len(payload.get("blind", []))
    cov_version = payload.get("coverage_version", "?")
    print(f"coverage map: {path}")
    print(f"  commit {commit} built {built} (age {age}), "
          f"{test_count} tests, {blind} blind spots, coverage {cov_version}")
except (OSError, ValueError) as error:
    print(f"coverage map: none at {path} ({error}); using static selection")
'
cat -- "$explain_file"
echo
echo "--- pytest targets ---"
if [[ "$selection" == FULL* ]]; then
    echo "FULL suite: pytest runs the whole tests/ directory"
elif [[ -z "$selection" ]]; then
    echo "none: this diff affects no test"
else
    printf '%s\n' "$selection"
fi
echo

pytest_argv=(-q)
if (( ${#pytest_args[@]} > 0 )); then
    pytest_argv+=("${pytest_args[@]}")
fi

if [[ "$selection" == FULL* ]] || { [[ -n "$selection" ]] && (( $(printf '%s\n' "$selection" | wc -l) > 50 )); }; then
    if "$gate_python" -c "import xdist" 2>/dev/null; then
        echo "(>50 tests selected: pytest runs with -n auto)"
        pytest_argv+=(-n auto)
    else
        echo "(>50 tests selected but pytest-xdist is not installed: serial run)"
    fi
fi

echo "--- ruff check . ---"
"$gate_python" -m ruff check .

echo
echo "--- mypy --strict src tests ---"
"$gate_python" -m mypy --strict src tests

echo
if [[ "$selection" == FULL* ]]; then
    echo "--- pytest -q (full suite required) ---"
    "$gate_python" -m pytest "${pytest_argv[@]}"
elif [[ -z "$selection" ]]; then
    echo "--- pytest skipped: no test is affected by this diff ---"
    exit 0
else
    echo "--- pytest (affected tests only) ---"
    mapfile -t targets <<<"$selection"
    # Targets go through a file, not argv: parametrized node IDs can exceed the
    # kernel's single-argument limit (MAX_ARG_STRLEN). pytest.main is in-process,
    # so argv limits never apply; flags behave exactly as on the CLI.
    printf '%s\n' "${targets[@]}" >"$targets_file"
    "$gate_python" - "$targets_file" "${pytest_argv[@]}" <<'PYEOF'
import sys
from pathlib import Path
import pytest
targets = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
raise SystemExit(pytest.main([*sys.argv[2:], *targets]))
PYEOF
fi
