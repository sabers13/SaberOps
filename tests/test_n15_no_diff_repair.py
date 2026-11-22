"""N15: a no-diff repair is structurally ALREADY_SATISFIED and gets one re-review.

Reproduces the confirming-run shape (ADR-0055): the candidate is complete, the
reviewer blocks asking for repository-wide proof, and the repair worker exits
successfully without editing anything.  The engine must trigger its one bounded
re-review from the structured outcome, never from the report's wording, and the
re-review must receive engine-verified evidence instead of a report labelled a
claim.  The six recorded report wordings that missed the old phrase list are
replayed, as is the formerly futile re-review.
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

#: The six no-diff repair reports that the old phrase list missed (report
#: §6, runs run_bd32f44a8fa8, run_ddd6fa66fd40, run_b93755dfc9ca,
#: run_2e08ed1da57b, run_a70e303c4e73, run_130ca0ed5e59).
_RECORDED_REPORTS = (
    (
        "codex_fix",
        "I made no changes and preserved candidate SHA. The inherited candidate "
        "already fixes the assertion.",
    ),
    (
        "opencode_fix",
        "No code change was needed — the inherited candidate is already correct "
        "and the test passes.",
    ),
    (
        "claude_fix",
        "The inherited candidate commit (2280b77) made the correct change; "
        "nothing further is required.",
    ),
    (
        "claude_rename",
        "The inherited candidate (e7956f7) is correct and complete; a full "
        "repository search found zero matches.",
    ),
    (
        "opencode_rename_a",
        "Code changes required: none. The rename is complete and no remaining "
        "reference exists.",
    ),
    (
        "opencode_rename_b",
        "No code change was required. The candidate already renames greet to "
        "welcome everywhere.",
    ),
)

_EVIDENCE_MARKERS = (
    "Engine-verified evidence (trusted",
    "git grep -n -I -i -e greet HEAD -- .",
    "Repository-wide search run by the engine",
)


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _prompts(prompt_log: Path) -> list[str]:
    return [
        json.loads(line)["prompt"]
        for line in prompt_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize(("label", "report"), _RECORDED_REPORTS)
def test_every_recorded_report_wordings_gets_the_bounded_re_review(
    rig: RigEnvironment, tmp_path: Path, label: str, report: str
) -> None:
    prompt_log = tmp_path / f"reviewer-prompts-{label}.jsonl"
    rig.set_worker_mode("commit", repair_mode="no_changes", report_text=report)
    rig.set_reviewer_verdict(
        "BLOCK",
        verdicts=["BLOCK", "PASS"],
        prompt_log=prompt_log,
        required_prompt_substrings=list(_EVIDENCE_MARKERS),
    )

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

    structured = [
        event
        for event in db.get_events(result.run_id, limit=2000)
        if event.event_type == "repair_no_diff_classified"
    ]
    assert len(structured) == 1
    assert (structured[0].payload or {})["outcome"] == "ALREADY_SATISFIED"

    events = [
        event
        for event in db.get_events(result.run_id, limit=2000)
        if event.event_type == "repair_no_diff_rereview"
    ]
    assert len(events) == 1
    payload = events[0].payload or {}
    assert payload["reason"] == "repair_no_diff_already_satisfied"
    assert payload["outcome"] == "ALREADY_SATISFIED"
    assert report[:40] in str(payload["repair_report_excerpt"])

    prompts = _prompts(prompt_log)
    assert len(prompts) == 2
    assert "Repair-round note (the repair worker's own report; a claim" in prompts[1]
    assert report[:40] in prompts[1]
    assert "CLAUSE 1: No reference to greet remains in the repository." in prompts[1]
    for marker in _EVIDENCE_MARKERS:
        assert marker in prompts[1]


def test_the_re_review_receives_engine_verified_evidence_because_the_report_is_a_claim(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    """The re-review passes only because the prompt carries engine evidence.

    The fake reviewer replays the recorded OpenCode block's stance: it returns a
    scripted BLOCK first, then a PASS only when the prompt contains the
    engine-run repository search.  The repair report alone cannot satisfy it.
    """
    prompt_log = tmp_path / "reviewer-prompts.jsonl"
    rig.set_worker_mode(
        "commit",
        repair_mode="no_changes",
        report_text=(
            "The repair-round scan is complete and no greet reference remains."
        ),
    )
    rig.set_reviewer_verdict(
        "BLOCK",
        verdicts=["BLOCK", "PASS"],
        prompt_log=prompt_log,
        required_prompt_substrings=list(_EVIDENCE_MARKERS),
    )

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
    prompts = _prompts(prompt_log)
    assert len(prompts) == 2
    assert "a claim, not engine verification" in prompts[1]
    assert "Repository-wide search run by the engine" in prompts[1]


def test_second_block_after_the_re_review_still_fails_closed(
    rig: RigEnvironment, tmp_path: Path
) -> None:
    """The re-review is bounded to one: a second BLOCK remains a terminal FAIL."""
    prompt_log = tmp_path / "reviewer-prompts.jsonl"
    rig.set_worker_mode(
        "commit",
        repair_mode="no_changes",
        report_text=_RECORDED_REPORTS[5][1],
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
