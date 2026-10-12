#!/usr/bin/env bash
# Main gate: the full gate on main; deterministic, no model, idempotent.
#
# What it does (see docs/operations.md "Main gate"):
#  1. verifies the main-gate worktree is the tool's own detached `_main-gate`
#     worktree (refuses anything else with exit 2) and refreshes it at main,
#  2. ensures its venv is current,
#  3. skips only when main is unchanged since the last green main gate
#     (unless SABEROPS_MAIN_GATE_REPEAT=1),
#  4. runs the full gate with SABEROPS_GATE_LABEL=main-gate,
#  5. rebuilds the coverage map when the gate is satisfied (PASS or
#     quarantined-only FLAKY_PASS; never on FAIL, TIMEOUT or INFRA),
#  6. writes flaky-report, summary.md and $SABEROPS_ORCH_DIR/main-gate/latest.md,
#  7. deletes main-gate output dirs older than 30 days (only inside a `main-gate`
#     cache dir; anything else is refused and skipped).
#
# Environment overrides (defaults are the production paths):
#  SABEROPS_MAIN_GATE_REPO  repository to gate (default: git toplevel of cwd).
#  SABEROPS_MAIN_GATE_WT    main-gate worktree (default /home/saber/Projects/SaberOps-wt/_main-gate).
#                          An existing directory is used only when it is a detached
#                          worktree of this repository whose basename is `_main-gate`;
#                          anything else is refused with exit 2 (see step 1).
#  SABEROPS_MAIN_GATE_ALLOW_PATH=1
#                          permits an existing detached worktree with a basename other
#                          than `_main-gate`. Test-only escape hatch.
#  SABEROPS_ORCH_DIR       .orch directory for main-gate/latest.md
#                          (default /home/saber/Projects/SaberOps-dogfood-0.3.7/.orch).
#  SABEROPS_MAIN_GATE_CACHE  main-gate output root
#                          (default $HOME/.cache/saberops-gate/main-gate). Test-only.
#  SABEROPS_MAIN_GATE_REPEAT=1 forces a run even when main is unchanged
#                          since the last green main gate (default 0 = skip).
#  SABEROPS_MAIN_GATE_GATE_CMD
#                          bash snippet run in the main-gate worktree instead of the
#                          real full gate; it must write $OUT_DIR/result/outcome.json
#                          (OUT_DIR is exported). Test-only stub hook.
#  SABEROPS_MAIN_GATE_MAP_CMD
#                          bash snippet run via `gate_tools.py exclusive --` instead of
#                          the real map build; its output goes to map.txt. Test-only.
#  SABEROPS_MAIN_GATE_SKIP_VENV=1 skips venv creation/refresh. Test-only.
#
# The script never pushes, never moves a branch, never edits .orch/ except
# main-gate/latest.md, and never calls a provider.
set -euo pipefail

MAIN="${SABEROPS_MAIN_GATE_REPO:-$(git rev-parse --show-toplevel)}"
MAIN="$(cd -- "$MAIN" && pwd)"
COMMON_RAW="$(git -C "$MAIN" rev-parse --git-common-dir)"
if [[ "$COMMON_RAW" = /* ]]; then
    COMMON="$COMMON_RAW"
else
    COMMON="$MAIN/$COMMON_RAW"
fi
MAIN_GATE_WT="${SABEROPS_MAIN_GATE_WT:-/home/saber/Projects/SaberOps-wt/_main-gate}"
ORCH_DIR="${SABEROPS_ORCH_DIR:-/home/saber/Projects/SaberOps-dogfood-0.3.7/.orch}"
CACHE_ROOT="${SABEROPS_MAIN_GATE_CACHE:-$HOME/.cache/saberops-gate/main-gate}"
REPEAT="${SABEROPS_MAIN_GATE_REPEAT:-0}"
HISTORY_FILE="$COMMON/saberops-gate-history/full.jsonl"
DATE="$(date +%F)"

# 1. Main-gate worktree (belongs to the tool; never MAIN's checkout or a lane worktree).
# An existing $MAIN_GATE_WT is refreshed only after it passes every check below; a
# refusal exits 2 naming the failed check and touches nothing (no checkout, no
# worktree add, no output dir).
guard_main_gate_worktree() {
    local target main_real list
    target="$(realpath -m -- "$MAIN_GATE_WT")"
    main_real="$(realpath -m -- "$MAIN")"
    list="$(git -C "$MAIN" worktree list --porcelain)" || {
        echo "main-gate: refusing $MAIN_GATE_WT: cannot list worktrees of $MAIN" >&2
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
        echo "main-gate: refusing $MAIN_GATE_WT: not a registered worktree of $MAIN" >&2
        return 2
    fi
    if [[ "$target" == "$main_real" || "$target" == "$first" ]]; then
        echo "main-gate: refusing $MAIN_GATE_WT: it is MAIN's checkout" >&2
        return 2
    fi
    if [[ "$attr" != "detached" ]]; then
        echo "main-gate: refusing $MAIN_GATE_WT: worktree is not detached (found: ${attr:-none})" >&2
        return 2
    fi
    if [[ "$(basename -- "$target")" != "_main-gate" && "${SABEROPS_MAIN_GATE_ALLOW_PATH:-0}" != "1" ]]; then
        echo "main-gate: refusing $MAIN_GATE_WT: basename is not _main-gate (set SABEROPS_MAIN_GATE_ALLOW_PATH=1 to override)" >&2
        return 2
    fi
}
if [[ -d "$MAIN_GATE_WT" ]]; then
    guard_main_gate_worktree || exit 2
    git -C "$MAIN_GATE_WT" checkout --detach --force main
else
    git -C "$MAIN" worktree add --detach "$MAIN_GATE_WT" main
fi

# 2. Venv: create when missing, refresh when pyproject.toml changed.
if [[ "${SABEROPS_MAIN_GATE_SKIP_VENV:-0}" != "1" ]]; then
    if [[ ! -x "$MAIN_GATE_WT/.venv/bin/python" ]]; then
        if command -v uv >/dev/null 2>&1; then
            (cd -- "$MAIN_GATE_WT" && uv venv -p 3.12 .venv && uv pip install -p .venv -e '.[dev]')
        else
            (cd -- "$MAIN_GATE_WT" && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]')
        fi
    elif [[ "$MAIN_GATE_WT/pyproject.toml" -nt "$MAIN_GATE_WT/.venv/pyvenv.cfg" ]]; then
        if command -v uv >/dev/null 2>&1; then
            (cd -- "$MAIN_GATE_WT" && uv pip install -p .venv -e '.[dev]')
        else
            (cd -- "$MAIN_GATE_WT" && .venv/bin/pip install -e '.[dev]')
        fi
    fi
fi
PYBIN="$MAIN_GATE_WT/.venv/bin/python"
if [[ ! -x "$PYBIN" ]]; then
    PYBIN="python3"
fi

MAIN_SHA="$(git -C "$MAIN" rev-parse main)"
SHORT_SHA="$(git -C "$MAIN_GATE_WT" rev-parse --short HEAD)"
OUT_DIR="$CACHE_ROOT/$DATE-$SHORT_SHA"
mkdir -p "$OUT_DIR"
export OUT_DIR MAIN_GATE_WT MAIN

# 3. Skip rule: unchanged main + last main-gate PASS => skip unless REPEAT=1.
LAST_MAIN_GATE="$(python3 - "$HISTORY_FILE" <<'EOF'
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
    if isinstance(entry, dict) and entry.get("label") == "main-gate":
        sha = str(entry.get("head_sha", ""))
        outcome = str(entry.get("outcome", ""))
if sha:
    print(f"{sha} {outcome}")
else:
    print("")
EOF
)"
LAST_SHA="${LAST_MAIN_GATE%% *}"
LAST_OUTCOME="${LAST_MAIN_GATE##* }"

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
        echo "# Main gate $DATE"
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
            sed -n '/## quarantine proposals/,/## main-gate failures/p' "$OUT_DIR/flaky.md" | sed '1d;$d'
        else
            echo "(no flaky report)"
        fi
        echo ""
        echo "## main-gate failures"
        if [[ -f "$OUT_DIR/flaky.md" ]]; then
            sed -n '/## main-gate failures/,$p' "$OUT_DIR/flaky.md" | sed '1d'
        else
            echo "(no flaky report)"
        fi
    } >"$OUT_DIR/summary.md"
    mkdir -p "$ORCH_DIR/main-gate"
    cp "$OUT_DIR/summary.md" "$ORCH_DIR/main-gate/latest.md"
}

if [[ -n "$LAST_SHA" && "$MAIN_SHA" == "$LAST_SHA" && "$LAST_OUTCOME" == "PASS" && "$REPEAT" != "1" ]]; then
    echo "main unchanged since last green main gate ($MAIN_SHA); skipping"
    {
        echo "# Main gate $DATE"
        echo ""
        echo "- main: $MAIN_SHA"
        echo "- outcome: SKIPPED (main unchanged since last green main gate)"
        echo "- map rebuilt: no"
    } >"$OUT_DIR/summary.md"
    mkdir -p "$ORCH_DIR/main-gate"
    cp "$OUT_DIR/summary.md" "$ORCH_DIR/main-gate/latest.md"
    exit 0
fi

# 4. Full gate (takes its own slots).
GATE_EXIT=0
if [[ -n "${SABEROPS_MAIN_GATE_GATE_CMD:-}" ]]; then
    if (cd -- "$MAIN_GATE_WT" && SABEROPS_GATE_LABEL=main-gate bash -c "$SABEROPS_MAIN_GATE_GATE_CMD" >"$OUT_DIR/gate.txt" 2>&1); then
        GATE_EXIT=0
    else
        GATE_EXIT=$?
    fi
else
    if (cd -- "$MAIN_GATE_WT" && SABEROPS_GATE_LABEL=main-gate bash scripts/gate-full.sh --result-dir "$OUT_DIR/result" >"$OUT_DIR/gate.txt" 2>&1); then
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
    if [[ -n "${SABEROPS_MAIN_GATE_MAP_CMD:-}" ]]; then
        (cd -- "$MAIN_GATE_WT" && "$PYBIN" scripts/gate_tools.py exclusive -- bash -c "$SABEROPS_MAIN_GATE_MAP_CMD" >"$OUT_DIR/map.txt" 2>&1) || true
    else
        (cd -- "$MAIN_GATE_WT" && "$PYBIN" scripts/gate_tools.py exclusive -- .venv/bin/python scripts/build_test_map.py >"$OUT_DIR/map.txt" 2>&1) || true
    fi
    MAP_REBUILT="yes"
fi

# 6. Flaky report.
(cd -- "$MAIN_GATE_WT" && "$PYBIN" scripts/gate_tools.py flaky-report >"$OUT_DIR/flaky.md" 2>&1) || true

# 7. Summary + latest.md.
write_summary "$OUTCOME" "$MAP_REBUILT" "$GATE_EXIT"

# 8. Retention: main-gate output dirs older than 30 days, nothing else. A cache root
# that is empty, / or $HOME, or that has no `main-gate` path component, is refused:
# warn, skip deletion, leave the exit status untouched.
if [[ -d "$CACHE_ROOT" ]]; then
    if [[ -z "$CACHE_ROOT" || "$CACHE_ROOT" == "/" || "$CACHE_ROOT" == "${HOME:-}" || "/$CACHE_ROOT/" != */main-gate/* ]]; then
        echo "main-gate: refusing retention in $CACHE_ROOT (not a main-gate cache dir); skipping deletion" >&2
    else
        find "$CACHE_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +30 -name '????-??-??-*' -exec rm -rf {} +
    fi
fi

exit "$GATE_EXIT"
