#!/usr/bin/env bash
# One integration gate: owned scratch, host-gates slot, one bounded parallel pass and JUnit evidence.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
gate_python=python
if [[ -x .venv/bin/python ]]; then
    gate_python=.venv/bin/python
fi
exec "$gate_python" scripts/gate_tools.py full "$@"
