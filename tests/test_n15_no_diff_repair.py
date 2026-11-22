"""N15: a no-diff repair that claims satisfaction gets one bounded re-review.

Reproduces run_1617597fd2c5's shape: the candidate is complete, the reviewer
blocks asking for repository-wide proof, and the repair worker exits
successfully without editing anything while reporting that the finding is
already satisfied.  The engine must re-review the inherited candidate with the
repair report as evidence instead of stalling, exactly once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.models import AttemptStatus, ReviewVerdict, RunStatus

_TASK = (
    "Rename greet to welcome everywhere in app.py and its callers.\n\n"
    "Acceptance:\n"
    "No reference to greet remains in the repository."
)
_REPAIR_REPORT = (
    "No code change was required. The candidate already renames greet to "
    "welcome everywhere, and a full repository search now confirms there are "
    "no remaining greet references."
)


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def test_no_diff_repair_gets_one_review_instead_of_stalling(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    prompt_log = tmp_path / "reviewer-prompts.jsonl"
    rig.set_worker_mode(
        "commit",
        repair_mode="no_changes",
        report_text=_REPAIR_REPORT,
    )
    rig.set_reviewer_verdict("BLOCK", verdicts=["BLOCK", "PASS"], prompt_log=prompt_log)

    result = rig.service().run_sync(
        task=_TASK,
        repo_path=rig.repo,
        gate_command=rig.gate_command,
        provider_override="codex",
        model_override="gpt-5.6-terra",
        review=True,
        review_timeout=20.0,
    )

    assert result.status is RunStatus.COMPLETED, result.summary
    db = rig.db()
    attempts = db.get_attempts_for_run(result.run_id)
    assert [attempt.status for attempt in attempts] == [
        AttemptStatus.SUCCESS,
        AttemptStatus.FAILED,
    ]
    assert attempts[1].worker_error is not None
    assert "no_changes" in attempts[1].worker_error

    reviews = db.get_reviews_for_run(result.run_id)
    assert [review.verdict for review in reviews] == [
        ReviewVerdict.BLOCK,
        ReviewVerdict.PASS,
    ]

    events = [
        event
        for event in db.get_events(result.run_id, limit=2000)
        if event.event_type == "repair_no_diff_rereview"
    ]
    assert len(events) == 1
    payload = events[0].payload or {}
    assert payload["reason"] == "repair_no_diff_claims_satisfied"
    assert "No code change was required" in str(payload["repair_report_excerpt"])

    prompts = [
        json.loads(line)["prompt"]
        for line in prompt_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(prompts) == 2
    assert "Repair-round evidence (a claim to verify against the diff, not proof)" in prompts[1]
    assert "No code change was required" in prompts[1]
    assert "CLAUSE 1: No reference to greet remains in the repository." in prompts[1]


def test_second_block_after_the_re_review_still_fails_closed(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    """The re-review is bounded to one: a second BLOCK remains a terminal FAIL."""
    prompt_log = tmp_path / "reviewer-prompts.jsonl"
    rig.set_worker_mode(
        "commit",
        repair_mode="no_changes",
        report_text=_REPAIR_REPORT,
    )
    rig.set_reviewer_verdict("BLOCK", verdicts=["BLOCK", "BLOCK"], prompt_log=prompt_log)

    result = rig.service().run_sync(
        task=_TASK,
        repo_path=rig.repo,
        gate_command=rig.gate_command,
        provider_override="codex",
        model_override="gpt-5.6-terra",
        review=True,
        review_timeout=20.0,
    )

    assert result.status is RunStatus.FAILED
    db = rig.db()
    reviews = db.get_reviews_for_run(result.run_id)
    assert [review.verdict for review in reviews] == [
        ReviewVerdict.BLOCK,
        ReviewVerdict.BLOCK,
    ]
    state = db.get_review_loop_state(result.run_id)
    assert state is not None
    assert state["terminal_verdict"] == ReviewVerdict.CONVERGENCE_STALLED.value
    stalled = [
        event
        for event in db.get_events(result.run_id, limit=2000)
        if event.event_type == "convergence_stalled"
    ]
    assert any(
        (event.payload or {}).get("reason") == "repair_no_diff_rereview_blocked"
        for event in stalled
    )
