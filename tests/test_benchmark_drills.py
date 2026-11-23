"""H35 wave 0: the phase 1 drill catalogue and its recorded baseline.

These are fast checks over declarations, not the drills themselves: the drill
tests stay slow and run in the full gate.  They fail when a drill is added,
renamed or regrouped without updating the catalogue and the baseline document.
"""

from __future__ import annotations

import ast
from pathlib import Path

from benchmark_core import (
    DRILL_CATALOGUE,
    DRILL_LIMIT_SECONDS,
    DRILLS_BY_NAME,
    RECOVERY_DRILL_LIMIT_SECONDS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DOC = REPO_ROOT / "docs" / "benchmark-drill-baseline.md"
BASELINE_START = "<!-- drill-baseline:start -->"
BASELINE_END = "<!-- drill-baseline:end -->"


def _baseline_rows() -> list[list[str]]:
    text = BASELINE_DOC.read_text(encoding="utf-8")
    block = text.split(BASELINE_START, 1)[1].split(BASELINE_END, 1)[0]
    rows: list[list[str]] = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "drill" or set(cells[0]) <= {"-", ":"}:
            continue
        rows.append(cells)
    return rows


def test_drill_catalogue_inventory() -> None:
    """Five B2 failure drills, the recovery drill and 21 B2b drills."""
    assert len(DRILL_CATALOGUE) == 27
    assert sum(1 for entry in DRILL_CATALOGUE if entry.lane == "B2") == 6
    assert sum(1 for entry in DRILL_CATALOGUE if entry.lane == "B2b") == 21
    assert len(DRILLS_BY_NAME) == len(DRILL_CATALOGUE)
    assert all(DRILLS_BY_NAME[entry.name] is entry for entry in DRILL_CATALOGUE)


def test_drill_catalogue_entries_are_well_formed() -> None:
    names: set[str] = set()
    for entry in DRILL_CATALOGUE:
        assert entry.name and entry.name not in names, entry
        names.add(entry.name)
        assert entry.lane in {"B2", "B2b"}, entry
        assert entry.group, entry
        assert entry.slow is True, entry
        assert entry.limit_seconds in {
            DRILL_LIMIT_SECONDS,
            RECOVERY_DRILL_LIMIT_SECONDS,
        }, entry
        if entry.group == "B2/B3":
            assert entry.limit_seconds == RECOVERY_DRILL_LIMIT_SECONDS, entry
        else:
            assert entry.limit_seconds == DRILL_LIMIT_SECONDS, entry
        path = REPO_ROOT / entry.test.split("::")[0]
        function = entry.test.split("::")[1]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert function in defined, f"{entry.name}: {entry.test} does not exist"

    # A drill's code construct must still exist: the name literal is what the
    # rig passes to DrillResult.  Removing the code without removing the entry
    # fails here instead of leaving a table row no run produces.
    rig_source = "\n".join(
        (REPO_ROOT / "tests" / name).read_text(encoding="utf-8")
        for name in ("benchmark_rig.py", "benchmark_wave3.py")
    )
    for entry in DRILL_CATALOGUE:
        assert f'"{entry.name}"' in rig_source, f"{entry.name} has no producing code"


def test_baseline_document_lists_every_drill_with_the_catalogue_facts() -> None:
    rows = _baseline_rows()
    assert [row[0] for row in rows] == [entry.name for entry in DRILL_CATALOGUE]
    for row, entry in zip(rows, DRILL_CATALOGUE, strict=True):
        name, lane, group, slow, limit, baseline = row
        assert (name, lane, group) == (entry.name, entry.lane, entry.group), row
        assert slow == "yes", row
        assert float(limit) == entry.limit_seconds, row
        seconds = float(baseline)
        assert 0.0 <= seconds <= entry.limit_seconds, row
