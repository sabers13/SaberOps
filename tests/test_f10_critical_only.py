"""Phase 1 F10: critical-only question delegation.

The lane adds one derived ``critical`` tag to every question and gates planner
self-answers on the effective owner mode.  These tests use a fake planner (a
writer lease plus the MCP tool) and never call a real vendor CLI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.cli import main
from saberops.jobs.service import submit_job
from saberops.mcp.errors import ForbiddenOperationError
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.owner_mode import (
    OwnerMode,
    QuestionDisposition,
    decide_question_answer,
    set_owner_mode,
)
from saberops.questions import (
    AnswerAuthority,
    AskRole,
    DecisionClass,
    Escalation,
    OwnerQuestionQueue,
    QuestionRequest,
    QuestionStoreError,
    critical_reasons,
    is_critical_question,
    question_return_summary,
    question_view,
    request_from_payload,
)

pytestmark = pytest.mark.packaged_routing_default


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _request(
    key: str = "q-routine",
    *,
    decision_class: DecisionClass = DecisionClass.ROUTINE_QUESTION,
    kind: str = "routine",
    escalated: bool = False,
    run_id: str | None = None,
    blocks: tuple[str, ...] = (),
    raised_by_role: AskRole = AskRole.BUILD,
    raised_by: str = "worker-1",
) -> QuestionRequest:
    return QuestionRequest(
        key=key,
        subject="What should happen",
        kind=kind,
        decision_class=decision_class,
        raised_by_role=raised_by_role,
        raised_by=raised_by,
        escalation=Escalation(
            requirement="The worker needs one decision to continue",
            proposed_expansion="Take the recommended option",
            smallest_compliant="Answer only this question",
            consequences="The job stays parked until it is answered",
            recommendation="Take the routine option",
        ),
        run_id=run_id,
        blocks=blocks,
        options=("yes", "no"),
        escalated=escalated,
    )


def _ask(store: Path, request: QuestionRequest | None = None) -> OwnerQuestionQueue:
    queue = OwnerQuestionQueue(store)
    queue.ask(request or _request())
    return queue


def _planner_context(
    store: Path, tmp_path: Path, *, db_path: Path | None = None
) -> ServerContext:
    lease = OrchestratorLease(tmp_path / "lease.json", holder="fake-planner", pid=os.getpid())
    assert lease.acquire() is True
    evidence_root = tmp_path / ".orch"
    evidence_root.mkdir(exist_ok=True)
    return ServerContext(
        db_path=db_path if db_path is not None else tmp_path / "mcp.db",
        questions_store=store,
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(evidence_root,)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
        lease=lease,
    )


def _answer(
    context: ServerContext,
    *,
    ref: str = "q-routine",
    words: str = "take the recommended option",
    evidence: str = "the previous gate passed",
    rejected: str = "escalate to the owner instead",
) -> dict[str, Any]:
    registry = build_default_registry(context)
    return registry.call(
        "answer_question",
        {
            "question_id_or_key": ref,
            "answer_text": words,
            "authority": "PLAN",
            "answered_by": "fake-planner",
            "evidence": evidence,
            "rejected_alternative": rejected,
        },
    )


# --------------------------------------------------------------------------- #
# The derived critical tag
# --------------------------------------------------------------------------- #


def test_critical_tag_is_the_additive_union(isolated: Path) -> None:
    """Owner-only, acceptance, never-delegable and escalation are all critical."""
    owner_only = _request(decision_class=DecisionClass.PUSH)
    assert is_critical_question(owner_only)
    assert "owner-only" in " ".join(critical_reasons(owner_only))

    acceptance = _request(decision_class=DecisionClass.ACCEPTANCE)
    assert is_critical_question(acceptance)
    assert acceptance.answerable_by.value == "OWNER"

    never_delegable = _request(kind="test_gate")
    assert is_critical_question(never_delegable)
    assert "never delegable" in " ".join(critical_reasons(never_delegable))

    escalated = _request(escalated=True)
    assert is_critical_question(escalated)
    assert "escalated" in " ".join(critical_reasons(escalated))

    routine = _request()
    assert not is_critical_question(routine)
    assert critical_reasons(routine) == ()


def test_escalation_can_only_add_never_remove(isolated: Path) -> None:
    """An owner-only class stays critical even when the payload says otherwise."""
    routine_but_owner_only = _request(decision_class=DecisionClass.MOVE_MAIN, escalated=False)
    assert is_critical_question(routine_but_owner_only)

    never_delegable_but_unflagged = _request(kind="publishing", escalated=False)
    assert is_critical_question(never_delegable_but_unflagged)


def test_escalation_tag_persists_and_is_exposed(isolated: Path) -> None:
    """A stored escalation survives reload and appears in every question view."""
    store = isolated / "questions.json"
    queue = _ask(store, _request("q-escalated", escalated=True))
    assert queue.orchestrator_alone_decisions() == ()

    reloaded = OwnerQuestionQueue(store).get("q-escalated")
    assert reloaded is not None
    assert reloaded.request.escalated is True
    assert reloaded.critical is True

    view = question_view(reloaded)
    assert view["critical"] is True
    assert view["escalated"] is True
    assert view["critical_reasons"]


def test_escalation_must_be_a_boolean() -> None:
    """A truthy string is refused rather than silently treated as escalation."""
    payload = _request().to_mapping()
    payload["escalated"] = "true"
    with pytest.raises(QuestionStoreError, match="boolean"):
        request_from_payload(payload)


# --------------------------------------------------------------------------- #
# The mode policy
# --------------------------------------------------------------------------- #


def test_ask_me_leaves_every_question_to_the_owner(isolated: Path) -> None:
    """The default mode is manual: a routine question still waits."""
    question = _ask(isolated / "questions.json").get("q-routine")
    assert question is not None
    decision = decide_question_answer(question, planner_present=True)
    assert decision.disposition is QuestionDisposition.ASK_OWNER
    assert decision.allowed is False
    assert "ask-me" in decision.reason


def test_critical_question_waits_in_every_mode(isolated: Path) -> None:
    """A critical question is never delegated, even in critical-only."""
    set_owner_mode(OwnerMode.CRITICAL_ONLY)
    queue = _ask(isolated / "questions.json", _request(escalated=True))
    question = queue.get("q-routine")
    assert question is not None
    decision = decide_question_answer(question, planner_present=True)
    assert decision.critical is True
    assert decision.disposition is QuestionDisposition.ASK_OWNER
    assert "critical" in decision.reason


def test_no_planner_waits_without_a_background_planner(isolated: Path) -> None:
    """Critical-only with no live planner parks the question and answers nothing."""
    set_owner_mode(OwnerMode.CRITICAL_ONLY)
    store = isolated / "questions.json"
    queue = _ask(store)
    question = queue.get("q-routine")
    assert question is not None

    decision = decide_question_answer(question, planner_present=False)
    assert decision.disposition is QuestionDisposition.WAIT_FOR_PLANNER
    assert decision.allowed is False
    assert "no background planner" in decision.reason

    # Closing the session loses no question: the durable document still holds it.
    reloaded = OwnerQuestionQueue(store).get("q-routine")
    assert reloaded is not None
    assert reloaded.status.value == "OPEN"
    assert queue.orchestrator_alone_decisions() == ()
    assert question_view(reloaded)["critical"] is False


def test_critical_only_with_a_live_planner_permits_a_routine_answer(isolated: Path) -> None:
    """The same routine question is answerable once a live planner is present."""
    set_owner_mode(OwnerMode.CRITICAL_ONLY)
    queue = _ask(isolated / "questions.json")
    question = queue.get("q-routine")
    assert question is not None
    decision = decide_question_answer(question, planner_present=True)
    assert decision.disposition is QuestionDisposition.PLANNER_MAY_ANSWER
    assert decision.allowed is True


# --------------------------------------------------------------------------- #
# The MCP surface
# --------------------------------------------------------------------------- #


def test_critical_only_answers_a_routine_question_and_logs_it(
    isolated: Path, tmp_path: Path
) -> None:
    """A fake planner answers a routine question; the answer is logged."""
    set_owner_mode(OwnerMode.CRITICAL_ONLY)
    store = isolated / "questions.json"
    _ask(store, _request(decision_class=DecisionClass.FIX_UP_CONTENT))
    context = _planner_context(store, tmp_path)

    payload = _answer(context)
    assert payload["question"]["status"] == "ANSWERED"
    assert payload["question"]["critical"] is False
    assert payload["answer_policy"]["mode"] == "critical-only"
    assert payload["answer_policy"]["disposition"] == "planner_may_answer"

    queue = OwnerQuestionQueue(store)
    audit = queue.orchestrator_alone_decisions()
    assert len(audit) == 1
    assert audit[0].words == "take the recommended option"
    assert audit[0].evidence == "the previous gate passed"
    assert audit[0].rejected_alternative == "escalate to the owner instead"


def test_ask_me_refuses_a_planner_self_answer(isolated: Path, tmp_path: Path) -> None:
    """Manual mode keeps the answer with the owner."""
    store = isolated / "questions.json"
    _ask(store, _request(decision_class=DecisionClass.FIX_UP_CONTENT))
    context = _planner_context(store, tmp_path)

    with pytest.raises(ForbiddenOperationError, match="ask-me"):
        _answer(context)

    question = OwnerQuestionQueue(store).get("q-routine")
    assert question is not None
    assert question.status.value == "OPEN"
    assert OwnerQuestionQueue(store).orchestrator_alone_decisions() == ()


def test_critical_question_is_refused_to_the_planner(
    isolated: Path, tmp_path: Path
) -> None:
    """An escalated routine question is refused even in critical-only."""
    set_owner_mode(OwnerMode.CRITICAL_ONLY)
    store = isolated / "questions.json"
    _ask(store, _request(decision_class=DecisionClass.FIX_UP_CONTENT, escalated=True))
    context = _planner_context(store, tmp_path)

    with pytest.raises(ForbiddenOperationError) as excinfo:
        _answer(context)
    assert excinfo.value.data["critical"] is True
    assert excinfo.value.data["disposition"] == "ask_owner"

    question = OwnerQuestionQueue(store).get("q-routine")
    assert question is not None
    assert question.status.value == "OPEN"
    assert OwnerQuestionQueue(store).orchestrator_alone_decisions() == ()


def test_owner_only_class_is_refused_before_the_mode_policy(
    isolated: Path, tmp_path: Path
) -> None:
    """The ADR-0004 D6 split still refuses an owner-only class outright."""
    set_owner_mode(OwnerMode.CRITICAL_ONLY)
    store = isolated / "questions.json"
    _ask(store, _request(decision_class=DecisionClass.MOVE_MAIN))
    context = _planner_context(store, tmp_path)

    with pytest.raises(ForbiddenOperationError, match="owner-only"):
        _answer(context)


# --------------------------------------------------------------------------- #
# catch_up, the return summary and notices
# --------------------------------------------------------------------------- #


def test_catch_up_shows_the_self_answer(rig: RigEnvironment, tmp_path: Path) -> None:
    """A job's self-answered decision is visible to a fresh session."""
    set_owner_mode(OwnerMode.CRITICAL_ONLY)
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="show the self answer").job
    store = tmp_path / "questions.json"
    _ask(
        store,
        _request(
            decision_class=DecisionClass.FIX_UP_CONTENT,
            blocks=(job.job_id,),
        ),
    )
    context = _planner_context(store, tmp_path, db_path=db.db_path)
    _answer(context)

    payload = build_default_registry(context).call("catch_up", {"job_id": job.job_id})
    assert len(payload["self_answered"]) == 1
    entry = payload["self_answered"][0]
    assert entry["words"] == "take the recommended option"
    assert entry["evidence"] == "the previous gate passed"
    assert entry["rejected_alternative"] == "escalate to the owner instead"


def test_return_summary_lists_self_answers_and_open_critical(isolated: Path) -> None:
    """The reusable summary is derived from records and writes nothing."""
    store = isolated / "questions.json"
    queue = _ask(store)
    queue.ask(
        _request(
            "q-critical",
            decision_class=DecisionClass.BILLING_OR_POSTURE,
            run_id="run-a",
        )
    )
    queue.ask(_request("q-routine-a", run_id="run-a", decision_class=DecisionClass.FIX_UP_CONTENT))
    queue.answer(
        "q-routine-a",
        words="take the recommended option",
        authority=AnswerAuthority.PLAN,
        answered_by="fake-planner",
        answered_by_role=AskRole.PLAN,
        evidence="the previous gate passed",
        rejected_alternative="escalate to the owner instead",
    )
    before = queue.path.read_bytes()

    summary = question_return_summary(queue, run_ids=("run-a",))
    assert len(summary.self_answered) == 1
    assert summary.self_answered[0].run_id == "run-a"
    assert [item.key for item in summary.critical_open] == ["q-critical"]
    assert summary.routine_open == ()
    assert question_return_summary(queue, run_ids=("run-b",)).to_mapping() == {
        "self_answered": [],
        "critical_open": [],
        "routine_open": [],
    }
    assert queue.path.read_bytes() == before


def test_critical_question_sends_a_critical_notice(
    rig: RigEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notifier labels a critical question as critical and a routine one plainly."""
    from saberops.notifications import deliver_pending

    queue = OwnerQuestionQueue()
    queue.ask(_request("q-critical", decision_class=DecisionClass.PUSH))
    queue.ask(_request("q-routine"))

    log = tmp_path / "notices"
    stub = tmp_path / "notify-send"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NOTICE_LOG"\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("NOTICE_LOG", str(log))
    monkeypatch.setenv("SABEROPS_DESKTOP_NOTIFICATIONS", "1")

    deliver_pending(rig.db())
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert any(line.startswith("SaberOps critical question") for line in lines)
    assert any(line.startswith("SaberOps question asked") for line in lines)


# --------------------------------------------------------------------------- #
# The owner CLI can escalate a routine question
# --------------------------------------------------------------------------- #


def test_cli_escalate_marks_a_routine_question_critical(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`orch questions ask --escalate` records the orchestrator's judgment."""
    code = main(
        [
            "questions",
            "ask",
            "--key",
            "q-cli-escalated",
            "--subject",
            "A routine question the planner judges critical",
            "--kind",
            "routine",
            "--decision-class",
            "ROUTINE_QUESTION",
            "--role",
            "BUILD",
            "--by",
            "worker-1",
            "--requirement",
            "r",
            "--expansion",
            "p",
            "--smallest",
            "s",
            "--consequences",
            "c",
            "--recommendation",
            "r",
            "--escalate",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert (code, captured.err) == (0, "")
    view = json.loads(captured.out)
    assert view["critical"] is True
    assert view["escalated"] is True
