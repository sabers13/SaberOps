"""Synthetic regression tests for the pure gate-outcome projection; no gate execution."""

from __future__ import annotations

import pytest

from saberops.gate_outcome_projection import (
    GateEvidence,
    GateRole,
    ProjectedDecisionKind,
    project_gate_outcome,
    to_gate_outcome,
)
from saberops.models import GateOutcome


def ev(**kwargs: object) -> GateEvidence:
    base: dict[str, object] = {"raw_outcome": "PASS", "exit_code": 0}
    base.update(kwargs)
    return GateEvidence(**base)  # type: ignore[arg-type]


def test_pass_accepted() -> None:
    """Covers: D1 PASS satisfies the gate with consistent evidence."""
    decision = project_gate_outcome(ev())
    assert decision.decision is ProjectedDecisionKind.PASS
    assert decision.accepted is True
    assert to_gate_outcome(decision.decision) is GateOutcome.PASS


def test_fail_is_code_failure() -> None:
    """Covers: D1 FAIL (exit 1) is a code failure for the repair loop."""
    decision = project_gate_outcome(
        ev(raw_outcome="FAIL", exit_code=1, failed=("tests/test_a.py::test_x",))
    )
    assert decision.decision is ProjectedDecisionKind.FAIL
    assert decision.accepted is False
    assert decision.selector_defect is False
    assert to_gate_outcome(decision.decision) is GateOutcome.VALIDATION_FAILURE


def test_timeout_distinct_from_fail() -> None:
    """Covers: D1 TIMEOUT keeps its own word and is never accepted."""
    decision = project_gate_outcome(
        ev(raw_outcome="TIMEOUT", exit_code=1, failed=("tests/test_a.py::test_slow",))
    )
    assert decision.decision is ProjectedDecisionKind.TIMEOUT
    assert decision.accepted is False
    assert to_gate_outcome(decision.decision) is GateOutcome.TIMEOUT


@pytest.mark.parametrize("reason", ["ENOSPC", "PYTEST_INTERNAL", "INTERRUPTED", "DISK_LOW"])
def test_infra_never_green(reason: str) -> None:
    """Covers: D1 INFRA is never a pass and never repair-loop input."""
    decision = project_gate_outcome(ev(raw_outcome="INFRA", exit_code=3, reason=reason))
    assert decision.decision is ProjectedDecisionKind.INFRA
    assert decision.accepted is False
    assert to_gate_outcome(decision.decision) is GateOutcome.INFRASTRUCTURE_FAILURE


def test_selector_defect_is_fail_with_flag() -> None:
    """Covers: D1 selector defect (exit 5) is FAIL, flagged, never a pass."""
    decision = project_gate_outcome(
        ev(raw_outcome="FAIL", exit_code=5, reason="SELECTED_BUT_ZERO_RAN")
    )
    assert decision.decision is ProjectedDecisionKind.FAIL
    assert decision.accepted is False
    assert decision.selector_defect is True


@pytest.mark.parametrize("word", ["GREEN", "", "SKIPPED", "UNKNOWN", "pass", "Flaky_Pass"])
def test_unsupported_words_blocked(word: str) -> None:
    """Covers: fail-closed behavior for unsupported outcome words."""
    decision = project_gate_outcome(ev(raw_outcome=word, exit_code=0))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert decision.raw_outcome == word
    assert to_gate_outcome(decision.decision) is None


@pytest.mark.parametrize("word", ["PASS", "FLAKY_PASS", "FAIL", "TIMEOUT", "INFRA"])
def test_exit_2_always_ambiguous(word: str) -> None:
    """Covers: ambiguous exit 2 is never success, whatever the word claims."""
    kwargs: dict[str, object] = {"raw_outcome": word, "exit_code": 2}
    if word == "FLAKY_PASS":
        kwargs["flaky"] = ("tests/test_a.py::test_x",)
    decision = project_gate_outcome(ev(**kwargs))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_missing_exit_code_blocked() -> None:
    """Covers: fail-closed behavior for missing exit-code evidence."""
    decision = project_gate_outcome(ev(exit_code=None))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_no_provenance_blocked_even_when_green_shaped() -> None:
    """Covers: evidence without provenance is never accepted as green."""
    decision = project_gate_outcome(ev(has_provenance=False))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


@pytest.mark.parametrize(
    ("word", "code"),
    [
        ("PASS", 1),
        ("PASS", 3),
        ("FAIL", 0),
        ("FAIL", 3),
        ("TIMEOUT", 0),
        ("TIMEOUT", 3),
        ("INFRA", 0),
        ("INFRA", 1),
        ("FLAKY_PASS", 1),
    ],
)
def test_exit_word_mismatch_blocked(word: str, code: int) -> None:
    """Covers: contradictory exit/word pairs stay ambiguous, never reinterpreted."""
    kwargs: dict[str, object] = {"raw_outcome": word, "exit_code": code}
    if word == "FLAKY_PASS":
        kwargs["flaky"] = ("tests/test_a.py::test_x",)
    decision = project_gate_outcome(ev(**kwargs))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_pass_with_failed_tests_blocked() -> None:
    """Covers: PASS claiming failures is contradictory, never green."""
    decision = project_gate_outcome(ev(failed=("tests/test_a.py::test_x",)))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_pass_with_failing_checks_blocked() -> None:
    """Covers: PASS with failing lint/type checks is contradictory."""
    decision = project_gate_outcome(ev(checks_ok=False))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_flaky_pass_empty_flaky_blocked() -> None:
    """Covers: FLAKY_PASS naming no flaky test is contradictory."""
    decision = project_gate_outcome(ev(raw_outcome="FLAKY_PASS", exit_code=0))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_flaky_pass_with_still_failing_blocked() -> None:
    """Covers: FLAKY_PASS with still-failing tests is contradictory."""
    decision = project_gate_outcome(
        ev(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=("tests/test_a.py::test_x",),
            failed=("tests/test_a.py::test_y",),
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_g1_flaky_pass_with_failing_checks_blocked() -> None:
    """Covers: D1 lint/type failure is never laundered green via FLAKY_PASS (G1)."""
    decision = project_gate_outcome(
        ev(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=("tests/test_a.py::test_x",),
            checks_ok=False,
            role=GateRole.G1,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert decision.raw_outcome == "FLAKY_PASS"
    assert decision.raw_exit_code == 0


def test_g3_quarantined_flaky_pass_with_failing_checks_blocked() -> None:
    """Covers: D1 lint/type failure is never laundered green via FLAKY_PASS (G3)."""
    flaky = ("tests/test_a.py::test_x",)
    decision = project_gate_outcome(
        ev(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=flaky,
            quarantined=frozenset(flaky),
            checks_ok=False,
            role=GateRole.G3,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert decision.raw_outcome == "FLAKY_PASS"
    assert decision.raw_exit_code == 0


def test_flaky_pass_with_zero_ran_blocked() -> None:
    """Covers: D1 FLAKY_PASS with a nonempty selection that ran zero tests."""
    decision = project_gate_outcome(
        ev(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=("tests/test_a.py::test_x",),
            selected_nonempty=True,
            tests_ran=0,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_g1_flaky_pass_needs_no_quarantine() -> None:
    """Covers: D1/D5 quarantine evidence is not required for G1 acceptability."""
    decision = project_gate_outcome(
        ev(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=("tests/test_a.py::test_x",),
            role=GateRole.G1,
        )
    )
    assert decision.decision is ProjectedDecisionKind.FLAKY_PASS
    assert decision.accepted is True
    assert decision.unquarantined_flaky == ("tests/test_a.py::test_x",)


def test_g3_rejects_unquarantined_flaky() -> None:
    """Covers: D5 full gate turns unquarantined FLAKY_PASS into FAIL."""
    decision = project_gate_outcome(
        ev(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=("tests/test_a.py::test_x",),
            role=GateRole.G3,
        )
    )
    assert decision.decision is ProjectedDecisionKind.FAIL
    assert decision.accepted is False
    assert decision.unquarantined_flaky == ("tests/test_a.py::test_x",)


def test_g3_accepts_quarantined_only_flaky() -> None:
    """Covers: D5 full gate accepts FLAKY_PASS when every flake is quarantined."""
    flaky = ("tests/test_a.py::test_x",)
    decision = project_gate_outcome(
        ev(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=flaky,
            quarantined=frozenset(flaky),
            role=GateRole.G3,
        )
    )
    assert decision.decision is ProjectedDecisionKind.FLAKY_PASS
    assert decision.accepted is True
    assert decision.unquarantined_flaky == ()


def test_unknown_role_blocked() -> None:
    """Covers: role-specific acceptance fails closed on unknown roles."""
    decision = project_gate_outcome(ev(role="G9"))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert decision.role == "G9"


def test_raw_identity_retained_verbatim() -> None:
    """Covers: prompt contract that raw original identity is retained."""
    decision = project_gate_outcome(
        ev(raw_outcome="FAIL", exit_code=1, reason="unquarantined: tests/test_a.py::t")
    )
    assert decision.raw_outcome == "FAIL"
    assert decision.raw_exit_code == 1
    assert decision.raw_reason == "unquarantined: tests/test_a.py::t"


def test_projection_is_deterministic_and_sorted() -> None:
    """Covers: prompt contract of deterministic classification."""
    kwargs: dict[str, object] = {
        "raw_outcome": "FAIL",
        "exit_code": 1,
        "failed": ("tests/test_b.py::t2", "tests/test_a.py::t1"),
    }
    first = project_gate_outcome(ev(**kwargs))
    second = project_gate_outcome(ev(**kwargs))
    assert first == second
    assert first.failed == ("tests/test_a.py::t1", "tests/test_b.py::t2")


def test_mapping_never_invents_taxonomy() -> None:
    """Covers: contract freeze that existing GateOutcome names are untouched."""
    assert to_gate_outcome(ProjectedDecisionKind.FLAKY_PASS) is None
    assert to_gate_outcome(ProjectedDecisionKind.BLOCKED) is None
