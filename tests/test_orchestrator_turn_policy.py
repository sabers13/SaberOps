"""Adversarial contract tests for the pure Orchestrator turn policy (ADR-0004 D11).

Every test states what it protects with a ``Covers:`` docstring line. The whole
file is hermetic: no provider, CLI, login, network, database, filesystem
mutation, sleeping or engine launch. The module under test is pure.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from saberops.orchestrator_turn_policy import (
    BRIEF_MAX_EVIDENCE_PATHS,
    BRIEF_MAX_MANDATORY_CHARS,
    BRIEF_MAX_NOTEBOOK_ENTRIES,
    BRIEF_MAX_STANDING_DECISIONS,
    BRIEF_MAX_TOTAL_CHARS,
    CONTEXT_ROTATION_THRESHOLD_PERCENT,
    MAX_EVIDENCE_PATHS,
    MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION,
    MAX_FAILED_ATTEMPTS_INPUT,
    MAX_IDENTIFIER_LENGTH,
    MAX_NOTEBOOK_ENTRIES,
    MAX_TEXT_ITEM_LENGTH,
    ORCHESTRATOR_TURN_POLICY_VERSION,
    ContextUsage,
    DecisionKind,
    ReasonCode,
    TurnAction,
    TurnPolicyError,
    TurnRequest,
    UnknownContextPolicy,
    WaveState,
    build_brief,
    decide_turn,
    idempotency_key,
)

HEAD_A = "a" * 40
HEAD_B = "b" * 40
WAVE = "wave-2"
_DEFAULT_USAGE = ContextUsage(used_tokens=10, total_tokens=100)


def _usage(used: int, total: int) -> ContextUsage:
    return ContextUsage(used_tokens=used, total_tokens=total)


def _wave(wave_id: str = WAVE, tag: str = "G3_PASS") -> WaveState:
    return WaveState(wave_id=wave_id, base_sha=HEAD_A, head_sha=HEAD_B, outcome_tag=tag)


def _request(
    *,
    decision_point_id: str = "dp-0001",
    run_id: str = "run-7",
    wave_id: str = WAVE,
    kind: DecisionKind = DecisionKind.PLAN_WAVE,
    at_turn_boundary: bool = True,
    session_resumable: bool = True,
    context_usage: ContextUsage | None = _DEFAULT_USAGE,
    failed_attempts: int = 0,
    rotation_requested: bool = False,
    unknown_context_policy: UnknownContextPolicy = UnknownContextPolicy.PREFER_NEW_SESSION,
    plan_reference: str | None = None,
    standing_decisions: tuple[str, ...] = (),
    notebook: tuple[str, ...] = (),
    wave_state: WaveState | None = None,
    question: str | None = None,
    evidence_paths: tuple[str, ...] = (),
) -> TurnRequest:
    return TurnRequest(
        decision_point_id=decision_point_id,
        run_id=run_id,
        wave_id=wave_id,
        kind=kind,
        at_turn_boundary=at_turn_boundary,
        session_resumable=session_resumable,
        context_usage=context_usage,
        failed_attempts=failed_attempts,
        rotation_requested=rotation_requested,
        unknown_context_policy=unknown_context_policy,
        plan_reference=plan_reference,
        standing_decisions=standing_decisions,
        notebook=notebook,
        wave_state=wave_state,
        question=question,
        evidence_paths=evidence_paths,
    )


def _unknown_request(**overrides: Any) -> TurnRequest:
    """Build a request whose usage is explicitly UNKNOWN (None)."""
    overrides.setdefault("context_usage", None)
    return _request(**overrides)


def _raw_request(**kwargs: Any) -> TurnRequest:
    """Construct a request bypassing static typing to exercise runtime validation."""
    return TurnRequest(**kwargs)


# --------------------------------------------------------------------------
# Rotation threshold: below / exactly / above 50 %
# --------------------------------------------------------------------------


def test_below_threshold_reuses_session() -> None:
    """Covers: ADR-0004 D11 rotation strictly above 50%."""
    decision = decide_turn(_request(context_usage=_usage(49, 100)))
    assert decision.action is TurnAction.REUSE_SESSION
    assert decision.reason_code is ReasonCode.REUSE_SESSION_WITHIN_BUDGET
    assert decision.rotation_required is False
    assert decision.context_usage_known is True
    assert (decision.used_tokens, decision.total_tokens) == (49, 100)


def test_exactly_half_reuses_session() -> None:
    """Covers: ADR-0004 D11 at exactly 50% reuse if otherwise safe."""
    decision = decide_turn(_request(context_usage=_usage(50, 100)))
    assert decision.action is TurnAction.REUSE_SESSION
    assert decision.reason_code is ReasonCode.REUSE_SESSION_AT_THRESHOLD
    assert decision.rotation_required is False


def test_exactly_half_with_non_hundred_total_reuses_session() -> None:
    """Covers: ADR-0004 D11 50% boundary uses integer maths, not floats."""
    decision = decide_turn(_request(context_usage=_usage(1, 2)))
    assert decision.action is TurnAction.REUSE_SESSION
    assert decision.reason_code is ReasonCode.REUSE_SESSION_AT_THRESHOLD


@pytest.mark.parametrize("used", [51, 75, 99, 100])
def test_above_threshold_rotates(used: int) -> None:
    """Covers: ADR-0004 D11 strictly >50% rotates between turns."""
    decision = decide_turn(_request(context_usage=_usage(used, 100)))
    assert decision.action is TurnAction.NEW_SESSION
    assert decision.reason_code is ReasonCode.NEW_SESSION_ROTATED
    assert decision.rotation_required is True


def test_zero_usage_reuses_session() -> None:
    """Covers: ADR-0004 D11 empty context is within budget."""
    decision = decide_turn(_request(context_usage=_usage(0, 100)))
    assert decision.action is TurnAction.REUSE_SESSION


def test_threshold_constant_is_fifty() -> None:
    """Covers: ADR-0004 D11 frozen 50% rotation threshold."""
    assert CONTEXT_ROTATION_THRESHOLD_PERCENT == 50


# --------------------------------------------------------------------------
# No session / unknown / corrupt telemetry
# --------------------------------------------------------------------------


def test_no_resumable_session_starts_new() -> None:
    """Covers: ADR-0004 D11 no session => new."""
    decision = decide_turn(_request(session_resumable=False))
    assert decision.action is TurnAction.NEW_SESSION
    assert decision.reason_code is ReasonCode.NEW_SESSION_NO_SESSION
    assert decision.rotation_required is False
    assert decision.brief is not None


def test_unknown_context_prefers_safe_new_session() -> None:
    """Covers: ADR-0004 D11 unknown usage never assumes ample budget."""
    decision = decide_turn(_unknown_request())
    assert decision.action is TurnAction.NEW_SESSION
    assert decision.reason_code is ReasonCode.NEW_SESSION_CONTEXT_UNKNOWN
    assert decision.context_usage_known is False
    assert decision.used_tokens is None and decision.total_tokens is None
    assert decision.rotation_required is True


def test_unknown_usage_without_resumable_session_starts_new() -> None:
    """Covers: ADR-0004 D11 no session precedes UNKNOWN-usage handling."""
    decision = decide_turn(_unknown_request(session_resumable=False))
    assert decision.action is TurnAction.NEW_SESSION
    assert decision.reason_code is ReasonCode.NEW_SESSION_NO_SESSION
    assert decision.context_usage_known is False
    assert decision.rotation_required is False


def test_unknown_context_defer_policy_defers() -> None:
    """Covers: ADR-0004 D11 unknown usage may DEFER explicitly."""
    decision = decide_turn(
        _unknown_request(unknown_context_policy=UnknownContextPolicy.DEFER_WHEN_UNKNOWN)
    )
    assert decision.action is TurnAction.DEFER
    assert decision.reason_code is ReasonCode.DEFER_CONTEXT_UNKNOWN
    assert decision.brief is None


def test_unknown_context_block_policy_blocks() -> None:
    """Covers: ADR-0004 D11 unknown usage may BLOCK explicitly."""
    decision = decide_turn(
        _unknown_request(unknown_context_policy=UnknownContextPolicy.BLOCK_WHEN_UNKNOWN)
    )
    assert decision.action is TurnAction.BLOCKED
    assert decision.reason_code is ReasonCode.BLOCKED_CONTEXT_UNKNOWN
    assert decision.brief is None


@pytest.mark.parametrize(
    "policy",
    [
        UnknownContextPolicy.PREFER_NEW_SESSION,
        UnknownContextPolicy.DEFER_WHEN_UNKNOWN,
        UnknownContextPolicy.BLOCK_WHEN_UNKNOWN,
    ],
)
def test_unknown_context_never_reuses_session(policy: UnknownContextPolicy) -> None:
    """Covers: ADR-0004 D11 unknown usage must not reuse an existing session."""
    decision = decide_turn(_unknown_request(unknown_context_policy=policy))
    assert decision.action is not TurnAction.REUSE_SESSION


def test_unknown_policy_string_coerces() -> None:
    """Covers: ADR-0004 D11 typed policy accepts its literal value."""
    request = _raw_request(
        decision_point_id="dp",
        run_id="run",
        wave_id=WAVE,
        kind="plan_wave",
        at_turn_boundary=True,
        session_resumable=True,
        context_usage=None,
        unknown_context_policy="defer_when_unknown",
    )
    assert decide_turn(request).action is TurnAction.DEFER


def test_unknown_policy_invalid_rejected() -> None:
    """Covers: ADR-0004 D11 strict validation of the unknown-context policy."""
    with pytest.raises(TurnPolicyError):
        _raw_request(
            decision_point_id="dp",
            run_id="run",
            wave_id=WAVE,
            kind="plan_wave",
            at_turn_boundary=True,
            session_resumable=True,
            unknown_context_policy="sometimes",
        )


@pytest.mark.parametrize(
    ("used", "total"),
    [(-1, 100), (0, 0), (101, 100), (5, -5)],
)
def test_corrupt_usage_rejected(used: int, total: int) -> None:
    """Covers: ADR-0004 D11 corrupt telemetry fails closed."""
    with pytest.raises(TurnPolicyError):
        ContextUsage(used_tokens=used, total_tokens=total)


def test_bool_usage_rejected() -> None:
    """Covers: ADR-0004 D11 bool is not a valid token count."""
    with pytest.raises(TurnPolicyError):
        ContextUsage(used_tokens=True, total_tokens=100)


def test_non_usage_object_rejected() -> None:
    """Covers: ADR-0004 D11 malformed usage object fails closed."""
    with pytest.raises(TurnPolicyError):
        _raw_request(
            decision_point_id="dp",
            run_id="run",
            wave_id=WAVE,
            kind="plan_wave",
            at_turn_boundary=True,
            session_resumable=True,
            context_usage=object(),
        )


# --------------------------------------------------------------------------
# Mid-turn rotation prohibition
# --------------------------------------------------------------------------


def test_mid_turn_requested_rotation_is_blocked() -> None:
    """Covers: ADR-0004 D11 rotation request during a turn is refused."""
    decision = decide_turn(
        _request(at_turn_boundary=False, rotation_requested=True, context_usage=_usage(99, 100))
    )
    assert decision.action is TurnAction.BLOCKED
    assert decision.reason_code is ReasonCode.BLOCKED_ROTATION_MID_TURN
    assert decision.rotation_refused is True
    assert decision.brief is None


def test_mid_turn_continues_exact_session() -> None:
    """Covers: ADR-0004 D11 a turn in progress continues the exact session."""
    decision = decide_turn(
        _request(at_turn_boundary=False, rotation_requested=False, context_usage=_usage(10, 100))
    )
    assert decision.action is TurnAction.CONTINUE_EXACT_SESSION
    assert decision.reason_code is ReasonCode.CONTINUE_TURN_IN_PROGRESS
    assert decision.brief is None
    assert decision.rotation_required is False


@pytest.mark.parametrize("rotation_requested", [False, True])
def test_mid_turn_never_returns_new_session(rotation_requested: bool) -> None:
    """Covers: ADR-0004 D11 mid-turn is distinct from permission for a fresh turn."""
    decision = decide_turn(
        _request(
            at_turn_boundary=False,
            rotation_requested=rotation_requested,
            context_usage=_usage(100, 100),
        )
    )
    assert decision.action is not TurnAction.NEW_SESSION


def test_owner_question_precedes_mid_turn_rotation_refusal() -> None:
    """Covers: ADR-0004 D11 repeated failure escalates before turn-boundary rules."""
    decision = decide_turn(
        _request(
            at_turn_boundary=False,
            rotation_requested=True,
            failed_attempts=MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION,
        )
    )
    assert decision.action is TurnAction.OWNER_QUESTION
    assert decision.reason_code is ReasonCode.OWNER_QUESTION_REPEATED_FAILURE
    assert decision.rotation_refused is False


# --------------------------------------------------------------------------
# Failed-attempt escalation and idempotency
# --------------------------------------------------------------------------


@pytest.mark.parametrize("attempts", [0, 1])
def test_fewer_than_two_failures_proceed(attempts: int) -> None:
    """Covers: ADR-0004 D11 fewer than two failed attempts may proceed."""
    decision = decide_turn(_request(failed_attempts=attempts))
    assert decision.action is not TurnAction.OWNER_QUESTION


@pytest.mark.parametrize("attempts", [2, 3, 5])
def test_two_or_more_failures_escalate_to_owner(attempts: int) -> None:
    """Covers: ADR-0004 D11 two failed attempts => owner question, never a third."""
    decision = decide_turn(_request(failed_attempts=attempts))
    assert decision.action is TurnAction.OWNER_QUESTION
    assert decision.reason_code is ReasonCode.OWNER_QUESTION_REPEATED_FAILURE
    assert decision.rotation_required is False
    assert decision.brief is not None


def test_repeated_failure_replay_keeps_decision_key() -> None:
    """Covers: ADR-0004 D11 failed replay uses the same idempotency key."""
    first = decide_turn(_request(failed_attempts=0))
    replay = decide_turn(_request(failed_attempts=1))
    escalated = decide_turn(_request(failed_attempts=2))
    assert first.decision_key == replay.decision_key == escalated.decision_key


def test_negative_failed_attempts_rejected() -> None:
    """Covers: ADR-0004 D11 negative failure count fails closed."""
    with pytest.raises(TurnPolicyError):
        _request(failed_attempts=-1)


def test_bool_failed_attempts_rejected() -> None:
    """Covers: ADR-0004 D11 bool failure count fails closed."""
    with pytest.raises(TurnPolicyError):
        _request(failed_attempts=True)


def test_excessive_failed_attempts_rejected() -> None:
    """Covers: ADR-0004 D11 unbounded failure count fails closed."""
    with pytest.raises(TurnPolicyError):
        _request(failed_attempts=MAX_FAILED_ATTEMPTS_INPUT + 1)


def test_idempotency_key_is_stable_and_domain_bound() -> None:
    """Covers: ADR-0004 D11 stable decision-point key across replays."""
    assert idempotency_key("dp-1", "run-1", "wave-1") == idempotency_key("dp-1", "run-1", "wave-1")
    assert idempotency_key("dp-1", "run-1", "wave-1") != idempotency_key("dp-2", "run-1", "wave-1")
    assert idempotency_key("dp-1", "run-1", "wave-1") != idempotency_key("dp-1", "run-2", "wave-1")
    assert idempotency_key("dp-1", "run-1", "wave-1") != idempotency_key("dp-1", "run-1", "wave-2")


def test_decision_key_matches_request_key() -> None:
    """Covers: ADR-0004 D11 decision carries the request's idempotency key."""
    request = _request(decision_point_id="dp-xyz", run_id="run-9", wave_id="wave-3")
    decision = decide_turn(request)
    assert decision.decision_key == request.idempotency_key
    assert decision.decision_key == idempotency_key("dp-xyz", "run-9", "wave-3")


def test_identical_input_is_deterministic() -> None:
    """Covers: ADR-0004 D11 deterministic action for the same immutable input."""
    first = decide_turn(_request(context_usage=_usage(80, 100)))
    second = decide_turn(_request(context_usage=_usage(80, 100)))
    assert first.as_dict() == second.as_dict()
    assert first.canonical_json() == second.canonical_json()
    assert first == second


# --------------------------------------------------------------------------
# Decision kinds
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(DecisionKind))
def test_all_approved_decision_kinds_accepted(kind: DecisionKind) -> None:
    """Covers: ADR-0004 D11 the four bounded decision points."""
    decision = decide_turn(_request(kind=kind))
    assert decision.decision_key == _request(kind=kind).idempotency_key


def test_kind_value_string_coerces() -> None:
    """Covers: ADR-0004 D11 literal value strings coerce to the enum."""
    request = _raw_request(
        decision_point_id="dp",
        run_id="run",
        wave_id=WAVE,
        kind="triage_review",
        at_turn_boundary=True,
        session_resumable=True,
    )
    assert request.kind is DecisionKind.TRIAGE_REVIEW


def test_kind_name_string_coerces() -> None:
    """Covers: ADR-0004 D11 enum member names coerce to the enum."""
    request = _raw_request(
        decision_point_id="dp",
        run_id="run",
        wave_id=WAVE,
        kind="WAVE_CLOSE",
        at_turn_boundary=True,
        session_resumable=True,
    )
    assert request.kind is DecisionKind.WAVE_CLOSE


def test_arbitrary_kind_rejected() -> None:
    """Covers: ADR-0004 D11 arbitrary autonomous model calls are not decision points."""
    with pytest.raises(TurnPolicyError):
        _raw_request(
            decision_point_id="dp",
            run_id="run",
            wave_id=WAVE,
            kind="autonomous_model_call",
            at_turn_boundary=True,
            session_resumable=True,
        )


# --------------------------------------------------------------------------
# Malformed identifiers and wave identity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", " bad", "bad ", "-leading", "a" * (MAX_IDENTIFIER_LENGTH + 1), "a b", "a\nb", "é"],
)
def test_malformed_decision_point_id_rejected(bad: str) -> None:
    """Covers: ADR-0004 D11 strict validation of malformed IDs."""
    with pytest.raises(TurnPolicyError):
        _request(decision_point_id=bad)


@pytest.mark.parametrize("field", ["run_id", "wave_id"])
def test_malformed_run_and_wave_ids_rejected(field: str) -> None:
    """Covers: ADR-0004 D11 strict validation of run/wave identity."""
    kwargs: dict[str, Any] = {
        "decision_point_id": "dp",
        "run_id": "run",
        "wave_id": WAVE,
        "kind": "plan_wave",
        "at_turn_boundary": True,
        "session_resumable": True,
    }
    kwargs[field] = "bad id"
    with pytest.raises(TurnPolicyError):
        _raw_request(**kwargs)


@pytest.mark.parametrize(
    "good", ["dp-1", "run.7", "wave/2", "A_b-c.d", "x" * MAX_IDENTIFIER_LENGTH]
)
def test_valid_identifiers_accepted(good: str) -> None:
    """Covers: ADR-0004 D11 well-formed identifiers are accepted."""
    assert decide_turn(_request(decision_point_id=good)).action is TurnAction.REUSE_SESSION


def test_non_string_identifier_rejected() -> None:
    """Covers: ADR-0004 D11 non-string identifier fails closed."""
    with pytest.raises(TurnPolicyError):
        _raw_request(
            decision_point_id=123,
            run_id="run",
            wave_id=WAVE,
            kind="plan_wave",
            at_turn_boundary=True,
            session_resumable=True,
        )


def test_conflicting_wave_identity_rejected() -> None:
    """Covers: ADR-0004 D11 conflicting duplicate wave identity fails closed."""
    with pytest.raises(TurnPolicyError):
        _request(wave_state=_wave(wave_id="wave-other"))


def test_matching_wave_identity_accepted() -> None:
    """Covers: ADR-0004 D11 brief wave state must match the decision point."""
    decision = decide_turn(_request(wave_state=_wave()))
    assert decision.brief is not None
    assert decision.brief.wave_state == _wave()


# --------------------------------------------------------------------------
# Exact SHA and outcome-tag validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["abc", "a" * 39, "a" * 41, "A" * 40, "g" * 40, "a" * 39 + "z"])
def test_invalid_base_sha_rejected(bad: str) -> None:
    """Covers: ADR-0004 D11 exact-SHA validation (no abbreviation)."""
    with pytest.raises(TurnPolicyError):
        WaveState(wave_id=WAVE, base_sha=bad, head_sha=HEAD_B, outcome_tag="G3_PASS")


def test_invalid_head_sha_rejected() -> None:
    """Covers: ADR-0004 D11 exact-SHA validation on the wave head."""
    with pytest.raises(TurnPolicyError):
        WaveState(wave_id=WAVE, base_sha=HEAD_A, head_sha="deadbeef", outcome_tag="OPEN")


@pytest.mark.parametrize("bad", ["g3_pass", "3PASS", "_PASS", "", "A" * 33, "G3 PASS"])
def test_invalid_outcome_tag_rejected(bad: str) -> None:
    """Covers: ADR-0004 D11 invalid outcome tags fail closed."""
    with pytest.raises(TurnPolicyError):
        WaveState(wave_id=WAVE, base_sha=HEAD_A, head_sha=HEAD_B, outcome_tag=bad)


def test_valid_outcome_tag_accepted() -> None:
    """Covers: ADR-0004 D11 well-formed outcome tag is accepted."""
    state = WaveState(wave_id=WAVE, base_sha=HEAD_A, head_sha=HEAD_B, outcome_tag="REVIEW_ACCEPT")
    assert state.outcome_tag == "REVIEW_ACCEPT"


# --------------------------------------------------------------------------
# Bounded, fact-only brief
# --------------------------------------------------------------------------


def test_notebook_upper_bound_truncates_with_disclosure() -> None:
    """Covers: ADR-0004 D11 notebook is size-capped with disclosure."""
    notebook = tuple(f"note-{i}" for i in range(BRIEF_MAX_NOTEBOOK_ENTRIES + 5))
    decision = decide_turn(_request(notebook=notebook))
    assert decision.brief is not None
    assert len(decision.brief.notebook) == BRIEF_MAX_NOTEBOOK_ENTRIES
    assert "notebook" in decision.brief.truncations


def test_standing_decisions_truncated_with_disclosure() -> None:
    """Covers: ADR-0004 D11 ordered standing decisions are bounded."""
    standing = tuple(f"decision-{i}" for i in range(BRIEF_MAX_STANDING_DECISIONS + 3))
    decision = decide_turn(_request(standing_decisions=standing))
    assert decision.brief is not None
    assert len(decision.brief.standing_decisions) == BRIEF_MAX_STANDING_DECISIONS
    assert "standing_decisions" in decision.brief.truncations


def test_huge_evidence_path_list_truncated_with_disclosure() -> None:
    """Covers: ADR-0004 D11 evidence path lists are bounded and disclosed."""
    paths = tuple(f"reports/{i:04d}.md" for i in range(BRIEF_MAX_EVIDENCE_PATHS + 7))
    decision = decide_turn(_request(evidence_paths=paths))
    assert decision.brief is not None
    assert len(decision.brief.evidence_paths) == BRIEF_MAX_EVIDENCE_PATHS
    assert "evidence_paths" in decision.brief.truncations


def test_notebook_hard_limit_rejected() -> None:
    """Covers: ADR-0004 D11 unbounded notebook input fails closed."""
    with pytest.raises(TurnPolicyError):
        _request(notebook=tuple("x" for _ in range(MAX_NOTEBOOK_ENTRIES + 1)))


def test_evidence_paths_hard_limit_rejected() -> None:
    """Covers: ADR-0004 D11 unbounded evidence path input fails closed."""
    with pytest.raises(TurnPolicyError):
        _request(evidence_paths=tuple("p" for _ in range(MAX_EVIDENCE_PATHS + 1)))


def test_oversized_text_entry_rejected() -> None:
    """Covers: ADR-0004 D11 oversized notebook entry fails closed."""
    with pytest.raises(TurnPolicyError):
        _request(notebook=("x" * (MAX_TEXT_ITEM_LENGTH + 1),))


def test_empty_text_entry_rejected() -> None:
    """Covers: ADR-0004 D11 empty brief entries fail closed."""
    with pytest.raises(TurnPolicyError):
        _request(notebook=("",))


def test_evidence_paths_are_references_only() -> None:
    """Covers: ADR-0004 D11 evidence is paths, never file content."""
    paths = ("reports/001-company.md", "jds/001.pdf")
    decision = decide_turn(_request(evidence_paths=paths))
    assert decision.brief is not None
    assert decision.brief.evidence_paths == paths


@pytest.mark.parametrize("bad", ["reports/001.md\nraw body", "a\rb", "a\x00b"])
def test_evidence_path_with_content_markers_rejected(bad: str) -> None:
    """Covers: ADR-0004 D11 path fields refuse embedded content."""
    with pytest.raises(TurnPolicyError):
        _request(evidence_paths=(bad,))


@pytest.mark.parametrize("bad", ["lesson\nraw body", "a\rb", "a\x00b"])
def test_notebook_entry_with_content_markers_rejected(bad: str) -> None:
    """Covers: ADR-0004 D11 notebook entries are single-line facts, not content."""
    with pytest.raises(TurnPolicyError):
        _request(notebook=(bad,))


def test_brief_never_embeds_evidence_content() -> None:
    """Covers: ADR-0004 D11 the brief carries references, not raw evidence."""
    decision = decide_turn(_request(evidence_paths=("reports/009.md",)))
    assert decision.brief is not None
    payload = decision.canonical_json()
    assert "content" not in payload
    assert "reports/009.md" in payload


def test_mandatory_brief_budget_blocks_turn() -> None:
    """Covers: ADR-0004 D11 oversized mandatory brief content blocks."""
    decision = decide_turn(_request(question="q" * (BRIEF_MAX_MANDATORY_CHARS + 1)))
    assert decision.action is TurnAction.BLOCKED
    assert decision.reason_code is ReasonCode.BLOCKED_BRIEF_BUDGET
    assert decision.brief is None


def test_mandatory_brief_budget_counts_pinned_wave_state() -> None:
    """Covers: ADR-0004 D11 the pinned wave state is mandatory brief content."""
    wave_chars = len(WAVE) + len(HEAD_A) + len(HEAD_B) + len("G3_PASS")
    question = "q" * (BRIEF_MAX_MANDATORY_CHARS - wave_chars + 1)
    decision = decide_turn(_request(question=question, wave_state=_wave()))
    assert decision.action is TurnAction.BLOCKED
    assert decision.reason_code is ReasonCode.BLOCKED_BRIEF_BUDGET
    assert decision.brief is None


def test_question_within_mandatory_budget_is_kept() -> None:
    """Covers: ADR-0004 D11 the turn question is included verbatim when it fits."""
    question = "q" * 100
    decision = decide_turn(_request(question=question))
    assert decision.brief is not None
    assert decision.brief.question == question


def test_plan_reference_truncated_with_disclosure() -> None:
    """Covers: ADR-0004 D11 optional plan reference is capped with disclosure."""
    decision = decide_turn(_request(plan_reference="p" * 1_000))
    assert decision.brief is not None
    assert decision.brief.plan_reference is not None
    assert len(decision.brief.plan_reference) <= 321
    assert "plan_reference" in decision.brief.truncations


def test_total_brief_budget_reduces_optional_sections() -> None:
    """Covers: ADR-0004 D11 total brief stays within its character budget."""
    decision = decide_turn(
        _request(
            notebook=tuple("n" * 300 for _ in range(BRIEF_MAX_NOTEBOOK_ENTRIES)),
            evidence_paths=tuple("e" * 300 for _ in range(BRIEF_MAX_EVIDENCE_PATHS)),
            standing_decisions=tuple("s" * 300 for _ in range(BRIEF_MAX_STANDING_DECISIONS)),
            plan_reference="p" * 300,
        )
    )
    assert decision.brief is not None
    brief = decision.brief
    size = len(brief.plan_reference or "") + len(brief.question or "")
    size += sum(len(item) for item in brief.standing_decisions)
    size += sum(len(item) for item in brief.notebook)
    size += sum(len(item) for item in brief.evidence_paths)
    assert size <= BRIEF_MAX_TOTAL_CHARS
    assert brief.truncations


def test_build_brief_requires_request() -> None:
    """Covers: ADR-0004 D11 build_brief validates its input type."""
    with pytest.raises(TurnPolicyError):
        build_brief(object())  # type: ignore[arg-type]


def test_decide_turn_requires_request() -> None:
    """Covers: ADR-0004 D11 decide_turn validates its input type."""
    with pytest.raises(TurnPolicyError):
        decide_turn(object())  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Immutability, serialization order and no side effects
# --------------------------------------------------------------------------


def test_sequences_are_normalized_to_immutable_tuples() -> None:
    """Covers: ADR-0004 D11 inputs are stored immutably."""
    request = TurnRequest(
        decision_point_id="dp",
        run_id="run",
        wave_id=WAVE,
        kind=DecisionKind.PLAN_WAVE,
        at_turn_boundary=True,
        session_resumable=True,
        context_usage=_usage(1, 100),
        notebook=["a", "b"],
        evidence_paths=["reports/1.md"],
    )
    assert request.notebook == ("a", "b")
    assert request.evidence_paths == ("reports/1.md",)
    assert isinstance(request.notebook, tuple)


def test_frozen_request_cannot_be_mutated() -> None:
    """Covers: ADR-0004 D11 request model is immutable."""
    request = _request()
    attribute = "notebook"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(request, attribute, ("x",))


def test_frozen_decision_cannot_be_mutated() -> None:
    """Covers: ADR-0004 D11 decision model is immutable."""
    decision = decide_turn(_request())
    attribute = "action"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(decision, attribute, TurnAction.BLOCKED)


def test_decide_turn_has_no_side_effects_on_input() -> None:
    """Covers: ADR-0004 D11 evaluation is side-effect free."""
    request = _request(notebook=("a", "b"), evidence_paths=("x/y",))
    before = dataclasses.asdict(_copy_request(request))
    decide_turn(request)
    assert request.notebook == ("a", "b")
    assert request.evidence_paths == ("x/y",)
    assert dataclasses.asdict(_copy_request(request)) == before


def _copy_request(request: TurnRequest) -> TurnRequest:
    return TurnRequest(
        decision_point_id=request.decision_point_id,
        run_id=request.run_id,
        wave_id=request.wave_id,
        kind=request.kind,
        at_turn_boundary=request.at_turn_boundary,
        session_resumable=request.session_resumable,
        context_usage=request.context_usage,
        failed_attempts=request.failed_attempts,
        rotation_requested=request.rotation_requested,
        unknown_context_policy=request.unknown_context_policy,
        plan_reference=request.plan_reference,
        standing_decisions=tuple(request.standing_decisions),
        notebook=tuple(request.notebook),
        wave_state=request.wave_state,
        question=request.question,
        evidence_paths=tuple(request.evidence_paths),
    )


def test_decision_serialization_key_order_is_fixed() -> None:
    """Covers: ADR-0004 D11 deterministic serialization order."""
    decision = decide_turn(_request())
    assert list(decision.as_dict()) == [
        "action",
        "reason_code",
        "decision_key",
        "rotation_required",
        "rotation_refused",
        "context_usage_known",
        "used_tokens",
        "total_tokens",
        "brief",
        "explanation",
        "policy_version",
    ]


def test_brief_serialization_key_order_is_fixed() -> None:
    """Covers: ADR-0004 D11 deterministic brief serialization order."""
    decision = decide_turn(_request(evidence_paths=("a/b",)))
    assert decision.brief is not None
    assert list(decision.brief.as_dict()) == [
        "plan_reference",
        "standing_decisions",
        "notebook",
        "wave_state",
        "question",
        "evidence_paths",
        "truncations",
        "policy_version",
    ]


def test_decision_carries_policy_version() -> None:
    """Covers: ADR-0004 D11 outputs are versioned for later verifiers."""
    decision = decide_turn(_request())
    assert decision.policy_version == ORCHESTRATOR_TURN_POLICY_VERSION
    assert decision.brief is not None
    assert decision.brief.policy_version == ORCHESTRATOR_TURN_POLICY_VERSION


def test_explanation_names_the_decision_point() -> None:
    """Covers: ADR-0004 D11 decisions are explainable."""
    decision = decide_turn(_request(decision_point_id="dp-42"))
    assert decision.explanation
    assert decision.explanation[0].startswith("dp-42:")


def test_owner_question_constants_are_two() -> None:
    """Covers: ADR-0004 D11 two failed attempts is the escalation point."""
    assert MAX_FAILED_ATTEMPTS_BEFORE_OWNER_QUESTION == 2
