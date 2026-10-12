#!/usr/bin/env bash
# Nightly full gate on main: deterministic, no model, idempotent.
#
# What it does (see docs/operations.md "Nightly full gate"):
#  1. verifies the nightly worktree is the tool's own detached `_nightly`
#     worktree (refuses anything else with exit 2) and refreshes it at main,
#  2. ensures its venv is current,
#  3. skips only when main is unchanged since the last green nightly
#     (unless SABEROPS_NIGHTLY_REPEAT=1),
#  4. runs the full gate with SABEROPS_GATE_LABEL=nightly,
#  5. rebuilds the coverage map when the gate is satisfied (PASS or
#     quarantined-only FLAKY_PASS; never on FAIL, TIMEOUT or INFRA),
#  6. writes flaky-report, summary.md and $SABEROPS_ORCH_DIR/nightly/latest.md,
#  7. deletes nightly output dirs older than 30 days (only inside a `nightly`
#     cache dir; anything else is refused and skipped).
#
# Environment overrides (defaults are the production paths):
#  SABEROPS_NIGHTLY_MAIN   repository to gate (default: git toplevel of cwd).
#  SABEROPS_NIGHTLY_WT     nightly worktree (default /home/saber/Projects/SaberOps-wt/_nightly).
#                          An existing directory is used only when it is a detached
#                          worktree of this repository whose basename is `_nightly`;
#                          anything else is refused with exit 2 (see step 1).
#  SABEROPS_NIGHTLY_ALLOW_PATH=1
#                          permits an existing detached worktree with a basename other
#                          than `_nightly`. Test-only escape hatch.
#  SABEROPS_ORCH_DIR       .orch directory for nightly/latest.md
#                          (default /home/saber/Projects/SaberOps-dogfood-0.3.7/.orch).
#  SABEROPS_NIGHTLY_CACHE  nightly output root
#                          (default $HOME/.cache/saberops-gate/nightly). Test-only.
#  SABEROPS_NIGHTLY_REPEAT=1 forces a run even when main is unchanged
#                          since the last green nightly (default 0 = skip).
#  SABEROPS_NIGHTLY_GATE_CMD
#                          bash snippet run in the nightly worktree instead of the
#                          real full gate; it must write $OUT_DIR/result/outcome.json
#                          (OUT_DIR is exported). Test-only stub hook.
#  SABEROPS_NIGHTLY_MAP_CMD
#                          bash snippet run via `gate_tools.py exclusive --` instead of
#                          the real map build; its output goes to map.txt. Test-only.
#  SABEROPS_NIGHTLY_SKIP_VENV=1 skips venv creation/refresh. Test-only.
#
# The script never pushes, never moves a branch, never edits .orch/ except
# nightly/latest.md, and never calls a provider.
set -euo pipefail

MAIN="${SABEROPS_NIGHTLY_MAIN:-$(git rev-parse --show-toplevel)}"
MAIN="$(cd -- "$MAIN" && pwd)"
COMMON_RAW="$(git -C "$MAIN" rev-parse --git-common-dir)"
if [[ "$COMMON_RAW" = /* ]]; then
    COMMON="$COMMON_RAW"
else
    COMMON="$MAIN/$COMMON_RAW"
fi
NIGHTLY_WT="${SABEROPS_NIGHTLY_WT:-/home/saber/Projects/SaberOps-wt/_nightly}"
ORCH_DIR="${SABEROPS_ORCH_DIR:-/home/saber/Projects/SaberOps-dogfood-0.3.7/.orch}"
CACHE_ROOT="${SABEROPS_NIGHTLY_CACHE:-$HOME/.cache/saberops-gate/nightly}"
REPEAT="${SABEROPS_NIGHTLY_REPEAT:-0}"
HISTORY_FILE="$COMMON/saberops-gate-history/full.jsonl"
DATE="$(date +%F)"

# 1. Nightly worktree (belongs to the tool; never MAIN's checkout or a lane worktree).
# An existing $NIGHTLY_WT is refreshed only after it passes every check below; a
# refusal exits 2 naming the failed check and touches nothing (no checkout, no
# worktree add, no output dir).
guard_nightly_worktree() {
    local target main_real list
    target="$(realpath -m -- "$NIGHTLY_WT")"
    main_real="$(realpath -m -- "$MAIN")"
    list="$(git -C "$MAIN" worktree list --porcelain)" || {
        echo "nightly-gate: refusing $NIGHTLY_WT: cannot list worktrees of $MAIN" >&2
        return 2
    }
    local line cur="" cur_real="" first="" attr="" listed=0
    while IFS= read -r line; do
        case "$line" in
            worktree\ *)
                cur="${line#worktree }"
                cur_real="$(realpath -m -- "$cur")"
                if [[ -z "$first" ]]; then
                    first="$cur_real"
                fi
                if [[ "$cur_real" == "$target" ]]; then
                    listed=1
                    attr=""
                fi
                ;;
            branch\ * | detached | bare)
                if [[ -n "$cur" && "$cur_real" == "$target" ]]; then
                    attr="$line"
                fi
                ;;
        esac
    done <<<"$list"
    if [[ "$listed" != "1" ]]; then
        echo "nightly-gate: refusing $NIGHTLY_WT: not a registered worktree of $MAIN" >&2
        return 2
    fi
    if [[ "$target" == "$main_real" || "$target" == "$first" ]]; then
        echo "nightly-gate: refusing $NIGHTLY_WT: it is MAIN's checkout" >&2
        return 2
    fi
    if [[ "$attr" != "detached" ]]; then
        echo "nightly-gate: refusing $NIGHTLY_WT: worktree is not detached (found: ${attr:-none})" >&2
        return 2
    fi
    if [[ "$(basename -- "$target")" != "_nightly" && "${SABEROPS_NIGHTLY_ALLOW_PATH:-0}" != "1" ]]; then
        echo "nightly-gate: refusing $NIGHTLY_WT: basename is not _nightly (set SABEROPS_NIGHTLY_ALLOW_PATH=1 to override)" >&2
        return 2
    fi
}
if [[ -d "$NIGHTLY_WT" ]]; then
    guard_nightly_worktree || exit 2
    git -C "$NIGHTLY_WT" checkout --detach --force main
else
    git -C "$MAIN" worktree add --detach "$NIGHTLY_WT" main
fi

# 2. Venv: create when missing, refresh when pyproject.toml changed.
if [[ "${SABEROPS_NIGHTLY_SKIP_VENV:-0}" != "1" ]]; then
    if [[ ! -x "$NIGHTLY_WT/.venv/bin/python" ]]; then
        if command -v uv >/dev/null 2>&1; then
            (cd -- "$NIGHTLY_WT" && uv venv -p 3.12 .venv && uv pip install -p .venv -e '.[dev]')
        else
            (cd -- "$NIGHTLY_WT" && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]')
        fi
    elif [[ "$NIGHTLY_WT/pyproject.toml" -nt "$NIGHTLY_WT/.venv/pyvenv.cfg" ]]; then
        if command -v uv >/dev/null 2>&1; then
            (cd -- "$NIGHTLY_WT" && uv pip install -p .venv -e '.[dev]')
        else
            (cd -- "$NIGHTLY_WT" && .venv/bin/pip install -e '.[dev]')
        fi
    fi
fi
PYBIN="$NIGHTLY_WT/.venv/bin/python"
if [[ ! -x "$PYBIN" ]]; then
    PYBIN="python3"
fi

MAIN_SHA="$(git -C "$MAIN" rev-parse main)"
SHORT_SHA="$(git -C "$NIGHTLY_WT" rev-parse --short HEAD)"
OUT_DIR="$CACHE_ROOT/$DATE-$SHORT_SHA"
mkdir -p "$OUT_DIR"
export OUT_DIR NIGHTLY_WT MAIN

# 3. Skip rule: unchanged main + last nightly PASS => skip unless REPEAT=1.
LAST_NIGHTLY="$(python3 - "$HISTORY_FILE" <<'EOF'
import json, sys
path = sys.argv[1]
try:
    text = open(path).read()
except OSError:
    print("")
    raise SystemExit
sha = ""
outcome = ""
for line in text.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        entry = json.loads(line)
    except ValueError:
        continue
    if isinstance(entry, dict) and entry.get("label") == "nightly":
        sha = str(entry.get("head_sha", ""))
        outcome = str(entry.get("outcome", ""))
if sha:
    print(f"{sha} {outcome}")
else:
    print("")
EOF
)"
LAST_SHA="${LAST_NIGHTLY%% *}"
LAST_OUTCOME="${LAST_NIGHTLY##* }"

write_summary() {
    # $1 outcome, $2 map_rebuilt(yes/no/skipped), $3 gate_exit
    local outcome="$1" map_rebuilt="$2" gate_exit="$3"
    local counts="n/a" wall="n/a" reason=""
    if [[ -f "$OUT_DIR/result/outcome.json" ]]; then
        local parsed
        parsed="$(python3 - "$OUT_DIR/result/outcome.json" <<'EOF'
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    print("n/a\tn/a\t")
else:
    counts = doc.get("counts", {})
    if not isinstance(counts, dict):
        counts = {}
    print("\t".join((
        f"{counts.get('tests', '?')} tests, "
        f"{counts.get('failed', '?')} failed, "
        f"{counts.get('flaky', '?')} flaky",
        str(doc.get("wall_time", "n/a")),
        str(doc.get("reason", "")),
    )))
EOF
)"
        IFS=$'\t' read -r counts wall reason <<<"$parsed"
    fi
    {
        echo "# Nightly gate $DATE"
        echo ""
        echo "- main: $MAIN_SHA"
        echo "- outcome: $outcome${reason:+ ($reason)}"
        echo "- counts: $counts"
        echo "- wall_time: $wall"
        echo "- gate exit: $gate_exit"
        echo "- map rebuilt: $map_rebuilt"
        echo ""
        echo "## quarantine proposals"
        if [[ -f "$OUT_DIR/flaky.md" ]]; then
            sed -n '/## quarantine proposals/,/## nightly failures/p' "$OUT_DIR/flaky.md" | sed '1d;$d'
        else
            echo "(no flaky report)"
        fi
        echo ""
        echo "## nightly failures"
        if [[ -f "$OUT_DIR/flaky.md" ]]; then
            sed -n '/## nightly failures/,$p' "$OUT_DIR/flaky.md" | sed '1d'
        else
            echo "(no flaky report)"
        fi
    } >"$OUT_DIR/summary.md"
    mkdir -p "$ORCH_DIR/nightly"
    cp "$OUT_DIR/summary.md" "$ORCH_DIR/nightly/latest.md"
}

if [[ -n "$LAST_SHA" && "$MAIN_SHA" == "$LAST_SHA" && "$LAST_OUTCOME" == "PASS" && "$REPEAT" != "1" ]]; then
    echo "main unchanged since last green nightly ($MAIN_SHA); skipping"
    {
        echo "# Nightly gate $DATE"
        echo ""
        echo "- main: $MAIN_SHA"
        echo "- outcome: SKIPPED (main unchanged since last green nightly)"
        echo "- map rebuilt: no"
    } >"$OUT_DIR/summary.md"
    mkdir -p "$ORCH_DIR/nightly"
    cp "$OUT_DIR/summary.md" "$ORCH_DIR/nightly/latest.md"
    exit 0
fi

# 4. Full gate (takes its own slots).
GATE_EXIT=0
if [[ -n "${SABEROPS_NIGHTLY_GATE_CMD:-}" ]]; then
    if (cd -- "$NIGHTLY_WT" && SABEROPS_GATE_LABEL=nightly bash -c "$SABEROPS_NIGHTLY_GATE_CMD" >"$OUT_DIR/gate.txt" 2>&1); then
        GATE_EXIT=0
    else
        GATE_EXIT=$?
    fi
else
    if (cd -- "$NIGHTLY_WT" && SABEROPS_GATE_LABEL=nightly bash scripts/gate-full.sh --result-dir "$OUT_DIR/result" >"$OUT_DIR/gate.txt" 2>&1); then
        GATE_EXIT=0
    else
        GATE_EXIT=$?
    fi
fi
OUTCOME="UNKNOWN"
if [[ -f "$OUT_DIR/result/outcome.json" ]]; then
    OUTCOME="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("outcome", "UNKNOWN"))' "$OUT_DIR/result/outcome.json")"
fi

# 5. Map rebuild when the gate is satisfied (PASS or quarantined-only FLAKY_PASS).
# FAIL, TIMEOUT and INFRA never rebuild the map.
MAP_REBUILT="no"
if [[ "$OUTCOME" == "PASS" || "$OUTCOME" == "FLAKY_PASS" ]]; then
    if [[ -n "${SABEROPS_NIGHTLY_MAP_CMD:-}" ]]; then
        (cd -- "$NIGHTLY_WT" && "$PYBIN" scripts/gate_tools.py exclusive -- bash -c "$SABEROPS_NIGHTLY_MAP_CMD" >"$OUT_DIR/map.txt" 2>&1) || true
    else
        (cd -- "$NIGHTLY_WT" && "$PYBIN" scripts/gate_tools.py exclusive -- .venv/bin/python scripts/build_test_map.py >"$OUT_DIR/map.txt" 2>&1) || true
    fi
    MAP_REBUILT="yes"
fi

# 6. Flaky report.
(cd -- "$NIGHTLY_WT" && "$PYBIN" scripts/gate_tools.py flaky-report >"$OUT_DIR/flaky.md" 2>&1) || true

# 7. Summary + latest.md.
write_summary "$OUTCOME" "$MAP_REBUILT" "$GATE_EXIT"

# 8. Retention: nightly output dirs older than 30 days, nothing else. A cache root
# that is empty, / or $HOME, or that has no `nightly` path component, is refused:
# warn, skip deletion, leave the exit status untouched.
if [[ -d "$CACHE_ROOT" ]]; then
    if [[ -z "$CACHE_ROOT" || "$CACHE_ROOT" == "/" || "$CACHE_ROOT" == "${HOME:-}" || "/$CACHE_ROOT/" != */nightly/* ]]; then
        echo "nightly-gate: refusing retention in $CACHE_ROOT (not a nightly cache dir); skipping deletion" >&2
    else
        find "$CACHE_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +30 -name '????-??-??-*' -exec rm -rf {} +
    fi
fi

exit "$GATE_EXIT"
