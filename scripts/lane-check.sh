#!/usr/bin/env bash
# Cheap lane evidence: whole-repo lint/types and explicit diff-derived test targets.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
gate_python=python
if [[ -x .venv/bin/python ]]; then
    gate_python=.venv/bin/python
fi
exec "$gate_python" scripts/gate_tools.py lane "$@"
