"""H9: three CLI views; the six old read-only names stay as warned aliases.

``status`` carries the plan, the latest handoff and the recent events;
``report`` carries the canonical evidence, one bounded durable ref and the
live follow frames.  ``events``, ``handoff``, ``plan``, ``observability``,
``orch-context`` and ``monitor`` remain aliases for one release.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from saberops.cli import main as orch_main
from saberops.db import Database
from saberops.models import (
    Handoff,
    PlanStatus,
    PlanStep,
    PlanStepStatus,
    Run,
    RunPlan,
    RunStatus,
    Tier,
)

_CREATED = "2026-11-23T10:00:00+00:00"
_HANDOFF_TEXT = "resume: continue from step_h9"


def _seed(db: Database) -> None:
    db.create_run(
        Run(
            id="run_h9",
            task="three views",
            target_repo="/tmp/h9-target",
            base_commit="base-sha",
            tier=Tier.T1,
            training_allowed=False,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at=_CREATED,
            project_id="proj_h9",
        )
    )
    db.create_run_plan(
        RunPlan(
            id="plan_h9",
            run_id="run_h9",
            status=PlanStatus.ACTIVE,
            version=1,
            created_at=_CREATED,
            updated_at=_CREATED,
        )
    )
    db.create_plan_step(
        PlanStep(
            id="step_h9",
            plan_id="plan_h9",
            run_id="run_h9",
            kind="implement",
            title="wire the three views",
            goal="keep status, watch and report",
            status=PlanStepStatus.READY,
            ordering=1,
            created_at=_CREATED,
            updated_at=_CREATED,
        )
    )
    db.create_handoff(
        Handoff(
            id="handoff_h9",
            run_id="run_h9",
            kind="handoff_full",
            version=1,
            content=_HANDOFF_TEXT,
            created_at=_CREATED,
        )
    )
    db.record_event("run_h9", "worker_completed", payload={"exit_code": 0})


def _status(db_path: Path, capsys: Any) -> str:
    assert orch_main(["status", "run_h9", "--db", str(db_path)]) == 0
    return str(capsys.readouterr().out)


def test_status_carries_plan_handoff_and_events(tmp_path: Path, capsys: Any) -> None:
    db_path = tmp_path / "state.db"
    _seed(Database(db_path))

    out = _status(db_path, capsys)

    assert "Plan:" in out
    assert "plan_h9" in out
    assert "wire the three views" in out
    assert "Handoff:" in out
    assert _HANDOFF_TEXT in out
    assert "Recent events" in out
    assert "worker_completed" in out


def test_report_carries_context_and_follow(tmp_path: Path, capsys: Any) -> None:
    db_path = tmp_path / "state.db"
    _seed(Database(db_path))

    assert orch_main(["report", "run_h9", "--context", "--db", str(db_path)]) == 0
    refs = json.loads(capsys.readouterr().out)
    assert "continuation" in refs and "handoff" in refs and "gate" in refs

    assert orch_main(["report", "run_h9", "--context", "handoff", "--db", str(db_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run_h9"
    assert payload["ref"] == "handoff"

    assert orch_main(["report", "run_h9", "--context", "nope", "--db", str(db_path)]) == 1
    assert "unsupported context ref" in capsys.readouterr().err

    assert orch_main(["report", "run_h9", "--follow", "--once", "--db", str(db_path)]) == 0
    frame = capsys.readouterr().out
    assert "Run run_h9" in frame


def test_old_view_names_stay_aliases_with_a_notice(tmp_path: Path, capsys: Any) -> None:
    db_path = tmp_path / "state.db"
    _seed(Database(db_path))

    aliases = {
        "events": ["events", "run_h9", "--db", str(db_path)],
        "handoff": ["handoff", "run_h9", "--db", str(db_path)],
        "plan": ["plan", "run_h9", "--db", str(db_path)],
        "observability": ["observability", "run_h9", "--db", str(db_path)],
        "orch-context": ["orch-context", "run_h9", "--db", str(db_path)],
        "monitor": ["monitor", "run_h9", "--once", "--db", str(db_path)],
    }
    for name, argv in aliases.items():
        assert orch_main(argv) == 0, name
        captured = capsys.readouterr()
        assert f"'orch {name}' is a deprecated alias" in captured.err, name
        assert captured.out.strip(), name
