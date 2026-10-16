"""Pure gate-outcome projection (early Step 009 slice, GS-2).

Deterministic, side-effect-free projection from developer-gate evidence
(words from ``scripts/gate_tools.py::evaluate``: ``PASS`` / ``FLAKY_PASS`` /
``FAIL`` / ``TIMEOUT`` / ``INFRA`` plus the ``SELECTED_BUT_ZERO_RAN``
selector defect) to a normalized :class:`ProjectedDecision` that retains
the raw observed identity verbatim.

Design law (ADR-0003 D1/D2/D5, fail-closed):

* Unknown, missing, or contradictory evidence is ``BLOCKED`` -- never
  green, never silently reinterpreted as another word.
* ``INFRA`` is never a pass and never repair-loop input.
* ``FLAKY_PASS`` satisfies G1/G2 without quarantine evidence; G3 accepts
  it only when every flaky test is owner-quarantined, otherwise ``FAIL``.
* Exit codes follow ``docs/gate.md``: 0 = PASS/FLAKY_PASS, 1 = FAIL/TIMEOUT,
  3 = INFRA, 5 = selector defect.  Any other code (including the
  conventional exit 2 for tool/usage errors) is ambiguous: ``BLOCKED``.

Evidence-completeness contract (fail-closed, fix-up 2):

* ``has_provenance`` is ``bool | None = None``.  Only an explicit ``True``
  establishes authoritative provenance; omission (``None``) and ``False``
  are ``BLOCKED``.  Authority is asserted by the caller, never inferred
  from a PASS word or a zero exit code.
* ``checks_ok`` is ``bool | None = None``.  Accepting ``PASS`` /
  ``FLAKY_PASS`` requires an explicit ``True`` lint/type result; omitted
  (unknown) or explicitly failed checks are ``BLOCKED``, including a fully
  quarantined G3 flaky pass.
* ``tests_ran`` is the number of tests the gate actually executed.  For a
  nonempty selection (``selected_nonempty=True``, the fail-closed default)
  acceptance requires a concrete positive ``int``; omitted, zero, negative
  or malformed (bool / non-int) counts are ``BLOCKED``.  A genuinely empty
  selection (for example a lint/type-only role) must be declared explicitly
  with ``selected_nonempty=False``; only then may ``tests_ran`` be ``None``
  or ``0``.  A positive count paired with an explicitly empty selection is
  contradictory and ``BLOCKED``.
* Producer FAIL-only reasons (``SELECTED_BUT_ZERO_RAN``,
  ``pytest failed without rerunnable node IDs`` and the ``unquarantined:``
  prefix) are emitted *only* with ``FAIL``; seeing one under
  ``PASS`` / ``FLAKY_PASS`` is never-emittable contradictory evidence and is
  ``BLOCKED`` rather than laundered green.

Caller migration (future engine wiring): populate ``has_provenance``,
``checks_ok`` and ``tests_ran`` from the trusted gate receipt, never with
invented or defaulted values.  A word alone is not acceptance evidence.

This module never executes a gate, never touches slots/disk admission,
never calls a provider, and never modifies the existing
:class:`saberops.models.GateOutcome` taxonomy or any existing JSON:
:func:`to_gate_outcome` only maps exact matches and returns ``None``
where no exact shared word exists (the FLAKY_PASS/BLOCKED naming gap).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from saberops.models import GateOutcome

#: Raw developer-gate words this projection understands.  Anything else
#: (missing, empty, or a word from another vocabulary) is ambiguous.
KNOWN_RAW_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"PASS", "FLAKY_PASS", "FAIL", "TIMEOUT", "INFRA"}
)

#: Exit codes per docs/gate.md.  Exit 2 (tool/usage error) is deliberately
#: absent: it carries no gate verdict and must stay ambiguous.
EXIT_PASS: Final[int] = 0
EXIT_FAIL: Final[int] = 1
EXIT_INFRA: Final[int] = 3
EXIT_SELECTOR_DEFECT: Final[int] = 5

#: Selector-defect reason emitted by ``gate_tools.evaluate`` for a
#: nonempty selection that ran zero tests (always paired with FAIL/exit 5).
SELECTOR_DEFECT_REASON: Final[str] = "SELECTED_BUT_ZERO_RAN"

#: Producer reasons (``scripts/gate_tools.py::evaluate``) emitted only
#: alongside a ``FAIL`` verdict.  Seeing one under ``PASS``/``FLAKY_PASS``
#: is never-emittable contradictory evidence and must not be green.
PRODUCER_FAIL_ONLY_REASONS: Final[frozenset[str]] = frozenset(
    {SELECTOR_DEFECT_REASON, "pytest failed without rerunnable node IDs"}
)

#: Producer FAIL-only reasons that embed the failing node IDs, so only the
#: exact emitted prefix is matched -- not a broad substring heuristic.
PRODUCER_FAIL_ONLY_REASON_PREFIXES: Final[tuple[str, ...]] = ("unquarantined: ",)


class GateRole(StrEnum):
    """Gate-check role the projection decides acceptance for."""

    G1 = "G1"
    G2 = "G2"
    G3 = "G3"


class ProjectedDecisionKind(StrEnum):
    """Normalized decision vocabulary (superset with fail-closed ``BLOCKED``)."""

    PASS = "PASS"
    FLAKY_PASS = "FLAKY_PASS"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    INFRA = "INFRA"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class GateEvidence:
    """Observed developer-gate evidence (inputs; raw identity preserved).

    The completeness flags default to *absent* (``None``) so that omitting
    evidence fails closed instead of asserting authority.  A caller must
    positively supply ``has_provenance=True``, ``checks_ok=True`` and, for a
    nonempty selection, a positive ``tests_ran`` before any ``PASS`` /
    ``FLAKY_PASS`` is accepted.
    """

    raw_outcome: str
    exit_code: int | None
    reason: str = ""
    failed: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    quarantined: frozenset[str] = field(default_factory=frozenset)
    checks_ok: bool | None = None
    selected_nonempty: bool = True
    tests_ran: int | None = None
    has_provenance: bool | None = None
    role: GateRole | str = GateRole.G1


@dataclass(frozen=True)
class ProjectedDecision:
    """Normalized projection; ``raw_*`` fields echo the input verbatim."""

    decision: ProjectedDecisionKind
    accepted: bool
    raw_outcome: str
    raw_exit_code: int | None
    raw_reason: str
    role: str
    reason: str
    failed: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    unquarantined_flaky: tuple[str, ...] = ()
    selector_defect: bool = False


def _blocked(
    evidence: GateEvidence, role: str, reason: str
) -> ProjectedDecision:
    return ProjectedDecision(
        decision=ProjectedDecisionKind.BLOCKED,
        accepted=False,
        raw_outcome=evidence.raw_outcome,
        raw_exit_code=evidence.exit_code,
        raw_reason=evidence.reason,
        role=role,
        reason=reason,
        failed=tuple(sorted(evidence.failed)),
        flaky=tuple(sorted(evidence.flaky)),
        unquarantined_flaky=tuple(
            sorted(set(evidence.flaky) - set(evidence.quarantined))
        ),
        selector_defect=evidence.reason == SELECTOR_DEFECT_REASON,
    )


def _is_producer_fail_only_reason(reason: str) -> bool:
    """True when ``reason`` is one the producer emits only with ``FAIL``."""
    return reason in PRODUCER_FAIL_ONLY_REASONS or reason.startswith(
        PRODUCER_FAIL_ONLY_REASON_PREFIXES
    )


def _checks_blocked(
    evidence: GateEvidence, role_name: str, word: str
) -> ProjectedDecision | None:
    """Fail-closed lint/type-check guard; ``None`` when explicitly passed."""
    if evidence.checks_ok is None:
        return _blocked(
            evidence,
            role_name,
            f"{word} with unknown lint/type-check evidence; contradictory evidence",
        )
    if evidence.checks_ok is not True:
        return _blocked(
            evidence,
            role_name,
            f"{word} with failing lint/type checks; contradictory evidence",
        )
    return None


def _execution_count_blocked(
    evidence: GateEvidence, role_name: str, word: str
) -> ProjectedDecision | None:
    """Fail-closed executed-test-count guard; ``None`` when acceptable.

    A nonempty selection requires a concrete positive integer.  An
    explicitly empty selection (``selected_nonempty=False``) permits
    ``None`` or ``0``; a positive count there is contradictory.
    """
    ran = evidence.tests_ran
    if isinstance(ran, bool) or (ran is not None and not isinstance(ran, int)):
        return _blocked(
            evidence,
            role_name,
            f"{word} with malformed executed-test count {ran!r}; contradictory evidence",
        )
    if evidence.selected_nonempty:
        if ran is None:
            return _blocked(
                evidence,
                role_name,
                f"{word} with unknown executed-test count for a nonempty selection; "
                "contradictory evidence",
            )
        if ran <= 0:
            return _blocked(
                evidence,
                role_name,
                f"{word} with a nonempty selection that ran {ran} tests; "
                "contradictory evidence",
            )
        return None
    if ran:
        return _blocked(
            evidence,
            role_name,
            f"{word} reporting {ran} executed tests for an explicitly empty selection; "
            "contradictory evidence",
        )
    return None


def project_gate_outcome(evidence: GateEvidence) -> ProjectedDecision:
    """Project developer-gate evidence to a normalized decision.

    Pure and deterministic: no I/O, no gate execution, no provider calls.
    Fail-closed: every ambiguous or incomplete input yields ``BLOCKED``
    with ``accepted=False``.
    """
    try:
        role = GateRole(evidence.role)
    except ValueError:
        return _blocked(
            evidence,
            str(evidence.role),
            f"unknown gate role {str(evidence.role)!r}; cannot decide acceptance",
        )
    role_name = role.value

    if evidence.has_provenance is not True:
        return _blocked(
            evidence,
            role_name,
            "evidence provenance unknown or missing "
            f"(has_provenance={evidence.has_provenance!r}); not authoritative",
        )

    raw = evidence.raw_outcome
    if raw not in KNOWN_RAW_OUTCOMES:
        return _blocked(evidence, role_name, f"unsupported outcome word {raw!r}")

    code = evidence.exit_code
    if code is None:
        return _blocked(evidence, role_name, "missing exit code")
    if code not in (
        EXIT_PASS,
        EXIT_FAIL,
        EXIT_INFRA,
        EXIT_SELECTOR_DEFECT,
    ):
        return _blocked(
            evidence, role_name, f"ambiguous exit code {code}; no gate verdict"
        )

    failed = tuple(sorted(evidence.failed))
    flaky = tuple(sorted(evidence.flaky))
    unquarantined = tuple(sorted(set(flaky) - set(evidence.quarantined)))
    selector_defect = evidence.reason == SELECTOR_DEFECT_REASON

    if raw == "INFRA":
        if code != EXIT_INFRA:
            return _blocked(
                evidence, role_name, f"INFRA with exit {code}; contradictory evidence"
            )
        return ProjectedDecision(
            decision=ProjectedDecisionKind.INFRA,
            accepted=False,
            raw_outcome=raw,
            raw_exit_code=code,
            raw_reason=evidence.reason,
            role=role_name,
            reason=f"infrastructure failure ({evidence.reason or 'no reason given'}); "
            "never a pass, never repair-loop input",
            failed=failed,
            flaky=flaky,
            unquarantined_flaky=unquarantined,
            selector_defect=selector_defect,
        )

    if raw == "TIMEOUT":
        if code != EXIT_FAIL:
            return _blocked(
                evidence, role_name, f"TIMEOUT with exit {code}; contradictory evidence"
            )
        return ProjectedDecision(
            decision=ProjectedDecisionKind.TIMEOUT,
            accepted=False,
            raw_outcome=raw,
            raw_exit_code=code,
            raw_reason=evidence.reason,
            role=role_name,
            reason="gate exceeded its runtime budget; treated as failure with timeout evidence",
            failed=failed,
            flaky=flaky,
            unquarantined_flaky=unquarantined,
            selector_defect=selector_defect,
        )

    if raw == "FAIL":
        if code not in (EXIT_FAIL, EXIT_SELECTOR_DEFECT):
            return _blocked(
                evidence, role_name, f"FAIL with exit {code}; contradictory evidence"
            )
        if code == EXIT_SELECTOR_DEFECT:
            if not selector_defect:
                return _blocked(
                    evidence,
                    role_name,
                    "exit 5 without SELECTED_BUT_ZERO_RAN reason; contradictory evidence",
                )
            return ProjectedDecision(
                decision=ProjectedDecisionKind.FAIL,
                accepted=False,
                raw_outcome=raw,
                raw_exit_code=code,
                raw_reason=evidence.reason,
                role=role_name,
                reason="nonempty selection ran zero tests; selector defect, not a pass",
                failed=failed,
                flaky=flaky,
                unquarantined_flaky=unquarantined,
                selector_defect=True,
            )
        if selector_defect:
            return _blocked(
                evidence,
                role_name,
                "SELECTED_BUT_ZERO_RAN reason with exit 1; contradictory evidence",
            )
        return ProjectedDecision(
            decision=ProjectedDecisionKind.FAIL,
            accepted=False,
            raw_outcome=raw,
            raw_exit_code=code,
            raw_reason=evidence.reason,
            role=role_name,
            reason="lint/type checks or validation failed; repair-loop input"
            if evidence.checks_ok is False
            else "code failure; repair-loop input",
            failed=failed,
            flaky=flaky,
            unquarantined_flaky=unquarantined,
            selector_defect=selector_defect,
        )

    if raw == "FLAKY_PASS":
        if code != EXIT_PASS:
            return _blocked(
                evidence, role_name, f"FLAKY_PASS with exit {code}; contradictory evidence"
            )
        if _is_producer_fail_only_reason(evidence.reason):
            return _blocked(
                evidence,
                role_name,
                f"FLAKY_PASS with producer FAIL-only reason {evidence.reason!r}; "
                "contradictory evidence",
            )
        if not flaky:
            return _blocked(
                evidence, role_name, "FLAKY_PASS with empty flaky set; contradictory evidence"
            )
        if failed:
            return _blocked(
                evidence,
                role_name,
                "FLAKY_PASS with still-failing tests; contradictory evidence",
            )
        checks_block = _checks_blocked(evidence, role_name, raw)
        if checks_block is not None:
            return checks_block
        count_block = _execution_count_blocked(evidence, role_name, raw)
        if count_block is not None:
            return count_block
        if role is GateRole.G3:
            if unquarantined:
                return ProjectedDecision(
                    decision=ProjectedDecisionKind.FAIL,
                    accepted=False,
                    raw_outcome=raw,
                    raw_exit_code=code,
                    raw_reason=evidence.reason,
                    role=role_name,
                    reason="G3 rejects unquarantined flaky tests: "
                    + ", ".join(unquarantined),
                    failed=failed,
                    flaky=flaky,
                    unquarantined_flaky=unquarantined,
                    selector_defect=selector_defect,
                )
            return ProjectedDecision(
                decision=ProjectedDecisionKind.FLAKY_PASS,
                accepted=True,
                raw_outcome=raw,
                raw_exit_code=code,
                raw_reason=evidence.reason,
                role=role_name,
                reason="G3 accepts quarantined-only flaky tests",
                failed=failed,
                flaky=flaky,
                unquarantined_flaky=unquarantined,
                selector_defect=selector_defect,
            )
        return ProjectedDecision(
            decision=ProjectedDecisionKind.FLAKY_PASS,
            accepted=True,
            raw_outcome=raw,
            raw_exit_code=code,
            raw_reason=evidence.reason,
            role=role_name,
            reason=f"{role_name} satisfied; quarantine not required outside G3",
            failed=failed,
            flaky=flaky,
            unquarantined_flaky=unquarantined,
            selector_defect=selector_defect,
        )

    # raw == "PASS".
    if code != EXIT_PASS:
        return _blocked(
            evidence, role_name, f"PASS with exit {code}; contradictory evidence"
        )
    if _is_producer_fail_only_reason(evidence.reason):
        return _blocked(
            evidence,
            role_name,
            f"PASS with producer FAIL-only reason {evidence.reason!r}; "
            "contradictory evidence",
        )
    checks_block = _checks_blocked(evidence, role_name, raw)
    if checks_block is not None:
        return checks_block
    if failed or flaky:
        return _blocked(
            evidence, role_name, "PASS with failed or flaky tests; contradictory evidence"
        )
    count_block = _execution_count_blocked(evidence, role_name, raw)
    if count_block is not None:
        return count_block
    return ProjectedDecision(
        decision=ProjectedDecisionKind.PASS,
        accepted=True,
        raw_outcome=raw,
        raw_exit_code=code,
        raw_reason=evidence.reason,
        role=role_name,
        reason="gate satisfied",
        failed=failed,
        flaky=flaky,
        unquarantined_flaky=unquarantined,
        selector_defect=selector_defect,
    )


def to_gate_outcome(decision: ProjectedDecisionKind) -> GateOutcome | None:
    """Map a projected decision onto the existing ``GateOutcome`` taxonomy.

    Only exact shared words map; ``FLAKY_PASS`` and ``BLOCKED`` have no
    counterpart and yield ``None`` rather than a silent rename.  Never
    invents or renames taxonomy members.
    """
    mapping: dict[ProjectedDecisionKind, GateOutcome | None] = {
        ProjectedDecisionKind.PASS: GateOutcome.PASS,
        ProjectedDecisionKind.FAIL: GateOutcome.VALIDATION_FAILURE,
        ProjectedDecisionKind.TIMEOUT: GateOutcome.TIMEOUT,
        ProjectedDecisionKind.INFRA: GateOutcome.INFRASTRUCTURE_FAILURE,
        ProjectedDecisionKind.FLAKY_PASS: None,
        ProjectedDecisionKind.BLOCKED: None,
    }
    return mapping[decision]
