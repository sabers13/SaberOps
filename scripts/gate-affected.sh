#!/usr/bin/env bash
# Per-task gate: ruff + mypy --strict (whole repo, unchanged) and only the pytest
# files the diff affects. scripts/gate-full.sh stays the authoritative gate for
# milestones and merge. Usage:
#
#   bash scripts/gate-affected.sh [--base REF] [extra pytest args]
#
# Any argument that is not --base/--base=REF is forwarded to pytest, e.g.
#   bash scripts/gate-affected.sh --base main -x
# The selected tests and the reasons they were selected are printed first, so the
# saved output is self-auditing. Exit 0 with pytest skipped means the diff touches
# no test (docs-only or a presentation-free change).
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

gate_python=python
if [[ -x .venv/bin/python ]]; then
    gate_python=.venv/bin/python
fi

base=""
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
        *)
            pytest_args+=("$1")
            shift
            ;;
    esac
done

explain_file=$(mktemp "${TMPDIR:-/tmp}/saberops-gate-affected.XXXXXX")
trap 'rm -f -- "$explain_file"' EXIT

if ! selection=$("$gate_python" scripts/select_tests.py --base "$base" --explain 2>"$explain_file"); then
    echo "gate-affected: test selection failed" >&2
    cat -- "$explain_file" >&2
    exit 2
fi

echo "=== affected-test gate (per task) ==="
echo "repository: $(pwd)"
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
    "$gate_python" -m pytest "${pytest_argv[@]}" "${targets[@]}"
fi
