#!/usr/bin/env bash
# UI-only entry points into the same owned scratch lifecycle as full/lane gates.
gate_scratch_start() {
    local scratch_manifest
    scratch_manifest=$(mktemp "${TMPDIR:-/tmp}/saberops-ui-owner.XXXXXX")
    if "$gate_python" scripts/gate_tools.py scratch-start "$scratch_manifest" "$$"; then
        :
    else
        local scratch_rc=$?
        rm -f -- "$scratch_manifest"
        return "$scratch_rc"
    fi
    gate_scratch=$(cat "$scratch_manifest")
    rm -f -- "$scratch_manifest"
    trap 'gate_scratch_finish "$?"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
}

gate_scratch_finish() {
    "$gate_python" scripts/gate_tools.py scratch-finish "$gate_scratch" "$1"
}

gate_pytest() {
    timeout "${SABEROPS_LANE_TIMEOUT:-1200}" "$gate_python" -m pytest \
        --basetemp "$gate_scratch/pytest-ui" "$@"
}
