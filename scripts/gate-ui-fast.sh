#!/usr/bin/env bash
# Presentation-only iteration; scope and browser requirements are in AGENTS.md.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

gate_python=python
if [[ -x .venv/bin/python ]]; then
    gate_python=.venv/bin/python
fi

command -v node >/dev/null

"$gate_python" - <<'PY'
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

template_dir = Path("src/saberops/templates")
env = Environment(loader=FileSystemLoader(template_dir), autoescape=select_autoescape())
names = env.list_templates(extensions=["html"])
if not names:
    raise SystemExit("No Jinja templates found")
for name in names:
    env.get_template(name)
print(f"Jinja compilation: {len(names)} templates passed")
PY

for asset in src/saberops/static/js/*.js; do
    node --check "$asset"
done

# Keep inherited dogfood paths/profiles out of these in-process rendering checks.
ui_gate_scratch=$(mktemp -d "${TMPDIR:-/tmp}/saberops-ui-fast.XXXXXX")
trap 'rm -rf -- "$ui_gate_scratch"' EXIT
export XDG_STATE_HOME="$ui_gate_scratch/state"
export XDG_CONFIG_HOME="$ui_gate_scratch/config"
export XDG_CACHE_HOME="$ui_gate_scratch/cache"
unset SABEROPS_PROFILE ORCH_ACCESS_STORE ORCH_ACCESS_MODELS_STORE
unset ORCH_BINDING_STORE ORCH_READINESS_STORE

# Extra arguments should be inspected, affected rendering tests by exact node ID.
"$gate_python" -m pytest -q \
    tests/test_c16c2_console.py::test_c16c2_dashboard_renders_approved_shell \
    tests/test_c16c2_console.py::test_c16c2_run_detail_renders_inspector_and_actions \
    tests/test_c16c2_console.py::test_c16c2_missing_run_keeps_shell \
    tests/test_c16c2_console.py::test_c16c2_console_assets_are_served \
    "$@"
