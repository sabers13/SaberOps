#!/usr/bin/env python3
"""Capture the pre-1.1 store fixture used by the H36 upgrade check.

Run once on pre-1.1 ``main`` (captured from ``7384d36``), from the repository
root::

    PYTHONPATH=src:tests .venv/bin/python tests/fixtures/pre_1_1_store/capture.py

It builds an isolated, fake-mode owner store through the public functions an
owner uses, adds the pre-1.1 settings the upgrade check names in the plan
(orchestrator binding, authority preset, a routing file naming Cline, review
knobs), and copies the resulting files into this directory.

The *artifacts* are the fixture; this script is only the record of how they
were produced.  It imports rig helpers and phase-1.1 modules that later lanes
delete, so it is not expected to run after those deletions.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[2]

#: The pre-1.1 commit this capture was taken from.
CAPTURE_COMMIT = "7384d36"

#: The PLAN binding the fake-mode rig discovers for the codex account.
ORCHESTRATOR_BINDING = "discovered-binding:orchestrator:codex-chatgpt-account:gpt-5.6-terra"

#: A Cline model route this store still carries; H17 removes the adapter.
CLINE_ROUTE = "cline/cline-pass/glm-5.3-flash"

#: Old read-only view names the store's owner used (H9 keeps three of the nine).
OLD_VIEWS = (
    "events",
    "handoff",
    "plan",
    "observability",
    "orch-context",
    "monitor",
)

#: Old recovery verbs the owner used (H7 folds them into ``resume_job``).
OLD_RECOVERY_VERBS = (
    "retry",
    "continue",
    "try-worker",
    "refresh",
    "recover",
    "reconcile",
    "handoff",
)


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _placeholder(payload: object, rig: Any) -> object:
    """Replace capture-machine paths with tokens the restore helper rewrites."""
    tokens = {
        str(rig.root): "{root}",
        str(rig.repo): "{repo}",
        str(rig.state_home / "saberops"): "{state}",
        str(rig.config_home / "saberops"): "{config}",
        str(rig.home_dir): "{home}",
        sys.executable: "{python}",
    }
    if isinstance(payload, str):
        for value, token in tokens.items():
            payload = payload.replace(value, token)
        return payload
    if isinstance(payload, list):
        return [_placeholder(item, rig) for item in payload]
    if isinstance(payload, dict):
        return {key: _placeholder(value, rig) for key, value in payload.items()}
    return payload


def _copy_json(source: Path, target: Path, rig: Any) -> None:
    payload = _placeholder(json.loads(source.read_text(encoding="utf-8")), rig)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    sys.path.insert(0, str(REPO_ROOT / "src"))

    work = Path("/var/tmp/h36-pre-1-1-capture")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    import pytest  # noqa: PLC0415
    from lifecycle_rig import build_lifecycle_rig  # noqa: PLC0415

    monkeypatch = pytest.MonkeyPatch()
    rig = build_lifecycle_rig(work, monkeypatch)

    from saberops.authority import set_authority_mode  # type: ignore[import-not-found]  # noqa: PLC0415

    from saberops.db import Database  # noqa: PLC0415
    from saberops.routing_config import (  # noqa: PLC0415
        add_candidate_to_chain,
        load_effective_routing_payload,
        save_user_routing,
    )
    from saberops.service import OrchestratorService  # noqa: PLC0415

    # 1. The owner's selected orchestrator binding: PINNED over the exact PLAN
    #    binding identity the store discovered.  H3 (ADR-0064) removed this
    #    owner command; the call stays as the record of the pre-1.1 capture.
    OrchestratorService(db=Database()).set_orchestrator_binding(  # type: ignore[attr-defined]
        ORCHESTRATOR_BINDING, mode="pinned"
    )

    # 2. An authority preset (H22 folds the presets into the owner mode).
    set_authority_mode("autonomous")

    # 3. A routing file that still names Cline on the BUILD chain.
    payload = load_effective_routing_payload()
    payload = add_candidate_to_chain(payload, role="WORKER", candidate_id=CLINE_ROUTE)
    save_user_routing(payload)

    # 4. Old review knobs: a chosen review mode and round limit.  H4 removes
    #    the reader, so the pre-1.1 store shape is written directly here.
    from saberops.project import bind_project_state_dir, compute_project_identity
    from saberops.project_settings import project_settings_path

    state_dir = bind_project_state_dir(compute_project_identity(rig.repo))
    settings_path = project_settings_path(state_dir)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings.update({"review_disabled": False, "review_mode": "max", "review_limit": 5})
    settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (state_dir / "review-policy.json").write_text(
        json.dumps({"version": 1, "mode": "ALWAYS_REQUIRED"}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    persisted = json.loads(
        (rig.config_home / "saberops" / "bindings.json").read_text(encoding="utf-8")
    )
    assert persisted["orch_binding_policy"]["pinned_binding_id"] == ORCHESTRATOR_BINDING

    config_dir = rig.config_home / "saberops"
    state_dir = rig.state_home / "saberops"
    project_dirs = sorted((state_dir / "projects").iterdir())
    assert len(project_dirs) == 1, project_dirs
    project_dir = project_dirs[0]

    for name in ("authority.json", "bindings.json", "control_plane.json", "routing.json"):
        _copy_json(config_dir / name, FIXTURE_DIR / "config" / "saberops" / name, rig)
    for name in ("access-models.json", "access-readiness.json"):
        _copy_json(state_dir / name, FIXTURE_DIR / "state" / "saberops" / name, rig)
    for name in ("project-settings.json", "review-policy.json"):
        _copy_json(project_dir / name, FIXTURE_DIR / "project" / name, rig)

    # The database is copied through a vacuum so the committed file holds no
    # free pages from the capture's own writes.  Schema and rows are untouched.
    raw_db = FIXTURE_DIR / "state" / "saberops" / "orchestrator.db"
    raw_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(state_dir / "orchestrator.db", raw_db)
    connection = sqlite3.connect(raw_db)
    connection.execute("VACUUM")
    connection.close()

    routing_doc = json.loads((config_dir / "routing.json").read_text(encoding="utf-8"))
    assert any(
        "cline" in route
        for group in routing_doc["roles"].values()
        for entry in group
        for route in entry.get("routes", ())
    ), routing_doc

    manifest = {
        "schema_version": 1,
        "captured_from_commit": CAPTURE_COMMIT,
        "captured_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "fake_mode": True,
        "source": "tests/lifecycle_rig.build_lifecycle_rig on an isolated store",
        "contains": {
            "orchestrator_binding": ORCHESTRATOR_BINDING,
            "authority_preset": "autonomous",
            "cline_route": CLINE_ROUTE,
            "review": {"mode": "always-required", "rounds": "max x5", "limit": 5},
        },
        "restore_notes": [
            "Paths inside the store are rewritten by tests/pre_1_1_store.py.",
            "job history is empty: the upgrade check runs the first job itself.",
        ],
    }
    (FIXTURE_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    surfaces = {
        "schema_version": 1,
        "old_views": list(OLD_VIEWS),
        "old_recovery_verbs": list(OLD_RECOVERY_VERBS),
        "kept_views": ["status", "watch", "report"],
    }
    (FIXTURE_DIR / "surfaces.json").write_text(
        json.dumps(surfaces, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    monkeypatch.undo()
    print(f"captured into {FIXTURE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
