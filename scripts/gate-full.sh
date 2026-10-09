#!/usr/bin/env bash
# The unchanged README / GitHub Actions checks. Run at explicit UI FINALIZE.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

gate_python=python
if [[ -x .venv/bin/python ]]; then
    gate_python=.venv/bin/python
fi

"$gate_python" -m ruff check .
"$gate_python" -m mypy --strict src tests
"$gate_python" -m pytest -q
