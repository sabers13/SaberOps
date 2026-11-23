"""H32: gate protection on the real acceptance path.

The candidate weakens ``conftest.py`` and the acceptance gate must refuse
before it runs, roll the target back to the recorded base, and surface the
owner question.  Fake CLIs only; no vendor or model call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from lifecycle_rig import build_lifecycle_rig

from saberops.accept import AcceptEngine
from saberops.db import current_iso_timestamp
from saberops.git import GitManager
from saberops.models import Attempt, AttemptStatus, Run, RunStatus, Tier
from saberops.provenance import mint_run_provenance


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def test_acceptance_blocks_a_tampered_conftest_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    repo = rig.repo
    base = rig.base_sha
    marker = tmp_path / "conftest-ran"

    task = "Add a conftest"
    provenance = mint_run_provenance(repo, task)
    run_id = provenance.run_id
    branch = f"orch/{run_id}/a1"
    worktree = tmp_path / "candidate"
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    (worktree / "conftest.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "import pytest\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "conftest.py")
    _git(worktree, "commit", "-m", "worker weakens conftest.py")
    candidate = _git(worktree, "rev-parse", "HEAD")

    now = current_iso_timestamp()
    db = rig.db()
    db.create_run(
        Run(
            id=run_id,
            task=task,
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=f"{sys.executable} -c 'import conftest'",
            status=RunStatus.COMPLETED,
            created_at=now,
            completed_at=now,
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json=json.dumps({"review_disabled": True}),
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}_a1",
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="gpt-5.6-terra",
            worktree_path=str(worktree),
            branch_name=branch,
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=now,
            completed_at=now,
            project_id=provenance.project_id,
            objective_id=provenance.objective_id,
            base_sha=base,
        )
    )

    with pytest.raises(RuntimeError) as excinfo:
        AcceptEngine(db=db).accept_run(run_id)

    message = str(excinfo.value)
    assert "GATE_PROTECTION_BLOCKED" in message or "GATE_PROTECTED_PATH_MUTATED" in message
    assert "Owner decision required" in message
    # The candidate's own conftest never ran; the target is back at its base.
    assert marker.exists() is False
    assert GitManager.get_head_commit(repo) == base
    assert GitManager.is_clean(repo)
