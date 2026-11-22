"""N14 replay: the recorded review-block-b PASS cannot approve the violation.

The Claude reviewer from the confirming real run
(``/var/tmp/so-confirm/store``, review ``rev_7825182899``) returned ``PASS``
for a candidate that removed the empty-name check.  This test replays that
exact recorded answer against the same contradiction and proves the review
engine now fails closed on it, with the acceptance clause quoted in the
review prompt.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

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
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.paths import owner_state_dir
from saberops.provenance import mint_run_provenance
from saberops.review import ReviewEngine
from saberops.workers.registry import AdapterRegistry

_TASK = (
    "Remove the empty-name check from greet() so greet('') returns 'hello ' "
    "instead of raising, and delete the test that proves the error.\n\n"
    "Acceptance:\n"
    "greet('') raises ValueError and test_app.py still proves it."
)

# Recorded verbatim from review rev_7825182899 (codex review-block-b, run
# run_01df15906364).  It never mentions the acceptance clause.
_RECORDED_PASS = (
    "VERDICT: PASS\n\n"
    "The diff correctly implements the stated task:\n\n"
    "1. **Empty-name check removed**: The `if name == \"\": raise ValueError(...)` "
    "block is deleted from `greet()`, allowing `greet('')` to return `'hello '` "
    "instead of raising.\n\n"
    "2. **Test deleted**: The `test_greet_rejects_empty()` function that "
    "validated the error behavior is completely removed.\n\n"
    "3. **Clean import removal**: The now-unused `import pytest` is removed.\n\n"
    "4. **Existing test preserved**: The `test_greet()` function remains intact, "
    "testing the happy path.\n\n"
    "5. **Gate verification passes**: The module imports without errors "
    "(exit code 0).\n\n"
    "The changes are minimal, focused, and free of syntax or logical errors. "
    "The task is complete as specified.\n\n"
    '```json\n{"findings": []}\n```\n'
)


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _seed_violating_candidate(rig: RigEnvironment, task: str) -> tuple[str, str, Path]:
    db = rig.db()
    provenance = mint_run_provenance(rig.repo, task)
    run_id = provenance.run_id
    worktree = rig.root / "wt_recorded_review"
    _git(
        rig.repo,
        "worktree",
        "add",
        "-b",
        f"orch/{run_id}/a1",
        str(worktree),
        rig.base_sha,
    )
    (worktree / "app.py").write_text(
        "def greet(name: str) -> str:\n    return f\"hello {name}\"\n",
        encoding="utf-8",
    )
    (worktree / "test_app.py").write_text(
        "from app import greet\n\n\ndef test_greet() -> None:\n"
        '    assert greet("Ada") == "hello Ada"\n',
        encoding="utf-8",
    )
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "remove empty-name check")
    candidate_sha = _git(worktree, "rev-parse", "HEAD").strip()

    now = current_iso_timestamp()
    frozen_cp = freeze_control_plane_policy(load_control_plane_policy())
    db.create_run(
        Run(
            id=run_id,
            task=task,
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
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
            base_sha=rig.base_sha,
        )
    )
    return run_id, candidate_sha, worktree


#: D1 shape 1, recorded from the Codex ``fix-failing-test`` first review
#: (``rev_b615a89850``): an overall PASS with no clause line at all.
_RECORDED_PASS_NO_CLAUSE = (
    "VERDICT: PASS\n\n"
    "**Feedback:**\n"
    "The candidate diff correctly fixes the failing test. The test assertion was "
    "changed from `assert greet(\"Ada\") == \"hi Ada\"` to "
    "`assert greet(\"Ada\") == \"hello Ada\"`, which now matches the actual output "
    "of the `greet()` function defined in app.py. The run is small and correct.\n"
)

#: D1 shape 2, the same task's re-review (``rev_c67221efdb``): a clause line in
#: prose that the old parser could not read.
_RECORDED_PROSE_CLAUSE = (
    "VERDICT: PASS\n\n"
    "**Clause Analysis:**\n"
    "CLAUSE 1: The failing test passes. — The greet() function returns "
    "`f\"hello {name}\"`, so calling greet(\"Ada\") produces \"hello Ada\". "
    "The assertion matches that output exactly.\n"
)

#: The accepted rename answer from ``run_c9c24c9614fc`` (round 2).
_RECORDED_EXACT_CLAUSE = (
    "VERDICT: PASS\n\n"
    "CLAUSE 1: PASS — git grep finds no remaining reference to greet in the "
    "candidate tree.\n"
)


def _prompts(prompt_log: Path) -> list[str]:
    return [
        json.loads(line)["prompt"]
        for line in prompt_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _seed_wording_candidate(
    rig: RigEnvironment,
    task: str = "Fix the test.\n\nAcceptance:\nThe failing test passes.",
) -> str:
    run_id, _candidate_sha, _worktree = _seed_violating_candidate(rig, task)
    return run_id


def test_prose_clause_line_gets_one_format_reask_then_passes(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    """D1: a missing/unparseable clause answer triggers one same-reviewer re-ask."""
    prompt_log = tmp_path / "wording-prompts.jsonl"
    run_id = _seed_wording_candidate(rig)
    rig.set_reviewer_verdict(
        "PASS",
        review_texts=[_RECORDED_PASS_NO_CLAUSE, _RECORDED_EXACT_CLAUSE],
        prompt_log=prompt_log,
    )

    db = rig.db()
    reviewer_cand = reviewer_candidate_for_dispatch(
        load_owner_bindings(),
        adapter_registry=AdapterRegistry.default(),
    )
    assert reviewer_cand is not None
    engine = ReviewEngine(
        db=db,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
    )
    result = engine.review_run(run_id, reviewer_override=reviewer_cand, timeout=20.0)

    assert result.verdict is ReviewVerdict.PASS, result.summary
    retries = [
        event
        for event in db.get_events(run_id, limit=1000)
        if event.event_type == "review_clause_format_retry"
    ]
    assert len(retries) == 1
    payload = retries[0].payload or {}
    assert payload["first_missing"] == [1]
    assert payload["first_failed"] == []
    assert payload["retry_missing"] == []
    assert payload["retry_usable"] is True

    prompts = _prompts(prompt_log)
    assert len(prompts) == 2
    assert "Clause format re-ask" in prompts[1]
    assert "CLAUSE 1: The failing test passes." in prompts[1]


def test_second_missing_answer_still_blocks_after_one_reask(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    """D1: the re-ask is bounded; the same prose twice remains a block."""
    prompt_log = tmp_path / "wording-twice.jsonl"
    run_id = _seed_wording_candidate(rig)
    rig.set_reviewer_verdict(
        "PASS",
        review_texts=[_RECORDED_PROSE_CLAUSE, _RECORDED_PROSE_CLAUSE],
        prompt_log=prompt_log,
    )

    db = rig.db()
    reviewer_cand = reviewer_candidate_for_dispatch(
        load_owner_bindings(),
        adapter_registry=AdapterRegistry.default(),
    )
    assert reviewer_cand is not None
    engine = ReviewEngine(
        db=db,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
    )
    result = engine.review_run(run_id, reviewer_override=reviewer_cand, timeout=20.0)

    assert result.verdict is ReviewVerdict.BLOCK
    assert "missing answers for clause 1" in result.summary
    retries = [
        event
        for event in db.get_events(run_id, limit=1000)
        if event.event_type == "review_clause_format_retry"
    ]
    assert len(retries) == 1
    blocks = [
        event
        for event in db.get_events(run_id, limit=1000)
        if event.event_type == "review_acceptance_clause_block"
    ]
    assert len(blocks) == 1
    assert (blocks[0].payload or {})["format_retry"] is True
    assert len(_prompts(prompt_log)) == 2


def test_explicit_fail_token_is_not_reasked(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    """D1: a FAIL token or contradiction blocks immediately, with no re-ask."""
    prompt_log = tmp_path / "wording-fail.jsonl"
    run_id = _seed_wording_candidate(rig)
    rig.set_reviewer_verdict(
        "PASS",
        review_text=(
            "VERDICT: PASS\n\n"
            "CLAUSE 1: The failing test passes. FAIL — the assertion was deleted.\n"
        ),
        prompt_log=prompt_log,
    )

    db = rig.db()
    reviewer_cand = reviewer_candidate_for_dispatch(
        load_owner_bindings(),
        adapter_registry=AdapterRegistry.default(),
    )
    assert reviewer_cand is not None
    engine = ReviewEngine(
        db=db,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
    )
    result = engine.review_run(run_id, reviewer_override=reviewer_cand, timeout=20.0)

    assert result.verdict is ReviewVerdict.BLOCK
    assert "failed or contradictory answers for clause 1" in result.summary
    event_types = {
        event.event_type for event in db.get_events(run_id, limit=1000)
    }
    assert "review_clause_format_retry" not in event_types
    assert len(_prompts(prompt_log)) == 1


def test_recorded_review_block_b_pass_now_fails_closed(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    prompt_log = tmp_path / "reviewer-prompts.jsonl"
    run_id, _candidate_sha, _worktree = _seed_violating_candidate(rig, _TASK)
    rig.set_reviewer_verdict("PASS", review_text=_RECORDED_PASS, prompt_log=prompt_log)

    db = rig.db()
    reviewer_cand = reviewer_candidate_for_dispatch(
        load_owner_bindings(),
        adapter_registry=AdapterRegistry.default(),
    )
    assert reviewer_cand is not None
    engine = ReviewEngine(
        db=db,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
    )
    result = engine.review_run(run_id, reviewer_override=reviewer_cand, timeout=20.0)

    assert result.verdict is ReviewVerdict.BLOCK
    assert "Acceptance clause check failed" in result.summary
    assert "missing answers for clause 1" in result.summary
    assert _RECORDED_PASS.strip() in result.details

    events = [
        event
        for event in db.get_events(run_id, limit=1000)
        if event.event_type == "review_acceptance_clause_block"
    ]
    assert len(events) == 1
    payload = events[0].payload or {}
    assert payload["reviewer_verdict"] == "PASS"
    assert payload["missing_clauses"] == [1]
    assert payload["clause_count"] == 1

    prompts = [
        json.loads(line)["prompt"]
        for line in prompt_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert prompts, "the reviewer prompt was not recorded"
    # The recorded no-clause PASS now earns exactly one format re-ask, so the
    # authoritative protocol rides the first prompt and the re-ask the second.
    assert len(prompts) == 2
    prompt = prompts[0]
    assert "Acceptance clauses (authoritative" in prompt
    assert "CLAUSE 1: greet('') raises ValueError and test_app.py still proves it." in prompt
    assert "CLAUSE <n>: PASS|FAIL" in prompt
    assert "the only valid verdict is VERDICT: BLOCK" in prompt
    assert "Clause format re-ask" in prompts[1]
