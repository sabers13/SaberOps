#!/usr/bin/env bash
# Presentation-only iteration; scope and browser requirements are in AGENTS.md.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

gate_python=python
if [[ -x .venv/bin/python ]]; then
    gate_python=.venv/bin/python
fi

source scripts/gate_lib.sh
gate_scratch_start

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

# Keep inherited dogfood profiles and store overrides out of these in-process
# rendering checks.  XDG isolation belongs to tests/conftest.py, which points
# every unset XDG base at the test's own scratch tree and refuses a base that
# resolves outside it, so this wrapper deliberately does not pin one.
unset XDG_STATE_HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME
unset SABEROPS_PROFILE ORCH_ACCESS_STORE ORCH_ACCESS_MODELS_STORE
unset ORCH_BINDING_STORE ORCH_READINESS_STORE

# Extra arguments should be inspected, affected rendering tests by exact node ID.
gate_pytest -q \
    tests/test_web.py::test_h23_root_opens_setup \
    tests/test_web.py::test_h23_retired_web_routes_are_gone \
    tests/test_web.py::test_h23_console_assets_are_served \
    tests/test_web.py::test_c16c2_health_stays_json \
    tests/test_web.py::test_ui1_onboarding_sections_render \
    "$@"
