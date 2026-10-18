"""Synthetic regression tests for the pure gate-outcome projection; no gate execution."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from saberops.gate_outcome_projection import (
    PRODUCER_ENGINE,
    GateEvidence,
    GateRole,
    ProjectedDecision,
    ProjectedDecisionKind,
    project_gate_outcome,
    to_gate_outcome,
)
from saberops.models import GateOutcome

#: Literal selector-defect reason emitted by scripts/gate_tools.py::evaluate.
SELECTOR_DEFECT_REASON = "SELECTED_BUT_ZERO_RAN"


def ev(**kwargs: object) -> GateEvidence:
    """Complete, explicitly-truthful synthetic evidence for positive cases.

    Omitting a completeness flag is exactly what the fail-closed contract
    rejects, so omission proofs must build :class:`GateEvidence` directly
    instead of relying on this helper's concrete values.
    """
    base: dict[str, object] = {
        "raw_outcome": "PASS",
        "exit_code": 0,
        "has_provenance": True,
        "checks_ok": True,
        "tests_ran": 1,
    }
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


# --- fix-up 2: omitted-evidence (fail-closed) adversarial cases ---------------
# These construct GateEvidence directly (never via ev()) so that omitting a
# completeness flag is the input under test.


def test_direct_omitted_provenance_blocked() -> None:
    """Covers: ADR-0003 D1/D5 omitted provenance is never accepted green."""
    decision = project_gate_outcome(
        GateEvidence(raw_outcome="PASS", exit_code=0, checks_ok=True, tests_ran=1)
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert decision.raw_outcome == "PASS"
    assert decision.raw_exit_code == 0


def test_direct_omitted_provenance_flaky_blocked() -> None:
    """Covers: ADR-0003 D1/D5 omitted provenance blocks FLAKY_PASS too."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=("tests/test_a.py::test_x",),
            checks_ok=True,
            tests_ran=1,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert decision.raw_outcome == "FLAKY_PASS"


def test_explicit_false_provenance_blocked() -> None:
    """Covers: ADR-0003 D1/D5 provenance asserted False stays blocked."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            has_provenance=False,
            checks_ok=True,
            tests_ran=1,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_omitted_checks_ok_blocks_pass() -> None:
    """Covers: ADR-0003 D1/D5 unknown lint/type result is never accepted."""
    decision = project_gate_outcome(
        GateEvidence(raw_outcome="PASS", exit_code=0, has_provenance=True, tests_ran=1)
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_omitted_checks_ok_blocks_flaky_pass() -> None:
    """Covers: ADR-0003 D1/D5 unknown lint/type result blocks FLAKY_PASS."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=("tests/test_a.py::test_x",),
            has_provenance=True,
            tests_ran=1,
            role=GateRole.G1,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_omitted_checks_ok_blocks_g3_quarantined_flaky() -> None:
    """Covers: ADR-0003 D1/D5 quarantine cannot excuse unknown lint/type evidence."""
    flaky = ("tests/test_a.py::test_x",)
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=flaky,
            quarantined=frozenset(flaky),
            has_provenance=True,
            tests_ran=1,
            role=GateRole.G3,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_omitted_tests_ran_blocks_pass_nonempty() -> None:
    """Covers: ADR-0003 D1/D5 nonempty selection with unknown count is blocked."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            has_provenance=True,
            checks_ok=True,
            selected_nonempty=True,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_omitted_tests_ran_blocks_flaky_pass_nonempty() -> None:
    """Covers: ADR-0003 D1/D5 FLAKY_PASS with unknown count is blocked."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=("tests/test_a.py::test_x",),
            has_provenance=True,
            checks_ok=True,
            selected_nonempty=True,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


@pytest.mark.parametrize("count", [0, -1, -7])
def test_nonpositive_tests_ran_blocks_pass(count: int) -> None:
    """Covers: ADR-0003 D1/D5 zero/negative executed count is never green."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            has_provenance=True,
            checks_ok=True,
            tests_ran=count,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


@pytest.mark.parametrize("count", [True, False, "3", 1.5])
def test_malformed_tests_ran_blocks_pass(count: object) -> None:
    """Covers: ADR-0003 D1/D5 bool/non-int counts fail closed."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            has_provenance=True,
            checks_ok=True,
            tests_ran=count,  # type: ignore[arg-type]
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


def test_empty_selection_allows_zero_ran_lint_only() -> None:
    """Covers: ADR-0003 D1/D5 explicit empty selection permits a zero count."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            has_provenance=True,
            checks_ok=True,
            selected_nonempty=False,
            tests_ran=0,
        )
    )
    assert decision.decision is ProjectedDecisionKind.PASS
    assert decision.accepted is True


def test_empty_selection_allows_unknown_ran() -> None:
    """Covers: ADR-0003 D1/D5 explicit empty selection permits unknown count."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            has_provenance=True,
            checks_ok=True,
            selected_nonempty=False,
        )
    )
    assert decision.decision is ProjectedDecisionKind.PASS
    assert decision.accepted is True


def test_empty_selection_with_positive_ran_blocked() -> None:
    """Covers: ADR-0003 D1/D5 count for an explicitly empty selection contradicts."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            has_provenance=True,
            checks_ok=True,
            selected_nonempty=False,
            tests_ran=5,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False


# --- fix-up 2: contradictory selector / producer FAIL-only reasons -----------


def test_pass_with_selector_defect_reason_blocked() -> None:
    """Covers: ADR-0003 D1/D5 contradictory selector reason cannot be green."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            reason=SELECTOR_DEFECT_REASON,
            has_provenance=True,
            checks_ok=True,
            tests_ran=1,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert decision.selector_defect is True
    assert decision.raw_reason == SELECTOR_DEFECT_REASON
    assert decision.raw_outcome == "PASS"
    assert decision.raw_exit_code == 0


def test_flaky_pass_with_selector_defect_reason_blocked() -> None:
    """Covers: ADR-0003 D1/D5 FLAKY_PASS + selector reason cannot be green."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            reason=SELECTOR_DEFECT_REASON,
            flaky=("tests/test_a.py::test_x",),
            has_provenance=True,
            checks_ok=True,
            tests_ran=1,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert decision.selector_defect is True
    assert decision.raw_reason == SELECTOR_DEFECT_REASON


@pytest.mark.parametrize(
    "reason",
    [
        "pytest failed without rerunnable node IDs",
        "unquarantined: tests/test_a.py::test_x",
    ],
)
def test_pass_with_producer_fail_only_reason_blocked(reason: str) -> None:
    """Covers: ADR-0003 D1/D5 other producer FAIL-only reasons never go green."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            reason=reason,
            has_provenance=True,
            checks_ok=True,
            tests_ran=1,
        )
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert decision.raw_reason == reason


def test_fail_selector_defect_preserved() -> None:
    """Covers: ADR-0003 D1/D5 normal selector FAIL/exit5 stays FAIL, flagged."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="FAIL",
            exit_code=5,
            reason=SELECTOR_DEFECT_REASON,
            has_provenance=True,
            checks_ok=True,
            selected_nonempty=True,
            tests_ran=0,
        )
    )
    assert decision.decision is ProjectedDecisionKind.FAIL
    assert decision.accepted is False
    assert decision.selector_defect is True
    assert decision.raw_reason == SELECTOR_DEFECT_REASON
    assert decision.raw_exit_code == 5


# --- fix-up 2: explicit complete-evidence positives (all roles) --------------


def test_full_evidence_pass_accepted_direct() -> None:
    """Covers: ADR-0003 D1/D5 complete evidence yields PASS."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            has_provenance=True,
            checks_ok=True,
            selected_nonempty=True,
            tests_ran=44,
        )
    )
    assert decision.decision is ProjectedDecisionKind.PASS
    assert decision.accepted is True
    assert decision.selector_defect is False


def test_full_evidence_flaky_pass_g1_accepted_direct() -> None:
    """Covers: ADR-0003 D1/D5 complete G1 evidence yields FLAKY_PASS."""
    flaky = ("tests/test_a.py::test_x",)
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=flaky,
            has_provenance=True,
            checks_ok=True,
            tests_ran=44,
            role=GateRole.G1,
        )
    )
    assert decision.decision is ProjectedDecisionKind.FLAKY_PASS
    assert decision.accepted is True
    assert decision.unquarantined_flaky == flaky


def test_full_evidence_flaky_pass_g3_quarantined_accepted_direct() -> None:
    """Covers: ADR-0003 D5 complete G3 quarantined evidence yields FLAKY_PASS."""
    flaky = ("tests/test_a.py::test_x",)
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=flaky,
            quarantined=frozenset(flaky),
            has_provenance=True,
            checks_ok=True,
            tests_ran=44,
            role=GateRole.G3,
        )
    )
    assert decision.decision is ProjectedDecisionKind.FLAKY_PASS
    assert decision.accepted is True
    assert decision.unquarantined_flaky == ()


def test_g2_pass_accepted() -> None:
    """Covers: ADR-0003 D1/D2 G2 fast-gate PASS is accepted."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="PASS",
            exit_code=0,
            has_provenance=True,
            checks_ok=True,
            tests_ran=3,
            role=GateRole.G2,
        )
    )
    assert decision.decision is ProjectedDecisionKind.PASS
    assert decision.accepted is True
    assert decision.role == "G2"


def test_g2_flaky_pass_needs_no_quarantine() -> None:
    """Covers: ADR-0003 D1/D2/D5 G2 accepts unquarantined FLAKY_PASS."""
    decision = project_gate_outcome(
        GateEvidence(
            raw_outcome="FLAKY_PASS",
            exit_code=0,
            flaky=("tests/test_a.py::test_x",),
            has_provenance=True,
            checks_ok=True,
            tests_ran=3,
            role=GateRole.G2,
        )
    )
    assert decision.decision is ProjectedDecisionKind.FLAKY_PASS
    assert decision.accepted is True
    assert decision.role == "G2"


# --- fix-up 2: hermetic producer -> adapter compatibility (no gate run) ------


def _load_gate_tools() -> Any:
    """Load scripts/gate_tools.py by path: pure import, no gate execution."""
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "gate_tools.py"
    spec = importlib.util.spec_from_file_location("_gs2_gate_tools_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _project_producer(
    word: str,
    reason: str,
    code: int,
    failed: list[str],
    flaky: list[str],
    *,
    tests_ran: int,
    role: GateRole = GateRole.G1,
) -> ProjectedDecision:
    """Bind an evaluate() tuple to the projection with trusted receipt extras."""
    return project_gate_outcome(
        GateEvidence(
            raw_outcome=word,
            exit_code=code,
            reason=reason,
            failed=tuple(failed),
            flaky=tuple(flaky),
            has_provenance=True,
            checks_ok=True,
            selected_nonempty=True,
            tests_ran=tests_ran,
            role=role,
        )
    )


def test_producer_evaluate_round_trip_maps_cleanly() -> None:
    """Covers: ADR-0003 D1/D5 real evaluate() evidence projects consistently."""
    gate_tools = _load_gate_tools()

    passing = gate_tools.Evidence(
        codes=[0], count=2, passed={"tests/t.py::a", "tests/t.py::b"}
    )
    word, reason, code, failed, flaky = gate_tools.evaluate(passing)
    assert (word, reason, code) == ("PASS", "", 0)
    decision = _project_producer(word, reason, code, failed, flaky, tests_ran=passing.count)
    assert decision.decision is ProjectedDecisionKind.PASS
    assert decision.accepted is True

    flaked = gate_tools.Evidence(
        codes=[1], count=2, passed={"tests/t.py::b"}, failed={"tests/t.py::a": "boom"}
    )
    rerun = gate_tools.Evidence(codes=[0], count=1, passed={"tests/t.py::a"})
    word, reason, code, failed, flaky = gate_tools.evaluate(
        flaked, rerun, quarantine={"tests/t.py::a"}
    )
    assert (word, code) == ("FLAKY_PASS", 0)
    decision = _project_producer(word, reason, code, failed, flaky, tests_ran=flaked.count)
    assert decision.decision is ProjectedDecisionKind.FLAKY_PASS
    assert decision.accepted is True
    assert decision.flaky == tuple(flaky)

    defect = gate_tools.Evidence(codes=[0], count=0)
    word, reason, code, failed, flaky = gate_tools.evaluate(defect, selected=True)
    assert (word, reason, code) == ("FAIL", SELECTOR_DEFECT_REASON, 5)
    decision = _project_producer(word, reason, code, failed, flaky, tests_ran=defect.count)
    assert decision.decision is ProjectedDecisionKind.FAIL
    assert decision.accepted is False
    assert decision.selector_defect is True


# ---------------------------------------------------------------------------
# Engine producer contract (ADR-0004 D3): only engine-run gates are evidence
# ---------------------------------------------------------------------------


def test_engine_producer_green_pass_accepted() -> None:
    """Covers: ADR-0004 D3 (engine-run gate is authoritative evidence)."""
    decision = project_gate_outcome(ev(producer=PRODUCER_ENGINE))
    assert decision.decision is ProjectedDecisionKind.PASS
    assert decision.accepted is True


def test_unspecified_producer_keeps_legacy_contract() -> None:
    """Covers: producer=None keeps the existing caller-asserted contract."""
    decision = project_gate_outcome(ev())
    assert decision.decision is ProjectedDecisionKind.PASS
    assert decision.accepted is True


@pytest.mark.parametrize("producer", ["model", "G0", "worker", "unknown", ""])
def test_non_engine_producer_blocked_even_when_green_shaped(producer: str) -> None:
    """Covers: ADR-0004 D3 (G0/model runs are never accepted as evidence)."""
    decision = project_gate_outcome(ev(producer=producer))
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
    assert "producer" in decision.reason


def test_non_engine_producer_blocks_fail_too() -> None:
    """Covers: a model's own failing run is also not authoritative evidence."""
    decision = project_gate_outcome(
        ev(raw_outcome="FAIL", exit_code=1, producer="model")
    )
    assert decision.decision is ProjectedDecisionKind.BLOCKED
    assert decision.accepted is False
