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
* This module never executes a gate, never touches slots/disk admission,
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
#: nonempty selection that ran zero tests.
SELECTOR_DEFECT_REASON: Final[str] = "SELECTED_BUT_ZERO_RAN"


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
    """Observed developer-gate evidence (inputs; raw identity preserved)."""

    raw_outcome: str
    exit_code: int | None
    reason: str = ""
    failed: tuple[str, ...] = ()
    flaky: tuple[str, ...] = ()
    quarantined: frozenset[str] = field(default_factory=frozenset)
    checks_ok: bool = True
    selected_nonempty: bool = True
    tests_ran: int | None = None
    has_provenance: bool = True
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


def project_gate_outcome(evidence: GateEvidence) -> ProjectedDecision:
    """Project developer-gate evidence to a normalized decision.

    Pure and deterministic: no I/O, no gate execution, no provider calls.
    Fail-closed: every ambiguous input yields ``BLOCKED`` with
    ``accepted=False``.
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

    if not evidence.has_provenance:
        return _blocked(evidence, role_name, "missing provenance; evidence not authoritative")

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
            reason="code failure; repair-loop input"
            if evidence.checks_ok
            else "lint/type checks or validation failed; repair-loop input",
            failed=failed,
            flaky=flaky,
            unquarantined_flaky=unquarantined,
        )

    if raw == "FLAKY_PASS":
        if code != EXIT_PASS:
            return _blocked(
                evidence, role_name, f"FLAKY_PASS with exit {code}; contradictory evidence"
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
        )

    # raw == "PASS".
    if code != EXIT_PASS:
        return _blocked(
            evidence, role_name, f"PASS with exit {code}; contradictory evidence"
        )
    if not evidence.checks_ok:
        return _blocked(
            evidence, role_name, "PASS with failing lint/type checks; contradictory evidence"
        )
    if failed or flaky:
        return _blocked(
            evidence, role_name, "PASS with failed or flaky tests; contradictory evidence"
        )
    if evidence.selected_nonempty and (evidence.tests_ran is not None) and evidence.tests_ran == 0:
        return _blocked(
            evidence, role_name, "PASS with a nonempty selection that ran zero tests"
        )
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
