"""F11: away mode wires the unattended policy into the run path.

Hermetic: isolated XDG dirs, a temp SQLite owner store, a fake launcher and a
toy repository path.  No vendor CLI, model, network or desktop notice is used.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_loop import _make_candidate, _make_job, _make_policy, _make_routes

from saberops import away
from saberops.away import (
    AuthorityMode,
    Decision,
    DecisionDisposition,
    DecisionScope,
    NeverDelegableKind,
    StandingDecision,
    resolve_decision,
)
from saberops.capacity.ledger import record_owner_quota
from saberops.capacity.loop import acquire_lease, scheduler_tick
from saberops.capacity.store import ensure_tables as ensure_capacity_tables
from saberops.db import Database
from saberops.jobs import store
from saberops.jobs.models import JobState, WaitingReason
from saberops.mcp.tools import ServerContext, _catch_up
from saberops.models import RunStatus
from saberops.owner_mode import OwnerMode, set_owner_mode
from saberops.questions import (
    AnswerAuthority,
    AskRole,
    DecisionClass,
    Escalation,
    OwnerQuestionQueue,
    QuestionRequest,
    QuestionStatus,
)

pytestmark = pytest.mark.packaged_routing_default

_NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)
_FUTURE = "2999-01-01T00:00:00+00:00"
_PAST = "2026-01-01T00:00:00+00:00"


def _escalation() -> Escalation:
    return Escalation(
        requirement="the owner's answer is required",
        proposed_expansion="do the wider thing",
        smallest_compliant="do the narrow thing",
        consequences="work stays parked",
        recommendation="do the narrow thing",
    )


def _ask(
    queue: OwnerQuestionQueue,
    *,
    key: str,
    decision_class: DecisionClass,
    blocks: tuple[str, ...],
) -> None:
    queue.ask(
        QuestionRequest(
            key=key,
            subject=f"question {key}",
            kind="owner-decision",
            decision_class=decision_class,
            raised_by_role=AskRole.PLAN,
            raised_by="planner",
            escalation=_escalation(),
            blocks=blocks,
        )
    )


def _policy(set_at: str | None = None, *, wall_clock: int = 3_600) -> away.AwayPolicy:
    return away.AwayPolicy(
        budget_units=100,
        wall_clock_seconds=wall_clock,
        worker_cap=5,
        paths=("src",),
        set_at=set_at or _NOW.isoformat(),
        set_by="owner",
    )


@pytest.fixture()
def owner_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """One isolated owner state/config root plus explicit away store paths."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (home, state, config, cache):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "0")
    for name in ("SABEROPS_PROFILE", "ORCH_DIR", "SABEROPS_DIR"):
        monkeypatch.delenv(name, raising=False)
    state_dir = state / "saberops"
    state_dir.mkdir(parents=True)
    paths = {
        "state": state_dir,
        "store": state_dir / "unattended-decisions.json",
        "journal": state_dir / "away-journal.jsonl",
        "policy": state_dir / "away-policy.json",
        "questions": state_dir / "questions.json",
    }
    monkeypatch.setenv("ORCH_UNATTENDED_STORE", str(paths["store"]))
    monkeypatch.setenv("ORCH_AWAY_JOURNAL", str(paths["journal"]))
    monkeypatch.setenv("ORCH_AWAY_POLICY", str(paths["policy"]))
    monkeypatch.setenv("ORCH_QUESTIONS_STORE", str(paths["questions"]))
    return paths


def test_exact_scope_rejects_analogy_prefix_and_wildcard(owner_env: dict[str, Path]) -> None:
    decision_store = away.StandingDecisionStore()
    project = away.project_scope_id("/work/repo")
    decision_store.add(
        StandingDecision(
            standing_decision_id="sd-exact",
            scope=DecisionScope(project_id=project, task_id="job_a", paths=("src",)),
            decision_key="choose-fixup",
            owner_words="take the smallest fix",
            answered_by="owner",
            expires_at=_FUTURE,
        )
    )
    sheet = decision_store.sheet("job_a")
    exact = sheet.find(
        "choose-fixup", DecisionScope(project_id=project, task_id="job_a", paths=("src",))
    )
    assert exact is not None and exact.owner_words == "take the smallest fix"

    # Analogy, prefix and wildcard are all different scopes, never matches.
    assert (
        sheet.find(
            "choose-fixup",
            DecisionScope(project_id=project, task_id="job_a", paths=("src/a.py",)),
        )
        is None
    )
    assert (
        sheet.find("choose-fixup", DecisionScope(project_id=project, task_id="job_a2"))
        is None
    )
    assert (
        sheet.find("choose-fixup", DecisionScope(project_id=project, task_id="job_a"))
        is None
    )
    with pytest.raises(ValueError):
        DecisionScope(project_id=project, task_id="job_*", paths=())
    with pytest.raises(ValueError):
        DecisionScope(project_id=project, task_id="job_a", paths=("src/*.py",))
    with pytest.raises(ValueError):
        # Two ids that could only ever match by pattern never become wildcards.
        DecisionScope(project_id=project, task_id="job_[ab]", paths=())


def test_unbounded_or_non_owner_decision_is_refused(owner_env: dict[str, Path]) -> None:
    project = away.project_scope_id("/work/repo")
    with pytest.raises(ValueError):
        StandingDecision(
            standing_decision_id="sd-unbounded",
            scope=DecisionScope(project_id=project, task_id="job_a"),
            decision_key="dispatch",
            owner_words="run it",
            answered_by="owner",
        )
    with pytest.raises(ValueError):
        StandingDecision(
            standing_decision_id="sd-model",
            scope=DecisionScope(project_id=project, task_id="job_a"),
            decision_key="dispatch",
            owner_words="run it",
            answered_by="orchestrator",
            expires_at=_FUTURE,
        )


def test_expired_decision_is_refused(owner_env: dict[str, Path]) -> None:
    decision_store = away.StandingDecisionStore()
    project = away.project_scope_id("/work/repo")
    decision_store.add(
        StandingDecision(
            standing_decision_id="sd-expired",
            scope=DecisionScope(project_id=project, task_id="job_a"),
            decision_key=away.decision_key_for_question("fixup"),
            owner_words="the old answer",
            answered_by="owner",
            expires_at=_PAST,
        )
    )
    queue = OwnerQuestionQueue()
    _ask(queue, key="fixup", decision_class=DecisionClass.FIX_UP_CONTENT, blocks=("job_a",))
    gate = away.evaluate_dispatch(
        job_id="job_a",
        repo="/work/repo",
        now=_NOW,
        policy=_policy(),
    )
    assert gate.allowed is False and gate.code == "escalated"
    assert queue.questions()[0].status is QuestionStatus.OPEN
    summary = away.build_return_summary()
    assert len(summary.escalations) == 1
    assert not summary.standing_decision_actions


def test_exact_live_decision_answers_verbatim_and_independent_job_proceeds(
    owner_env: dict[str, Path],
) -> None:
    decision_store = away.StandingDecisionStore()
    project = away.project_scope_id("/work/repo")
    words = "Use option two, exactly as written."
    decision_store.add(
        StandingDecision(
            standing_decision_id="sd-live",
            scope=DecisionScope(project_id=project, task_id="job_a"),
            decision_key=away.decision_key_for_question("fixup"),
            owner_words=words,
            answered_by="owner",
            expires_at=_FUTURE,
        )
    )
    queue = OwnerQuestionQueue()
    _ask(queue, key="fixup", decision_class=DecisionClass.FIX_UP_CONTENT, blocks=("job_a",))

    gate = away.evaluate_dispatch(
        job_id="job_a", repo="/work/repo", now=_NOW, policy=_policy()
    )
    assert gate.allowed is True
    answered = queue.questions()[0]
    assert answered.status is QuestionStatus.ANSWERED
    assert answered.answer is not None
    assert answered.answer.words == words
    assert answered.answer.answered_by == "standing-decision:sd-live"
    summary = away.build_return_summary()
    assert len(summary.standing_decision_actions) == 1
    assert summary.standing_decision_actions[0]["owner_words"] == words

    # An independent task with no blocking question is never parked by that.
    other = away.evaluate_dispatch(
        job_id="job_b", repo="/work/repo", now=_NOW, policy=_policy()
    )
    assert other.allowed is True


def test_never_delegable_parks_only_its_task(owner_env: dict[str, Path]) -> None:
    queue = OwnerQuestionQueue()
    _ask(queue, key="push-main", decision_class=DecisionClass.PUSH, blocks=("job_a",))
    policy = _policy()
    tasks = (
        DecisionScope(project_id=away.project_scope_id("/work/repo"), task_id="job_a"),
        DecisionScope(project_id=away.project_scope_id("/work/repo"), task_id="job_b"),
    )
    gate = away.evaluate_dispatch(
        job_id="job_a",
        repo="/work/repo",
        plan_tasks=tasks,
        now=_NOW,
        policy=policy,
    )
    assert gate.allowed is False and gate.code == "parked"
    assert gate.details["never_delegable"] == NeverDelegableKind.PUBLISHING.value
    independent = away.evaluate_dispatch(
        job_id="job_b", repo="/work/repo", plan_tasks=tasks, now=_NOW, policy=policy
    )
    assert independent.allowed is True
    summary = away.build_return_summary()
    assert len(summary.parked_branches) == 1
    assert summary.plan_impact is not None
    runnable = [
        item["task_id"] for item in summary.plan_impact["runnable"]
    ]
    assert runnable == ["job_b"]


def test_preflight_refuses_without_policy_and_on_expired_wall_clock(
    owner_env: dict[str, Path],
) -> None:
    missing = away.evaluate_dispatch(
        job_id="job_a", repo="/work/repo", now=_NOW, policy=None
    )
    assert missing.allowed is False and missing.code == "preflight"
    codes = {blocker["code"] for blocker in missing.details["blockers"]}
    assert {"budget_not_set", "wall_clock_not_set", "no_authorized_scope"} <= codes

    expired = away.evaluate_dispatch(
        job_id="job_a",
        repo="/work/repo",
        now=_NOW,
        policy=_policy(set_at=(_NOW - timedelta(seconds=4_000)).isoformat(), wall_clock=3_600),
    )
    assert expired.allowed is False
    assert "wall_clock_exceeded" in {
        blocker["code"] for blocker in expired.details["blockers"]
    }


def test_circuit_breaker_halts_loudly_and_owner_resets(
    owner_env: dict[str, Path],
) -> None:
    journal = away.AwayJournal()
    project = away.project_scope_id("/work/repo")
    for index in range(3):
        away.record_job_outcome(
            project_id=project, task_id=f"task-{index}", passed=False, journal=journal
        )
    assert journal.halted(project)
    summary = away.build_return_summary()
    assert len(summary.halts) == 1
    assert "three consecutive" in summary.halts[0]["reason"]
    blocked = away.evaluate_dispatch(
        job_id="job_a", repo="/work/repo", now=_NOW, policy=_policy()
    )
    assert blocked.allowed is False and blocked.code == "circuit_open"
    journal.reset_breaker(project)
    assert not journal.halted(project)
    recovered = away.evaluate_dispatch(
        job_id="job_a", repo="/work/repo", now=_NOW, policy=_policy()
    )
    assert recovered.allowed is True


def _owner_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "owner.db")
    store.ensure_tables(db)
    ensure_capacity_tables(db)
    return db


def test_scheduler_parks_only_blocked_task_and_keeps_audit_across_restart(
    owner_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    set_owner_mode(OwnerMode.AWAY, until=_FUTURE, set_by="owner")
    away.save_away_policy(_policy())

    db = _owner_db(tmp_path)
    lease = acquire_lease(db, "scheduler", ttl_seconds=600.0, now=_NOW)
    assert lease is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    for job_id in ("job_a", "job_b"):
        store.insert_job(
            db,
            _make_job(
                job_id,
                str(repo),
                created_at=_NOW.isoformat(),
                updated_at=_NOW.isoformat(),
                request_digest=f"digest-{job_id}",
            ),
            now=_NOW,
        )
    record_owner_quota(db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=_NOW)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy(posture_now=_NOW)
    _ask(OwnerQuestionQueue(), key="push-main", decision_class=DecisionClass.PUSH,
         blocks=("job_a",))

    launched: list[str] = []

    def launcher(job: Any, decision: Any, run_id: str) -> None:
        launched.append(job.job_id)

    first = scheduler_tick(
        db,
        lease,
        now=_NOW,
        routes=routes,
        policy=policy,
        run_launcher=launcher,
        run_status=lambda run_id: RunStatus.RUNNING,
        clock_fn=lambda: _NOW,
    )
    assert first.ran is True
    assert launched == ["job_b"]
    parked = store.get_job(db, "job_a")
    assert parked is not None
    assert parked.state is JobState.QUEUED
    assert parked.waiting_reason is WaitingReason.AWAY_PARKED

    before_restart = away.build_return_summary()
    assert len(before_restart.parked_branches) == 1
    assert before_restart.parked_branches[0]["never_delegable"] == "publishing"

    # A killed scheduler is a new process with no memory: only the durable
    # journal exists.  The audit is identical after the restart.
    after_restart = away.build_return_summary(away.AwayJournal())
    assert after_restart.as_dict() == before_restart.as_dict()
    assert after_restart.revision is not None and after_restart.records > 0

    later = _NOW + timedelta(seconds=30)
    second = scheduler_tick(
        db,
        lease,
        now=later,
        routes=routes,
        policy=policy,
        run_launcher=launcher,
        run_status=lambda run_id: RunStatus.RUNNING,
        clock_fn=lambda: later,
    )
    assert second.ran is True
    assert launched == ["job_b"]
    still_parked = store.get_job(db, "job_a")
    assert still_parked is not None
    assert still_parked.waiting_reason is WaitingReason.AWAY_PARKED
    assert len(away.build_return_summary().parked_branches) == 1


def test_return_summary_matches_records_and_catch_up(
    owner_env: dict[str, Path], tmp_path: Path
) -> None:
    _ask(OwnerQuestionQueue(), key="push-main", decision_class=DecisionClass.PUSH,
         blocks=("job_a",))
    away.evaluate_dispatch(
        job_id="job_a", repo=str(tmp_path), now=_NOW, policy=_policy()
    )
    expected = away.build_return_summary().as_dict()

    db = _owner_db(tmp_path)
    store.insert_job(
        db,
        _make_job(
            "job_a",
            str(tmp_path),
            created_at=_NOW.isoformat(),
            updated_at=_NOW.isoformat(),
            request_digest="digest-a",
        ),
        now=_NOW,
    )
    context = ServerContext(
        db_path=tmp_path / "owner.db",
        questions_store=owner_env["questions"],
    )
    payload = _catch_up(context, {"job_id": "job_a"})
    # The advisor block is attached to the return summary too (F26), so the
    # shared object is compared without it and the same projection is checked
    # on its own.
    summary = dict(payload["return_summary"])
    advisor = summary.pop("advisor", None)
    assert summary == expected
    assert advisor == payload["advisor"]
    assert payload["return_summary"]["parked_branches"] == expected["parked_branches"]


def test_the_return_summary_lists_every_self_answer_with_its_cost(
    owner_env: dict[str, Path],
) -> None:
    """H27: the kept self-answer record feeds the return summary, cost included."""
    queue = OwnerQuestionQueue()
    queue.ask(
        QuestionRequest(
            key="review/fix-up",
            subject="Take the narrow fix?",
            kind="review-finding",
            decision_class=DecisionClass.FIX_UP_CONTENT,
            raised_by_role=AskRole.CHECK,
            raised_by="reviewer:run_a",
            escalation=_escalation(),
            run_id="run_a",
            blocks=("job_a",),
        )
    )
    queue.answer(
        "review/fix-up",
        words="take the narrow fix",
        authority=AnswerAuthority.PLAN,
        answered_by="planner",
        answered_by_role=AskRole.PLAN,
        evidence="the previous gate passed",
        rejected_alternative="widen the fix",
        cost_if_wrong="a wider fix ships late and delays the lane",
    )

    summary = away.build_return_summary()
    (entry,) = summary.self_answered
    assert entry["words"] == "take the narrow fix"
    assert entry["evidence"] == "the previous gate passed"
    assert entry["cost_if_wrong"] == "a wider fix ships late and delays the lane"
    assert summary.has_items is True
    assert "1 self-answered" in summary.one_line()
    assert summary.as_dict()["self_answered"][0]["cost_if_wrong"] == entry["cost_if_wrong"]


def test_cli_grants_exact_bounded_owner_decision(
    owner_env: dict[str, Path], tmp_path: Path
) -> None:
    from saberops.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    code = main(
        [
            "away",
            "grant",
            "--repo",
            str(repo),
            "--task",
            "job_a",
            "--question-key",
            "fixup",
            "--words",
            "use option two",
            "--path",
            "src",
            "--expires-at",
            _FUTURE,
            "--json",
        ]
    )
    assert code == 0
    decisions = away.StandingDecisionStore().decisions()
    assert len(decisions) == 1
    assert decisions[0].decision_key == away.decision_key_for_question("fixup")
    assert decisions[0].scope.task_id == "job_a"
    assert decisions[0].scope.paths == ("src",)
    assert decisions[0].owner_words == "use option two"

    # An unbounded grant and a ttl without its start turn are both refused.
    assert (
        main(
            [
                "away",
                "grant",
                "--repo",
                str(repo),
                "--task",
                "job_a",
                "--key",
                "dispatch",
                "--words",
                "run",
            ]
        )
        == 1
    )
    assert (
        main(
            [
                "away",
                "grant",
                "--repo",
                str(repo),
                "--task",
                "job_a",
                "--key",
                "dispatch",
                "--words",
                "run",
                "--ttl-turns",
                "2",
            ]
        )
        == 1
    )
    # Result: a resolution can only ever be exact-scope.
    sheet = away.StandingDecisionStore().sheet("job_a")
    resolution = resolve_decision(
        sheet,
        Decision(
            decision_id="q-1",
            scope=DecisionScope(
                project_id=away.project_scope_id(repo), task_id="job_a", paths=("src",)
            ),
            decision_key=away.decision_key_for_question("fixup"),
            question="which fix?",
        ),
        now=_NOW.isoformat(),
        current_turn=0,
        authority_mode=AuthorityMode.SUPERVISED,
    )
    assert resolution.disposition is DecisionDisposition.APPLY_STANDING_DECISION


# ---------------------------------------------------------------------------
# D10: say what away mode needs, and let a policy set unpark the queue
# ---------------------------------------------------------------------------


def _away_policy_cli(repo: Path) -> list[str]:
    return [
        "away",
        "policy",
        "set",
        "--budget-units",
        "5",
        "--wall-clock-seconds",
        "3600",
        "--worker-cap",
        "2",
        "--path",
        "src",
    ]


def test_mode_set_away_warns_until_a_policy_exists(
    owner_env: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D10: the owner learns at away-mode time that every job would be parked."""
    from saberops.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    until = "2999-01-01T00:00:00+00:00"

    assert main(["mode", "set", "away", "--until", until]) == 0
    out = capsys.readouterr().out
    assert "Away dispatch of every job is refused" in out
    assert "orch away policy set" in out
    assert "nothing starts" not in out

    assert main(["mode", "set", "away", "--until", until, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["away_policy"] == "not-set"
    assert "away_warning" in payload

    assert main(_away_policy_cli(repo)) == 0
    capsys.readouterr()
    assert main(["mode", "set", "away", "--until", until, "--json"]) == 0
    quiet = json.loads(capsys.readouterr().out)
    assert quiet["away_policy"] == "set"
    assert "away_warning" not in quiet

    assert main(["mode", "clear"]) == 0
    capsys.readouterr()
    # No away mode, no away warning: the policy is only relevant while away.
    assert main(["mode", "set", "ask-me"]) == 0
    assert "Away dispatch of every job is refused" not in capsys.readouterr().out


def test_saving_the_away_policy_resumes_the_parked_queue(
    owner_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D10: a preflight-parked job dispatches on the first tick after the policy."""
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    set_owner_mode(OwnerMode.AWAY, until=_FUTURE, set_by="owner")

    db = _owner_db(tmp_path)
    lease = acquire_lease(db, "scheduler", ttl_seconds=600.0, now=_NOW)
    assert lease is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    store.insert_job(
        db,
        _make_job(
            "job_away",
            str(repo),
            created_at=_NOW.isoformat(),
            updated_at=_NOW.isoformat(),
            request_digest="digest-job_away",
        ),
        now=_NOW,
    )
    record_owner_quota(db, "openai", "chatgpt-plus", 100.0, None, source="OWNER", now=_NOW)
    routes = _make_routes(((_make_candidate("openai", "gpt-5"),),))
    policy = _make_policy(posture_now=_NOW)
    launched: list[str] = []

    def launcher(job: Any, decision: Any, run_id: str) -> None:
        launched.append(job.job_id)

    def tick(moment: Any) -> Any:
        return scheduler_tick(
            db,
            lease,
            now=moment,
            routes=routes,
            policy=policy,
            run_launcher=launcher,
            run_status=lambda run_id: RunStatus.RUNNING,
            clock_fn=lambda: moment,
        )

    first = tick(_NOW)
    assert first.ran is True
    parked = store.get_job(db, "job_away")
    assert parked is not None
    assert parked.waiting_reason is WaitingReason.AWAY_PARKED
    assert launched == []

    away.save_away_policy(
        away.AwayPolicy(
            budget_units=5,
            wall_clock_seconds=3_600,
            worker_cap=2,
            paths=("src",),
            set_at=_NOW.isoformat(),
            set_by="owner",
        )
    )
    later = _NOW + timedelta(seconds=30)
    second = tick(later)
    assert second.ran is True
    assert launched == ["job_away"]


def test_an_absolute_policy_path_is_refused_when_it_is_saved(
    owner_env: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D10: the policy loader only accepts repo-relative paths; saving must too."""
    from saberops.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    with pytest.raises(away.AwayError) as caught:
        away.save_away_policy(
            away.AwayPolicy(
                budget_units=5,
                wall_clock_seconds=3_600,
                worker_cap=2,
                paths=(str(repo),),
                set_at=_NOW.isoformat(),
                set_by="owner",
            )
        )
    assert "repository-relative" in str(caught.value)
    assert not owner_env["policy"].exists()

    assert (
        main(
            [
                "away",
                "policy",
                "set",
                "--budget-units",
                "5",
                "--wall-clock-seconds",
                "3600",
                "--worker-cap",
                "2",
                "--path",
                str(repo),
            ]
        )
        == 1
    )
    assert "repository-relative" in capsys.readouterr().err
    assert not owner_env["policy"].exists()


def test_an_unreadable_policy_blocks_dispatch_instead_of_raising(
    owner_env: dict[str, Path], tmp_path: Path
) -> None:
    """D10: a corrupt policy parks work; it never raises into the scheduler tick."""
    owner_env["policy"].write_text("{not json", encoding="utf-8")
    gate = away.evaluate_dispatch(job_id="job_a", repo=str(tmp_path), now=_NOW)
    assert gate.allowed is False
    assert gate.code == "preflight"


def test_return_summary_lists_candidates_awaiting_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 D17: a standalone away candidate is named with its accept command.

    The four away jobs in the B3 drill were SUCCEEDED with candidate SHAs, but
    ``orch away summary`` read only the journal and plan lanes, so it said
    ``0 parked`` and omitted them entirely.
    """
    from lifecycle_rig import build_lifecycle_rig

    from saberops.capacity.loop import run_scheduler_loop
    from saberops.jobs.service import get_job, submit_job
    from saberops.model_access.readiness_service import ReadinessService
    from saberops.paths import owner_state_dir

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    submitted = submit_job(
        db, repo=rig.repo, goal="Away candidate awaiting the owner"
    )
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert tick is not None and tick.ran and tick.dispatched == 1
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=60.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    run_scheduler_loop(db, once=True, readiness_service=readiness)

    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.state is JobState.SUCCEEDED
    summary = away.build_return_summary(db=db)
    assert len(summary.awaiting_acceptance) == 1
    entry = summary.awaiting_acceptance[0]
    assert entry["job_id"] == job.job_id
    assert entry["run_id"] == job.current_run_id
    assert entry["candidate_sha"]
    assert entry["owner_action"] == f"orch accept {job.current_run_id}"
    assert "1 candidate(s) awaiting acceptance" in summary.one_line()
    assert summary.as_dict()["awaiting_acceptance"][0]["run_id"] == job.current_run_id


def test_dispatch_turn_counts_only_resolution_records(
    owner_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: the turn counter counts resolution records, not every record."""
    queue = OwnerQuestionQueue()
    _ask(queue, key="fixup", decision_class=DecisionClass.FIX_UP_CONTENT, blocks=("job_a",))
    journal = away.AwayJournal()
    journal.append({"kind": "preflight", "run_id": "job_a"})
    journal.append({"kind": "resolution", "run_id": "job_a"})
    journal.append({"kind": "task_outcome", "run_id": "job_a"})
    journal.append({"kind": "wall_clock", "run_id": "job_a"})
    captured: dict[str, int | None] = {}
    real = away.resolve_decision

    def spy(sheet: Any, decision: Any, **kwargs: Any) -> Any:
        captured["turn"] = kwargs.get("current_turn")
        return real(sheet, decision, **kwargs)

    monkeypatch.setattr(away, "resolve_decision", spy)
    gate = away.evaluate_dispatch(
        job_id="job_a",
        repo="/work/repo",
        now=_NOW,
        policy=_policy(),
        journal=journal,
        queue=queue,
    )
    assert gate.allowed is False
    assert captured["turn"] == 1


def test_an_answered_question_does_not_stop_a_later_blocking_one(
    owner_env: dict[str, Path],
) -> None:
    """Covers: after applying a standing decision, the next question is examined."""
    decision_store = away.StandingDecisionStore()
    project = away.project_scope_id("/work/repo")
    decision_store.add(
        StandingDecision(
            standing_decision_id="sd-live",
            scope=DecisionScope(project_id=project, task_id="job_a"),
            decision_key=away.decision_key_for_question("fixup"),
            owner_words="Use option two, exactly as written.",
            answered_by="owner",
            expires_at=_FUTURE,
        )
    )
    queue = OwnerQuestionQueue()
    _ask(queue, key="other", decision_class=DecisionClass.FIX_UP_CONTENT, blocks=("job_b",))
    _ask(queue, key="fixup", decision_class=DecisionClass.FIX_UP_CONTENT, blocks=("job_a",))
    _ask(queue, key="later", decision_class=DecisionClass.FIX_UP_CONTENT, blocks=("job_a",))
    gate = away.evaluate_dispatch(
        job_id="job_a", repo="/work/repo", now=_NOW, policy=_policy()
    )
    assert gate.allowed is False
    assert gate.code == "escalated"
    statuses = [question.status for question in queue.questions()]
    assert statuses == [QuestionStatus.OPEN, QuestionStatus.ANSWERED, QuestionStatus.OPEN]
