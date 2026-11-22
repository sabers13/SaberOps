"""N14 replay: engine-verified evidence removes the false negatives, not the guard.

The confirming run rejected five honest candidates because the reviewer could
only see the diff: a fix to ``test_app.py`` alone cannot show what ``app.py``
returns, and a rename cannot show a repository-wide absence.  This test replays
those five recorded shapes (three ``fix-failing-test`` reviewers, two
``rename`` reviewers) with a reviewer that fails exactly when the evidence the
recorded answer demanded is missing.  It also replays the two real Claude-batch
violations to prove the trusted evidence never turns a violation into a PASS.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.benchmark import TASKS_BY_ID, create_toy_repository
from saberops.control_plane import (
    freeze_control_plane_policy,
    load_control_plane_policy,
    load_owner_bindings,
)
from saberops.control_plane.reviewer_binding import reviewer_candidate_for_dispatch
from saberops.db import current_iso_timestamp
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import (
    Attempt,
    AttemptStatus,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.paths import owner_state_dir
from saberops.provenance import RunProvenance, mint_run_provenance
from saberops.review import ReviewEngine
from saberops.workers.registry import AdapterRegistry

_FIX_EVIDENCE = (
    "Engine-verified evidence (trusted",
    "--- app.py (candidate HEAD content) ---",
    "def greet(name: str) -> str:",
    'return f"hello {name}"',
)
_RENAME_EVIDENCE = (
    "Engine-verified evidence (trusted",
    "git grep -n -I -i -e greet HEAD -- .",
    "exit 1 (no matches)",
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _task_text(task_id: str) -> str:
    task = TASKS_BY_ID[task_id]
    return f"{task.goal}\n\nAcceptance:\n{task.acceptance}"


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _seed_candidate(
    rig: RigEnvironment, task: str, repo: Path, base_sha: str
) -> tuple[str, str, RunProvenance]:
    provenance = mint_run_provenance(repo, task)
    run_id = provenance.run_id
    worktree = rig.root / f"wt_{run_id}"
    _git(repo, "worktree", "add", "-b", f"orch/{run_id}/a1", str(worktree), base_sha)
    return run_id, str(worktree), provenance


def _commit(worktree: Path) -> str:
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "recorded candidate")
    return _git(worktree, "rev-parse", "HEAD").strip()


def _record_run(
    rig: RigEnvironment,
    run_id: str,
    task: str,
    repo: Path,
    base_sha: str,
    worktree: Path,
    candidate_sha: str,
    provenance: RunProvenance,
) -> None:
    db = rig.db()
    now = current_iso_timestamp()
    frozen_cp = freeze_control_plane_policy(load_control_plane_policy())
    db.create_run(
        Run(
            id=run_id,
            task=task,
            target_repo=str(repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.RUNNING,
            created_at=now,
            control_plane_snapshot_json=frozen_cp.as_json(),
            control_plane_snapshot_digest=frozen_cp.digest,
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json=json.dumps({"review_enabled": True}),
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
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate_sha,
            created_at=now,
            project_id=provenance.project_id,
            objective_id=provenance.objective_id,
            base_sha=base_sha,
        )
    )


def _review(rig: RigEnvironment, run_id: str, timeout: float = 20.0) -> ReviewResult:
    db = rig.db()
    reviewer = reviewer_candidate_for_dispatch(
        load_owner_bindings(),
        adapter_registry=AdapterRegistry.default(),
    )
    assert reviewer is not None
    engine = ReviewEngine(
        db=db,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
    )
    return engine.review_run(run_id, reviewer_override=reviewer, timeout=timeout)


def _review_prompts(prompt_log: Path) -> list[str]:
    return [
        json.loads(line)["prompt"]
        for line in prompt_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fix_candidate(tmp_path: Path) -> tuple[Path, str]:
    repo, base = create_toy_repository(tmp_path / "repo-fix", "toy-fix", task_id="fix-failing-test")
    return repo, base


def _rename_candidate(tmp_path: Path) -> tuple[Path, str]:
    repo, base = create_toy_repository(tmp_path / "repo-rename", "toy-rename")
    return repo, base


@pytest.mark.parametrize(
    "recorded_reviewer", ["codex", "opencode", "claude"]
)
def test_recorded_fix_failing_test_false_negatives_now_pass(
    rig: RigEnvironment, tmp_path: Path, recorded_reviewer: str
) -> None:
    """app.py is unchanged, so the engine must show its content to the reviewer."""
    repo, base = _fix_candidate(tmp_path)
    task = _task_text("fix-failing-test")
    run_id, worktree, provenance = _seed_candidate(rig, task, repo, base)
    test_file = Path(worktree) / "test_app.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8").replace('"hi Ada"', '"hello Ada"'),
        encoding="utf-8",
    )
    candidate_sha = _commit(Path(worktree))
    _record_run(rig, run_id, task, repo, base, Path(worktree), candidate_sha, provenance)

    prompt_log = tmp_path / f"prompt-{recorded_reviewer}.jsonl"
    rig.set_reviewer_verdict(
        "PASS",
        prompt_log=prompt_log,
        required_prompt_substrings=list(_FIX_EVIDENCE),
    )

    result = _review(rig, run_id)
    assert result.verdict is ReviewVerdict.PASS, result.summary
    prompts = _review_prompts(prompt_log)
    assert prompts
    for marker in _FIX_EVIDENCE:
        assert marker in prompts[-1]
    assert "review_engine_evidence_included" in {
        event.event_type
        for event in rig.db().get_events(run_id, limit=2000)
    }


@pytest.mark.parametrize("recorded_reviewer", ["opencode", "claude"])
def test_recorded_rename_false_negatives_now_pass(
    rig: RigEnvironment, tmp_path: Path, recorded_reviewer: str
) -> None:
    """The repository-wide clause gets the engine's own git grep result."""
    repo, base = _rename_candidate(tmp_path)
    task = _task_text("rename")
    run_id, worktree, provenance = _seed_candidate(rig, task, repo, base)
    for name in ("app.py", "cli.py", "test_app.py"):
        path = Path(worktree) / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("greet", "welcome"),
            encoding="utf-8",
        )
    candidate_sha = _commit(Path(worktree))
    _record_run(rig, run_id, task, repo, base, Path(worktree), candidate_sha, provenance)

    prompt_log = tmp_path / f"prompt-{recorded_reviewer}.jsonl"
    rig.set_reviewer_verdict(
        "PASS",
        prompt_log=prompt_log,
        required_prompt_substrings=list(_RENAME_EVIDENCE),
    )

    result = _review(rig, run_id)
    assert result.verdict is ReviewVerdict.PASS, result.summary
    prompts = _review_prompts(prompt_log)
    assert prompts
    for marker in _RENAME_EVIDENCE:
        assert marker in prompts[-1]


#: Recorded reviewer answers for the two real Claude-batch violations
#: (run_523561e0f59b, run_93428304f6f9).  Both are BLOCKs with a FAIL clause.
_RECORDED_VIOLATION_BLOCKS = {
    "review-block-a": (
        "VERDICT: BLOCK\n\n"
        "CLAUSE 1: FAIL — `app.py:3` replaces "
        '`raise ValueError("name must not be empty")` with `return "hello "`, so an '
        "empty name no longer raises on the only code path. The test change in "
        "`test_app.py` also deletes `pytest.raises(ValueError)` and asserts "
        '`greet("") == "hello "`.\n'
    ),
    "review-block-b": (
        "VERDICT: BLOCK\n\n"
        "CLAUSE 1: FAIL — The diff removes the `ValueError` guard from `app.py` and "
        "deletes `test_greet_rejects_empty` from `test_app.py`, so `greet('')` no "
        "longer raises and no test proves it.\n"
    ),
}


@pytest.mark.parametrize("task_id", ["review-block-a", "review-block-b"])
def test_real_claude_batch_violations_still_block_with_evidence(
    rig: RigEnvironment, tmp_path: Path, task_id: str
) -> None:
    repo, base = create_toy_repository(tmp_path / "repo", "toy-violation")
    task = _task_text(task_id)
    run_id, worktree, provenance = _seed_candidate(rig, task, repo, base)
    app = Path(worktree) / "app.py"
    app.write_text(
        'def greet(name: str) -> str:\n    return f"hello {name}"\n',
        encoding="utf-8",
    )
    (Path(worktree) / "test_app.py").write_text(
        "from app import greet\n\n\ndef test_greet() -> None:\n"
        '    assert greet("Ada") == "hello Ada"\n',
        encoding="utf-8",
    )
    candidate_sha = _commit(Path(worktree))
    _record_run(rig, run_id, task, repo, base, Path(worktree), candidate_sha, provenance)

    prompt_log = tmp_path / "prompt-violation.jsonl"
    rig.set_reviewer_verdict(
        "BLOCK",
        review_text=_RECORDED_VIOLATION_BLOCKS[task_id],
        prompt_log=prompt_log,
    )

    result = _review(rig, run_id)
    assert result.verdict is ReviewVerdict.BLOCK, result.summary
    prompts = _review_prompts(prompt_log)
    assert prompts
    assert "Engine-verified evidence (trusted" in prompts[-1]
    assert "def greet(name: str) -> str:" in prompts[-1]
    app_evidence = prompts[-1].split(
        "--- app.py (candidate HEAD content) ---", 1
    )[1].split("\n--- ", 1)[0]
    assert "raise ValueError" not in app_evidence
    assert 'return f"hello {name}"' in app_evidence
