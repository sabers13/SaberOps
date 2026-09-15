"""Comparability contract enforcement for C12-B experiments (Section 13).

Two replay execution records may be directly compared ONLY when all required
controls match.  Mismatched controls produce NOT_DIRECTLY_COMPARABLE rather
than silent aggregation.

Required controls for direct comparison:
- A verified FrozenStartingState present on BOTH records (fail closed)
- Same canonical Project identity (project_id from verified frozen state)
- Same root_commit (same project history)
- Same starting_sha (same frozen starting state)
- Same checkpoint/state identity (checkpoint_id and state_digest; legitimate
  joint absence is allowed, asymmetric presence is incomparable)
- Same frozen-state identity (frozen_state_digest)
- Same canonical task-definition identity (task_definition_digest)
- Same execution semantics (definitions identical beyond the strategy arm)
- Same verifier specification
- Same verifier-toolchain fingerprint (Section 6 / F-04)

Direct comparison requires POSITIVE EVIDENCE that each mandatory control is
present on both sides.  Equality of absences is never equivalence: two records
that both lack frozen_starting_state, carry an empty/UNKNOWN frozen_state_digest
or task_definition_digest, or both lack an effective_definition are NOT DIRECTLY
COMPARABLE — they are both unproven, not proven identical.

A toolchain fingerprint that is TOOLCHAIN_UNAVAILABLE, UNKNOWN, or None makes
the comparison NOT_DIRECTLY_COMPARABLE EVEN IF BOTH SIDES SHARE THE SAME
SENTINEL: two runs of unknown toolchain are not proven equivalent, they are
both unproven.

Context strategy arm IS allowed to differ — that is the point of C12-B.
"""

from __future__ import annotations

from saberops.replay.contracts import (
    UNKNOWN,
    ComparabilityVerdict,
    ComparisonResult,
    ReplayExecutionRecord,
)
from saberops.replay.toolchain import (
    TOOLCHAIN_UNAVAILABLE,
    fingerprint_is_unavailable,
)


def assert_comparable(
    record_a: ReplayExecutionRecord,
    record_b: ReplayExecutionRecord,
    *,
    allow_different_arm: bool = True,
) -> ComparisonResult:
    """Assert that two execution records satisfy the comparability contract.

    Args:
        record_a: First execution record.
        record_b: Second execution record.
        allow_different_arm: If True (default), different strategy_id values
            are allowed — this is the expected case for cross-arm comparison.
            Set to False to additionally require identical strategy_id.

    Returns:
        ComparisonResult with COMPARABLE or NOT_DIRECTLY_COMPARABLE verdict
        and a tuple of reasons for any mismatches.

    Notes:
        Mismatched toolchain fingerprints ALWAYS produce NOT_DIRECTLY_COMPARABLE.
        This is never suppressed regardless of allow_different_arm.
    """
    reasons: list[str] = []

    # --- Presence of mandatory controls (fail closed) ---
    # Equality of absences is never equivalence.  Every mandatory control must
    # be affirmatively present on BOTH records before any equality is trusted.
    state_a = record_a.frozen_starting_state
    state_b = record_b.frozen_starting_state

    for label, state in (("record_a", state_a), ("record_b", state_b)):
        if state is None:
            reasons.append(f"missing frozen_starting_state on {label}")
            continue
        if not state.project_id:
            reasons.append(f"missing project_id on {label}")
        if not state.root_commit:
            reasons.append(f"missing root_commit on {label}")
        if not state.starting_git_sha:
            reasons.append(f"missing starting_git_sha on {label}")

    for label, starting_sha in (
        ("record_a", record_a.starting_sha),
        ("record_b", record_b.starting_sha),
    ):
        if not starting_sha:
            reasons.append(f"missing starting_sha on {label}")

    for label, digest in (
        ("record_a", record_a.frozen_state_digest),
        ("record_b", record_b.frozen_state_digest),
    ):
        if _control_absent(digest):
            reasons.append(f"missing frozen_state_digest on {label}")

    for label, task_id in (("record_a", record_a.task_id), ("record_b", record_b.task_id)):
        if not task_id:
            reasons.append(f"missing task_id on {label}")

    for label, task_digest in (
        ("record_a", record_a.task_definition_digest),
        ("record_b", record_b.task_definition_digest),
    ):
        if _control_absent(task_digest):
            reasons.append(f"missing task_definition_digest on {label}")

    if record_a.effective_definition is None:
        reasons.append("missing effective_definition on record_a")
    if record_b.effective_definition is None:
        reasons.append("missing effective_definition on record_b")

    # --- Project identity ---

    project_id_a = state_a.project_id if state_a else None
    project_id_b = state_b.project_id if state_b else None
    if project_id_a != project_id_b:
        reasons.append(
            f"project_id mismatch: {project_id_a!r} vs {project_id_b!r}"
        )

    root_commit_a = state_a.root_commit if state_a else None
    root_commit_b = state_b.root_commit if state_b else None
    if root_commit_a != root_commit_b:
        reasons.append(
            f"root_commit mismatch: {root_commit_a!r} vs {root_commit_b!r}"
        )

    # --- Starting state ---
    if record_a.starting_sha != record_b.starting_sha:
        reasons.append(
            f"starting_sha mismatch: {record_a.starting_sha!r} vs {record_b.starting_sha!r}"
        )

    # --- Checkpoint / state identity ---
    ckpt_a = state_a.checkpoint_id if state_a else None
    ckpt_b = state_b.checkpoint_id if state_b else None
    if ckpt_a != ckpt_b:
        reasons.append(f"checkpoint_id mismatch: {ckpt_a!r} vs {ckpt_b!r}")

    sd_a = state_a.state_digest if state_a else None
    sd_b = state_b.state_digest if state_b else None
    if sd_a != sd_b:
        reasons.append(f"state_digest mismatch: {sd_a!r} vs {sd_b!r}")

    # --- Frozen-state identity ---
    if record_a.frozen_state_digest != record_b.frozen_state_digest:
        reasons.append(
            f"frozen_state_digest mismatch: {record_a.frozen_state_digest!r} vs "
            f"{record_b.frozen_state_digest!r}"
        )

    # --- Task identity ---
    if record_a.task_id != record_b.task_id:
        reasons.append(
            f"task_id mismatch: {record_a.task_id!r} vs {record_b.task_id!r}"
        )

    # --- Canonical task-definition identity ---
    td_a = record_a.task_definition_digest
    td_b = record_b.task_definition_digest
    if td_a != td_b:
        reasons.append(
            f"task_definition_digest mismatch: task semantics differ "
            f"({td_a!r} vs {td_b!r})"
        )

    # --- Verifier specification & execution semantics ---
    def_a = record_a.effective_definition
    def_b = record_b.effective_definition
    if def_a is not None and def_b is not None:
        verifier_dict_a = def_a.verifier.to_dict()
        verifier_dict_b = def_b.verifier.to_dict()
        if verifier_dict_a != verifier_dict_b:
            reasons.append(
                "verifier_spec mismatch: definitions differ"
            )
        # Execution semantics: everything except the strategy arm must match.
        semantics_a = {k: v for k, v in def_a.content_dict().items() if k != "strategy_id"}
        semantics_b = {k: v for k, v in def_b.content_dict().items() if k != "strategy_id"}
        if semantics_a != semantics_b:
            reasons.append(
                "execution_semantics mismatch: definitions differ beyond the strategy arm"
            )
    elif def_a is None and def_b is not None:
        reasons.append("verifier_spec: record_a has no effective_definition")
    elif def_a is not None and def_b is None:
        reasons.append("verifier_spec: record_b has no effective_definition")

    # --- Toolchain fingerprint (F-04) — always enforced ---
    fp_a = _get_toolchain_fingerprint(record_a)
    fp_b = _get_toolchain_fingerprint(record_b)

    if fingerprint_is_unavailable(fp_a) or fingerprint_is_unavailable(fp_b):
        # Even when BOTH sides carry the identical sentinel, the runs are not
        # proven comparable: their toolchains are simply unknown.
        reasons.append(
            f"toolchain_fingerprint unavailable/unknown on one or both sides "
            f"({fp_a!r} vs {fp_b!r}) — toolchain equivalence is unproven; "
            "cross-run comparison is NOT DIRECTLY COMPARABLE"
        )
    elif fp_a != fp_b:
        reasons.append(
            f"toolchain_fingerprint mismatch: {fp_a!r} vs {fp_b!r} — "
            "toolchain drift detected; runs are NOT DIRECTLY COMPARABLE"
        )

    # --- Strategy arm (allowed to differ by default) ---
    if not allow_different_arm and record_a.strategy_id != record_b.strategy_id:
        reasons.append(
            f"strategy_id mismatch: {record_a.strategy_id!r} vs {record_b.strategy_id!r}"
        )

    verdict = (
        ComparabilityVerdict.COMPARABLE
        if not reasons
        else ComparabilityVerdict.NOT_DIRECTLY_COMPARABLE
    )
    return ComparisonResult(verdict=verdict, reasons=tuple(reasons))


def _control_absent(value: str) -> bool:
    """Return True if a mandatory digest-like control is absent or UNKNOWN.

    Empty strings, whitespace-only strings, and the UNKNOWN sentinel all denote
    absence.  UNKNOWN on both sides is NOT equivalence — it is double absence.
    """
    if not value:
        return True
    return value.strip() == UNKNOWN or value.strip() == ""


def _get_toolchain_fingerprint(record: ReplayExecutionRecord) -> str:
    """Extract toolchain fingerprint from measurement evidence."""
    if record.measurement is not None:
        fp = record.measurement.verifier_toolchain_fingerprint
        if fp is not None:
            return fp
    return TOOLCHAIN_UNAVAILABLE


def _record_definition_inconsistencies(
    rec: ReplayExecutionRecord,
) -> list[str]:
    """Find contradictions between record-level identity and its definition.

    Canonical owner (C12-E consolidation): this check was previously
    duplicated in ``efficiency.py`` and reused from there by
    ``parallelism.py``.  Records whose record-level identity contradicts
    their embedded effective_definition are rejected outright before any
    dimension normalization.
    """
    eff = rec.effective_definition
    if eff is None:
        return []
    issues: list[str] = []
    if rec.task_id != eff.task_id:
        issues.append(
            f"record task_id {rec.task_id!r} contradicts effective_definition "
            f"task_id {eff.task_id!r}"
        )
    if rec.project_key != eff.project_key:
        issues.append(
            f"record project_key {rec.project_key!r} contradicts "
            f"effective_definition project_key {eff.project_key!r}"
        )
    if rec.starting_sha != eff.frozen_start_sha:
        issues.append(
            f"record starting_sha {rec.starting_sha!r} contradicts "
            f"effective_definition frozen_start_sha {eff.frozen_start_sha!r}"
        )
    if rec.task_definition_digest != eff.task_definition_digest:
        issues.append(
            f"record task_definition_digest {rec.task_definition_digest!r} "
            "contradicts effective_definition task_definition_digest "
            f"{eff.task_definition_digest!r}"
        )
    if rec.definition_digest != eff.definition_digest:
        issues.append(
            f"record definition_digest {rec.definition_digest!r} contradicts "
            f"effective_definition definition_digest {eff.definition_digest!r}"
        )
    return issues


def _positive_security_identity(rec: ReplayExecutionRecord) -> str | None:
    """Return positively bound policy identity, or None when unestablished.

    Canonical owner (C12-E consolidation): previously duplicated in
    ``efficiency.py`` and reused from there by ``parallelism.py``.  The
    current canonical ReplayEngine seam exposes no trustworthy actual
    policy identity bound to the SandboxPolicy it used, so security
    identity is UNESTABLISHED for every current C12 execution record.
    This function always returns None: ordinary caller-written strategy
    evidence — including hand-written 64-hex digests, even ones claiming
    ``bound_to == "canonical_execution_seam"`` — is never positive proof
    of the actual runtime SandboxPolicy and can never open comparability.
    Equality of unproven digests is not equivalence.
    """
    return None
