"""Safe accept and integration engine for verified candidate runs."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from saberops.candidate_lifecycle import find_matching_rejection_event
from saberops.db import Database
from saberops.gate_runner import (
    GateReceipt,
    GateRunner,
    GateRunnerInfrastructureError,
    GateRunnerRequest,
    GateScratchPreflightError,
)
from saberops.git import GitManager, GitRefRaceError
from saberops.models import (
    Attempt,
    AttemptStatus,
    GateScratchMode,
    ReviewVerdict,
    Run,
    RunStatus,
)
from saberops.provenance import (
    ProvenanceError,
    assert_candidate_provenance,
    assert_run_repository,
    failover_authorized_bases,
)


@dataclass(frozen=True)
class AcceptPrecondition:
    """One read-only acceptance precondition for UI rendering.

    ``status`` is ``"pass"`` or ``"fail"``; ``reason`` carries the
    human-readable evidence (or the exact failure).  The Web renders
    these as a checklist and gates the Accept button on the derived
    eligibility -- it never recreates acceptance policy itself.
    """

    key: str
    label: str
    status: str
    reason: str

    def passed(self) -> bool:
        """Return True only for an explicit pass."""
        return self.status == "pass"


@dataclass(frozen=True)
class AcceptEligibility:
    """Read-only projection of acceptance preconditions for a candidate.

    ``eligible`` is True when the acceptance preconditions are
    currently satisfied.  It is a static pre-CAS promise only:
    acceptance revalidates state and still runs the post-integration
    gate, so an eligible projection never guarantees successful
    acceptance.
    """

    run_id: str
    eligible: bool
    checks: tuple[AcceptPrecondition, ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize for JSON endpoints and template contexts."""
        return {
            "run_id": self.run_id,
            "eligible": self.eligible,
            "checks": [
                {
                    "key": check.key,
                    "label": check.label,
                    "status": check.status,
                    "reason": check.reason,
                }
                for check in self.checks
            ],
        }


def _pass(key: str, label: str, reason: str) -> AcceptPrecondition:
    return AcceptPrecondition(key=key, label=label, status="pass", reason=reason)


def _fail(key: str, label: str, reason: str) -> AcceptPrecondition:
    return AcceptPrecondition(key=key, label=label, status="fail", reason=reason)


@dataclass
class AcceptResult:
    """Outcome of safe candidate integration."""

    run_id: str
    accepted: bool
    target_repo: str
    final_branch: str
    final_sha: str
    summary: str
    gate_passed: bool


def _gate_passed(receipt: GateReceipt) -> bool:
    """Authoritative ``ProcessResult.passed`` equivalent.

    Delegates to :meth:`GateReceipt.passed` so the receipt alone is
    sufficient to make the acceptance verdict.  Preserves the
    deterministic ProcessResult.passed contract:

    * exit_code == 0
    * not timed_out
    * not launch_failed
    * not termination_incomplete
    * not evidence_persistence_failed
    * actual_scratch_backend != NOT_RUN
    """
    return receipt.passed()


# Gate-phase failure classifications.  They are SUBORDINATE to the
# rollback invariant: every classification receives the same exact
# CAS-safe rollback; the classification only shapes the surfaced
# diagnostic and the recorded failure event.
PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
GATE_FAILED = "GATE_FAILED"
INFRASTRUCTURE_OR_EVIDENCE_FAILURE = "INFRASTRUCTURE_OR_EVIDENCE_FAILURE"
UNEXPECTED_GATE_FAILURE = "UNEXPECTED_GATE_FAILURE"


@dataclass(frozen=True)
class _GatePhaseFailure:
    """Internal typed carrier for ONE post-CAS gate-phase failure.

    The gate phase RETURNS this instead of propagating raw exceptions,
    so there is exactly ONE failure boundary
    (:meth:`AcceptEngine._fail_closed_after_cas`): it records the
    failure evidence best-effort and then ALWAYS attempts the exact
    CAS-safe rollback exactly once.  No failure kind may bypass it.
    """

    classification: str
    # Present when the Gate Runner did produce a receipt (failed verdict,
    # receipt-event persistence failure, or post-gate mutation).
    receipt: GateReceipt | None = None
    preflight_error: GateScratchPreflightError | None = None
    infra_error: GateRunnerInfrastructureError | None = None
    unexpected_error: Exception | None = None
    gate_exit_code: int = -1
    # True when the receipt's durable ``gate_receipt`` event was already
    # recorded before this failure surfaced; the boundary never
    # duplicates it.
    receipt_event_recorded: bool = False


def _select_final_candidate(attempts: list[Attempt]) -> Attempt | None:
    """Return the final successful candidate attempt, or None.

    Shared deterministic precondition logic: both the read-only
    projection (:meth:`AcceptEngine.check_accept_eligibility`) and the
    engine (:meth:`AcceptEngine.accept_run`) consume this helper, so
    the "which candidate" decision lives in exactly one place.  The
    latest successful attempt with a commit SHA wins.
    """
    successful = [a for a in attempts if a.status == AttemptStatus.SUCCESS and a.commit_sha]
    if not successful:
        return None
    return successful[-1]


def _lineage_base_sha(attempts: list[Attempt], final_attempt: Attempt) -> str | None:
    """Return the lineage base SHA for provenance of ``final_attempt``.

    Shared with the same consumers as :func:`_select_final_candidate`:
    the most recent commit SHA from an earlier attempt number, if any.
    """
    return next(
        (
            prior.commit_sha
            for prior in reversed(attempts)
            if prior.attempt_number < final_attempt.attempt_number and prior.commit_sha
        ),
        None,
    )


class AcceptEngine:
    """Safely merges and verifies completed candidate runs into target repository."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    def _resolve_review_required_from_persisted_run(self, run_id: str) -> bool:
        """Return whether independent review is required for ``run_id``.

        Authority precedence:

        1. C13-F frozen decision JSON on the run row (when present and
           valid).  Malformed persisted decisions fall through to (2)
           but conservatively require review -- the classifier will
           produce ``required=True`` for any non-trivial change anyway.
        2. Legacy ``review_enabled`` boolean in the persisted config
           JSON.  Reconstructs via :func:`reconstruct_run_config` for
           one canonical path; reconstruction never invents a skip.

        This is the canonical helper every accept-side decision must use
        -- the post-CAS verdict / gate / publication checks remain
        unchanged.
        """
        run = self.db.get_run(run_id)
        if run is None:
            return False
        decision_json = self.db.get_review_risk_decision_json(run_id)
        if decision_json is not None:
            from saberops.review_adaptive import (
                ReviewDecision,
                ReviewPolicyError,
                validate_project_review_policy_invariants,
            )

            try:
                decision = ReviewDecision.from_json(decision_json)
                validate_project_review_policy_invariants(decision)
                return bool(decision.independent_review_required)
            except ReviewPolicyError:
                # Malformed persisted decision: conservative.
                return True
        # Legacy / partial / never-classified run: use the frozen
        # ``review_enabled`` boolean from the persisted config.
        #
        # Pre-C13-F invariant: a malformed legacy persisted config
        # caused acceptance to hard-refuse with a RuntimeError
        # ("Cannot accept run '<id>': persisted run configuration is
        # invalid").  That fail-closed law is restored here: silently
        # coercing corrupt persisted config to ``review_enabled=False``
        # would make the accept engine treat an unreadable authority
        # marker as if no review was required, which would silently
        # weaken what the engine is about to execute.  Valid legacy
        # configs (``review_enabled=True`` -> required,
        # ``review_enabled=False`` -> not required) preserve their
        # pre-C13-F behavior exactly.
        if not run.config_json:
            return False
        try:
            parsed = json.loads(run.config_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot accept run '{run_id}': persisted run configuration "
                f"is invalid: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"Cannot accept run '{run_id}': persisted run configuration "
                f"is invalid: config must be a JSON object, got "
                f"{type(parsed).__name__}"
            )
        if "review_enabled" not in parsed:
            return False
        value = parsed["review_enabled"]
        if not isinstance(value, bool):
            raise RuntimeError(
                f"Cannot accept run '{run_id}': persisted run configuration "
                f"is invalid: review_enabled must be a boolean, got "
                f"{type(value).__name__}"
            )
        return value

    @staticmethod
    def _gate_evidence_dir(target_repo: Path) -> Path:
        """Return the durable evidence directory for a gate run against ``target_repo``.

        Lives inside the project state directory bound to the target
        repository, so gate receipts and log references survive process
        restarts and never depend on tmpfs or any RAM scratch the runner
        may have used during the gate command itself.
        """
        from saberops.project import (
            ProjectIdentity,
            bind_project_state_dir,
            compute_project_identity,
        )

        identity = compute_project_identity(target_repo)
        if not isinstance(identity, ProjectIdentity):
            # Defensive: ``compute_project_identity`` raises on failure; we
            # never reach this branch.  Kept so the type checker stays happy.
            raise ProvenanceError(
                f"Cannot resolve project identity for {target_repo}"
            )
        state_dir = bind_project_state_dir(identity)
        return state_dir / "gate-evidence" / "candidates"

    @staticmethod
    def _frozen_gate_scratch_mode(run: Run) -> GateScratchMode:
        """Return the gate-scratch mode frozen into the run's config JSON.

        Strict semantics for acceptance:

        * absent field (no key in config JSON, empty config JSON,
          legacy run rows without ``gate_scratch_mode``) -> DISK.
        * present and valid -> exact persisted mode.
        * PRESENT BUT INVALID (non-string, unknown enum value,
          malformed JSON, non-object root) -> fail closed.  Silently
          re-mapping an invalid persisted owner decision to DISK would
          silently weaken what the engine is about to execute.

        ``reconstruct_run_config`` deliberately defaults a missing
        field to DISK (legacy contract for retry/repair/review).
        Acceptance is stricter: it requires either an absent field
        (default DISK) or a syntactically + semantically valid value.
        """
        raw = run.config_json
        # Field absent (no config JSON at all) -> DISK legacy default.
        if not raw:
            return GateScratchMode.DISK
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            # PRESENT BUT INVALID (config JSON exists but is malformed).
            # Fail closed; do NOT silently repair.
            raise RuntimeError(
                f"Cannot accept run '{run.id}': persisted run configuration "
                f"JSON is malformed: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Cannot accept run '{run.id}': persisted run configuration "
                f"must be a JSON object, got {type(data).__name__}"
            )
        if "gate_scratch_mode" not in data:
            # Field absent -> DISK legacy default.
            return GateScratchMode.DISK
        raw_mode = data["gate_scratch_mode"]
        if raw_mode is None:
            # PRESENT BUT INVALID: explicit null is not a recognized
            # owner decision.
            raise RuntimeError(
                f"Cannot accept run '{run.id}': persisted gate_scratch_mode "
                f"is null; must be one of DISK / RAM_PREFERRED / RAM_REQUIRED"
            )
        if not isinstance(raw_mode, str):
            raise RuntimeError(
                f"Cannot accept run '{run.id}': persisted gate_scratch_mode "
                f"must be a string, got {type(raw_mode).__name__}"
            )
        try:
            return GateScratchMode(raw_mode.strip())
        except ValueError as exc:
            raise RuntimeError(
                f"Cannot accept run '{run.id}': persisted gate_scratch_mode "
                f"{raw_mode!r} is not a recognized mode"
            ) from exc

    def _record_gate_receipt_event(
        self,
        run_id: str,
        receipt: GateReceipt | None,
        preflight_error: GateScratchPreflightError | None = None,
    ) -> None:
        """Persist the Gate Runner receipt on the run's durable event stream.

        Receipts survive rollback, raised acceptance failure, and
        ``RAM_REQUIRED`` preflight block: the durable event payload is
        written BEFORE any acceptance decision consumes the receipt.
        When the runner raises ``GateScratchPreflightError`` without a
        receipt, the typed failure is recorded so the event stream
        still carries the preflight-block evidence.
        """
        if receipt is not None:
            self.db.record_event(
                run_id,
                "gate_receipt",
                payload=receipt.as_dict(),
            )
        elif preflight_error is not None:
            self.db.record_event(
                run_id,
                "gate_preflight_blocked",
                payload={
                    "reason": str(preflight_error),
                    "evidence_dir": str(self._gate_evidence_dir_for(run_id)),
                },
            )

    def _record_runner_infra_event(
        self,
        run_id: str,
        exc: GateRunnerInfrastructureError,
    ) -> None:
        """Persist a typed Gate Runner infrastructure/evidence failure.

        Evidence integrity is uncertain; the original error must
        remain visible after rollback handling.
        """
        self.db.record_event(
            run_id,
            "gate_runner_infrastructure_error",
            payload={
                "reason": str(exc),
                "evidence_dir": str(self._gate_evidence_dir_for(run_id)),
                "exception_type": type(exc).__name__,
            },
        )

    def _record_runner_unexpected_event(
        self,
        run_id: str,
        exc: Exception,
    ) -> None:
        """Persist an unexpected Gate Runner exception.

        The original error must remain visible after rollback handling.
        """
        self.db.record_event(
            run_id,
            "gate_runner_unexpected_exception",
            payload={
                "reason": str(exc),
                "evidence_dir": str(self._gate_evidence_dir_for(run_id)),
                "exception_type": type(exc).__name__,
            },
        )

    def _gate_evidence_dir_for(self, run_id: str) -> Path:
        """Return the canonical evidence directory used for ``run_id``.

        Reads the run's persisted target repo so the evidence path is
        computed against the same identity the runner used.
        """
        run = self.db.get_run(run_id)
        if run is None:
            raise ValueError(f"Run '{run_id}' not found")
        target_repo = assert_run_repository(run)
        return self._gate_evidence_dir(target_repo)

    def _rollback_after_gate_failure(
        self,
        target_repo: Path,
        base_commit: str,
        *,
        expected_current_sha: str,
        gate_command: str,
        gate_exit_code: int,
        preflight_message: str | None = None,
        infra_message: str | None = None,
        unexpected_message: str | None = None,
        event_error_message: str | None = None,
    ) -> NoReturn:
        """Roll back to ``base_commit`` after a gate-phase failure, then raise.

        This method ALWAYS raises a ``RuntimeError``: rollback is a
        fail-closed acceptance outcome that the caller MUST propagate.
        The CAS-safe rollback doctrine is preserved exactly:
        ``expected_current_sha`` is the candidate SHA this acceptance
        invocation installed with its own compare-and-swap; the
        rollback never derives the expected-old from a fresh HEAD read
        after the gate (which would authorize deleting another writer's
        revision), and an external writer that advanced the target
        during the gate is preserved (rollback/ref-race law).

        Exactly one classification channel is populated, mapping to the
        four mandated classifications:

        * ``preflight_message``  -> ``PREFLIGHT_BLOCKED`` (typed
          pre-execution block, no subprocess launched).
        * ``infra_message``      -> ``INFRASTRUCTURE_OR_EVIDENCE_FAILURE``
          (Gate Runner evidence / I/O / resolver failure; gate may have
          run).
        * ``unexpected_message`` -> ``UNEXPECTED_GATE_FAILURE``
          (unexpected non-typed failure; gate may or may not have run).
        * none of the above      -> ``GATE_FAILED`` (legacy
          ``ProcessResult`` exit-code path).

        ``event_error_message``, when set, carries a CAPTURED
        failure-evidence event persistence error.  It is appended to the
        surfaced diagnostic so both facts survive -- the primary gate
        failure AND the evidence-recording failure -- but it can never
        prevent or skip the rollback itself.
        """
        reset_ok, head_after, is_clean_after, rollback_detail = self._rollback_to_base(
            target_repo,
            base_commit,
            expected_current_sha=expected_current_sha,
        )
        if preflight_message is not None:
            critical_core = f"Pre-gate preflight blocked ({preflight_message})"
            ok_message = (
                f"Post-integration gate '{gate_command}' did not run: "
                f"preflight blocked: {preflight_message}. "
                f"Target repository has been rolled back to {base_commit[:8]}."
            )
        elif infra_message is not None:
            critical_core = (
                f"Gate Runner infrastructure/evidence failure ({infra_message})"
            )
            ok_message = (
                f"Gate Runner infrastructure/evidence failure for "
                f"'{gate_command}': {infra_message}. "
                f"Target repository has been rolled back to {base_commit[:8]}."
            )
        elif unexpected_message is not None:
            critical_core = (
                f"Gate Runner raised unexpected exception ({unexpected_message})"
            )
            ok_message = (
                f"Gate Runner raised unexpected exception for "
                f"'{gate_command}': {unexpected_message}. "
                f"Target repository has been rolled back to {base_commit[:8]}."
            )
        else:
            critical_core = (
                f"Post-integration gate '{gate_command}' failed "
                f"(exit code {gate_exit_code})"
            )
            ok_message = (
                f"Post-integration gate '{gate_command}' failed "
                f"(exit code {gate_exit_code}). "
                f"Target repository has been rolled back to {base_commit[:8]}."
            )
        if event_error_message is not None:
            critical_core += (
                "; failure-evidence event persistence also failed: "
                f"{event_error_message}"
            )
            ok_message += (
                " Failure-evidence event persistence also failed: "
                f"{event_error_message}."
            )
        if not reset_ok or head_after != base_commit or not is_clean_after:
            raise RuntimeError(
                f"CRITICAL: {critical_core} AND rollback to base commit "
                f"{base_commit[:8]} is incomplete (reset_ok={reset_ok}, "
                f"HEAD={head_after[:8]}, clean={is_clean_after}). "
                f"{rollback_detail} "
                "Manual intervention required."
            )
        raise RuntimeError(ok_message)

    def _rollback_to_base(
        self,
        target_repo: Path,
        base_commit: str,
        *,
        expected_current_sha: str,
    ) -> tuple[bool, str, bool, str]:
        """CAS the target ref candidate->base, then sync index/worktree.

        ``expected_current_sha`` MUST be the exact candidate SHA *this*
        AcceptEngine invocation installed with its own compare-and-swap.
        The rollback therefore never derives its expected-old value from a
        fresh HEAD read after the gate (which would explicitly authorize
        deleting another writer's revision), and the post-CAS
        synchronization never moves the branch ref again: if an external
        writer advanced the target during the gate or inside the
        CAS-to-sync window, the compare-and-swap fails closed, the
        external revision is preserved, and no rollback occurs.

        Returns ``(ok, head, clean, detail)``; ``detail`` carries a
        fail-closed diagnostic whenever ``ok`` is False.
        """
        branch = GitManager.get_current_branch(target_repo)
        ref = f"refs/heads/{branch}" if branch else "HEAD"
        try:
            GitManager.compare_and_swap_ref(
                target_repo,
                ref,
                expected_old_sha=expected_current_sha,
                new_target_sha=base_commit,
            )
        except GitRefRaceError as exc:
            head = GitManager.get_head_commit(target_repo)
            clean = GitManager.is_clean(target_repo)
            return (
                False,
                head,
                clean,
                "rollback/ref race: target ref was no longer the exact "
                f"candidate {expected_current_sha[:8]} installed by this "
                f"acceptance ({exc}); the externally advanced revision is "
                "preserved, no rollback was performed, and manual "
                "reconciliation is required",
            )
        # Index/worktree synchronization from the already-CASed ref: this
        # Git primitive never performs another branch-ref update, so the
        # ref contract above holds even if another writer advances the
        # branch inside this window.
        try:
            GitManager.sync_index_worktree(target_repo)
        except RuntimeError as exc:
            head = GitManager.get_head_commit(target_repo)
            clean = GitManager.is_clean(target_repo)
            return (
                False,
                head,
                clean,
                f"rollback worktree synchronization failed without any "
                f"further ref mutation: {exc}",
            )
        head_after = GitManager.get_head_commit(target_repo)
        is_clean_after = GitManager.is_clean(target_repo)
        ok = head_after == base_commit and is_clean_after
        detail = (
            ""
            if ok
            else (
                "post-rollback verification failed "
                f"(HEAD={head_after[:8]}, clean={is_clean_after}); the "
                "target branch ref was not modified again after the "
                "compare-and-swap"
            )
        )
        return ok, head_after, is_clean_after, detail

    def _execute_gate_phase(
        self,
        run_id: str,
        runner: GateRunner,
        request: GateRunnerRequest,
        *,
        post_gate_sha_resolver: Callable[[], tuple[str, bool]],
    ) -> GateReceipt | _GatePhaseFailure:
        """Execute the authoritative gate exactly once and verify the outcome.

        Returns the verified receipt, OR a ``_GatePhaseFailure`` carrying
        everything the ONE centralized post-CAS boundary needs.  This
        method is the only place gate outcomes are mapped to the four
        failure classifications; the rollback itself lives only in
        ``_fail_closed_after_cas``.  Classification is subordinate to
        the rollback invariant: every failure outcome below is returned
        to the same single boundary.
        """
        try:
            receipt: GateReceipt | None = runner.run(
                request, post_gate_sha_resolver=post_gate_sha_resolver
            )
        except GateScratchPreflightError as exc:
            return _GatePhaseFailure(
                classification=PREFLIGHT_BLOCKED, preflight_error=exc
            )
        except GateRunnerInfrastructureError as exc:
            return _GatePhaseFailure(
                classification=INFRASTRUCTURE_OR_EVIDENCE_FAILURE, infra_error=exc
            )
        except Exception as exc:
            return _GatePhaseFailure(
                classification=UNEXPECTED_GATE_FAILURE, unexpected_error=exc
            )
        if receipt is None:
            # Defensive fail-closed: a runner that returns no receipt must
            # go through the centralized boundary like any other failure,
            # never bypass the CAS-safe rollback.
            return _GatePhaseFailure(
                classification=UNEXPECTED_GATE_FAILURE,
                unexpected_error=RuntimeError("Gate Runner returned no receipt"),
            )
        # Persist receipt-level evidence on the run's durable event stream
        # BEFORE the verdict consumes it.  If this write fails, the failure
        # is routed through the SAME centralized boundary: event
        # persistence must never become a reason the rollback is skipped.
        try:
            self._record_gate_receipt_event(run_id, receipt)
        except Exception as exc:
            return _GatePhaseFailure(
                classification=INFRASTRUCTURE_OR_EVIDENCE_FAILURE,
                receipt=receipt,
                infra_error=GateRunnerInfrastructureError(
                    f"gate receipt event persistence failed: {exc}"
                ),
                gate_exit_code=int(receipt.exit_code),
            )
        if not _gate_passed(receipt):
            return _GatePhaseFailure(
                classification=GATE_FAILED,
                receipt=receipt,
                gate_exit_code=int(receipt.exit_code),
                receipt_event_recorded=True,
            )
        # Guard against the gate moving HEAD or leaving the working tree
        # dirty on exit-0 "success".  The receipt's captured post-gate
        # evidence is authoritative -- the real GateRunner is always
        # given a ``post_gate_sha_resolver``, so a receipt that fails to
        # record the post-gate SHA is an unexpected gate-phase failure,
        # never a reason for a second live repository read.  Any
        # mismatch is a gate-phase failure like any other and reaches
        # the centralized boundary.
        if not receipt.post_gate_candidate_sha:
            return _GatePhaseFailure(
                classification=UNEXPECTED_GATE_FAILURE,
                receipt=receipt,
                unexpected_error=RuntimeError(
                    "gate receipt records no post-gate candidate SHA; "
                    "post-gate evidence is not authoritative"
                ),
                gate_exit_code=int(receipt.exit_code),
                receipt_event_recorded=True,
            )
        head_after_gate = receipt.post_gate_candidate_sha
        if (
            head_after_gate != request.expected_post_gate_sha
            or not receipt.post_gate_worktree_clean
        ):
            return _GatePhaseFailure(
                classification=UNEXPECTED_GATE_FAILURE,
                receipt=receipt,
                unexpected_error=RuntimeError(
                    f"gate exited 0 but unexpectedly mutated the target "
                    f"repository (HEAD={head_after_gate[:8]}, "
                    f"clean={receipt.post_gate_worktree_clean})"
                ),
                gate_exit_code=int(receipt.exit_code),
                receipt_event_recorded=True,
            )
        return receipt

    def _fail_closed_after_cas(
        self,
        run_id: str,
        target_repo: Path,
        base_commit: str,
        *,
        candidate_sha: str,
        gate_command: str,
        failure: _GatePhaseFailure,
    ) -> NoReturn:
        """THE one post-CAS gate-phase failure boundary.

        EVERY gate-phase failure after the candidate CAS arrives here:
        preflight blocks, ``GateRunnerInfrastructureError``, unexpected
        runner exceptions, ``None`` receipts, gate receipt/event
        persistence failures, failed gate verdicts, and post-gate
        state-resolution failures.  The invariant is structural:

            candidate installed
            -> gate phase
            -> either a successful verified gate
               OR the exact CAS-safe rollback attempted exactly once

        Failure-evidence events are recorded BEST-EFFORT and their
        failures are captured and surfaced in the rollback diagnostic --
        event persistence must never prevent or skip the rollback.
        """
        event_error: Exception | None = None

        def _best_effort(record: Callable[[], None]) -> None:
            nonlocal event_error
            try:
                record()
            except Exception as exc:
                # Preserve the first evidence-recording failure so both
                # facts survive (primary failure + event failure); never
                # let it block the rollback below.
                if event_error is None:
                    event_error = exc

        receipt = failure.receipt
        if receipt is not None and not failure.receipt_event_recorded:
            _best_effort(lambda: self._record_gate_receipt_event(run_id, receipt))
        if failure.preflight_error is not None:
            preflight_error = failure.preflight_error
            _best_effort(
                lambda: self._record_gate_receipt_event(run_id, None, preflight_error)
            )
        elif failure.infra_error is not None:
            infra_error = failure.infra_error
            _best_effort(lambda: self._record_runner_infra_event(run_id, infra_error))
        elif failure.unexpected_error is not None:
            unexpected_error = failure.unexpected_error
            _best_effort(
                lambda: self._record_runner_unexpected_event(run_id, unexpected_error)
            )

        self._rollback_after_gate_failure(
            target_repo,
            base_commit,
            expected_current_sha=candidate_sha,
            gate_command=gate_command,
            gate_exit_code=failure.gate_exit_code,
            preflight_message=(
                str(failure.preflight_error) if failure.preflight_error else None
            ),
            infra_message=str(failure.infra_error) if failure.infra_error else None,
            unexpected_message=(
                str(failure.unexpected_error) if failure.unexpected_error else None
            ),
            event_error_message=str(event_error) if event_error is not None else None,
        )

    def check_accept_eligibility(self, run_id: str) -> AcceptEligibility:
        """Project whether acceptance preconditions are currently satisfied, read-only.

        Every blocking check mirrors a precondition enforced by
        :meth:`accept_run` before (or atomically at) the candidate
        compare-and-swap -- the Web renders the returned checks as a
        checklist and gates the Accept button on :attr:`eligible`,
        so acceptance policy lives in exactly one place.  There are
        no projection-only blockers.

        No repository mutation, no gate execution, and no event writes
        happen here.  Anything unreadable fails closed: the check
        reports ``fail`` and the candidate is ineligible.

        Inherently dynamic execution outcomes are NOT static
        eligibility promises and are therefore not modeled here: a
        later CAS race, a post-integration gate failure,
        evidence-directory I/O failure, or infrastructure failure
        occurring after the owner clicks Accept.  Acceptance
        revalidates state and still runs the post-integration gate.
        """
        checks: list[AcceptPrecondition] = []

        run = self.db.get_run(run_id)
        if run is None:
            checks.append(_fail("run_completed", "Run completed", f"Run '{run_id}' not found"))
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        if run.status != RunStatus.COMPLETED:
            checks.append(
                _fail(
                    "run_completed",
                    "Run completed",
                    f"Status is {run.status.value}, expected COMPLETED",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        checks.append(_pass("run_completed", "Run completed", "Status is COMPLETED"))

        attempts = self.db.get_attempts_for_run(run_id)
        final_attempt = _select_final_candidate(attempts)
        if final_attempt is None:
            checks.append(
                _fail(
                    "candidate_available",
                    "Candidate available",
                    "Run has no successful candidate commit",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        candidate_sha = final_attempt.commit_sha or ""
        checks.append(
            _pass(
                "candidate_available",
                "Candidate available",
                f"Candidate commit {candidate_sha[:8]} from attempt "
                f"#{final_attempt.attempt_number}",
            )
        )

        # Provenance: same assertion the engine executes, read-only, with
        # the same shared lineage-base helper.
        try:
            provenance = assert_candidate_provenance(
                run,
                final_attempt,
                lineage_base_sha=_lineage_base_sha(attempts, final_attempt),
                extra_authorized_bases=failover_authorized_bases(self.db, run_id),
            )
            candidate_sha = provenance.candidate_sha
            candidate_branch = final_attempt.branch_name
            checks.append(
                _pass(
                    "candidate_provenance",
                    "Candidate belongs to this run",
                    f"Candidate {candidate_sha[:8]} is provably a product of this run",
                )
            )
        except ProvenanceError as exc:
            checks.append(
                _fail("candidate_provenance", "Candidate belongs to this run", str(exc))
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))

        # Owner rejection: a canonical candidate_rejected event for the
        # exact current candidate blocks acceptance.  Uses the SAME
        # shared helper as the lifecycle projection and the Reject
        # action, so "rejected" has exactly one definition.  The Web
        # never invents this blocker; it renders this checklist.
        if find_matching_rejection_event(self.db, run_id, candidate_sha) is not None:
            checks.append(
                _fail(
                    "candidate_not_rejected",
                    "Candidate not rejected",
                    f"Candidate {candidate_sha[:8]} was rejected by the owner "
                    "(candidate_rejected event); re-run to produce a new candidate.",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        checks.append(
            _pass(
                "candidate_not_rejected",
                "Candidate not rejected",
                "No owner rejection recorded for this candidate",
            )
        )

        # Target repository: derived from the run row, never the UI selection.
        try:
            target_repo = assert_run_repository(run).resolve()
            checks.append(
                _pass(
                    "target_repository",
                    "Target repository resolved",
                    f"Target repository is {target_repo}",
                )
            )
        except ProvenanceError as exc:
            checks.append(
                _fail("target_repository", "Target repository resolved", str(exc))
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))

        # Target must be clean.
        try:
            clean = GitManager.is_clean(target_repo)
        except Exception as exc:
            checks.append(
                _fail("target_clean", "Target repository clean", f"Unreadable: {exc}")
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        if not clean:
            checks.append(
                _fail(
                    "target_clean",
                    "Target repository clean",
                    f"Target repository '{target_repo}' has uncommitted or "
                    "untracked changes. Working tree must be clean before accept.",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        checks.append(
            _pass("target_clean", "Target repository clean", "Working tree is clean")
        )

        # Target HEAD must still equal the candidate base (no rebasing here; R4).
        try:
            current_head = GitManager.get_head_commit(target_repo)
        except Exception as exc:
            checks.append(
                _fail(
                    "target_head",
                    "Candidate based on current HEAD",
                    f"Target HEAD unreadable: {exc}",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        if current_head != run.base_commit:
            checks.append(
                _fail(
                    "target_head",
                    "Candidate based on current HEAD",
                    f"Target HEAD moved since this candidate was created "
                    f"(HEAD {current_head[:8]}, base {run.base_commit[:8]}). "
                    "Refusing accept.",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        checks.append(
            _pass(
                "target_head",
                "Candidate based on current HEAD",
                f"Target HEAD equals candidate base {run.base_commit[:8]}",
            )
        )

        # Candidate ref, when present, must name the candidate SHA.
        try:
            branch_commit = GitManager.get_ref_commit(target_repo, candidate_branch)
        except Exception as exc:
            checks.append(
                _fail(
                    "candidate_ref",
                    "Candidate ref consistent",
                    f"Candidate ref unreadable: {exc}",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        if branch_commit is not None and branch_commit != candidate_sha:
            checks.append(
                _fail(
                    "candidate_ref",
                    "Candidate ref consistent",
                    f"Candidate branch '{candidate_branch}' points to "
                    f"{branch_commit[:8]}, expected candidate commit "
                    f"{candidate_sha[:8]}.",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        checks.append(
            _pass(
                "candidate_ref",
                "Candidate ref consistent",
                "Candidate ref names the candidate commit"
                if branch_commit is not None
                else "No candidate ref to reconcile",
            )
        )

        # Deterministic gate evidence: the candidate attempt succeeded, which
        # is only recorded after its run-time gate passed.  The
        # post-integration gate itself still runs inside accept_run.
        checks.append(
            _pass(
                "deterministic_gate",
                "Deterministic gate passed",
                f"Attempt #{final_attempt.attempt_number} succeeded after its gate",
            )
        )

        # Review rule, exactly as the engine enforces it: required review
        # must exist; any present non-substantive verdict blocks; no review
        # when none is required is valid; PASS and PASS_WITH_BACKLOG approve.
        try:
            review_required = self._resolve_review_required_from_persisted_run(run_id)
        except RuntimeError as exc:
            checks.append(
                _fail("review", "Review requirement satisfied", str(exc))
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        latest_review = self.db.get_latest_review(run_id)
        if latest_review is None and review_required:
            checks.append(
                _fail(
                    "review",
                    "Review requirement satisfied",
                    "Independent review is required by the persisted run "
                    "configuration but no review exists",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        if latest_review is not None and latest_review.verdict not in (
            ReviewVerdict.PASS,
            ReviewVerdict.PASS_WITH_BACKLOG,
        ):
            checks.append(
                _fail(
                    "review",
                    "Review requirement satisfied",
                    f"Independent review verdict is "
                    f"{latest_review.verdict.value}, not a substantive approval: "
                    f"{latest_review.summary}",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        if latest_review is None:
            checks.append(
                _pass(
                    "review",
                    "Review requirement satisfied",
                    "Review not required by policy and none exists",
                )
            )
        else:
            checks.append(
                _pass(
                    "review",
                    "Review requirement satisfied",
                    f"Review verdict {latest_review.verdict.value} is a "
                    "substantive approval",
                )
            )

        # Candidate must fast-forward from the target HEAD.
        try:
            descendant = GitManager.is_ancestor(target_repo, current_head, candidate_sha)
        except Exception as exc:
            checks.append(
                _fail(
                    "fast_forward",
                    "Candidate fast-forwards target",
                    f"Ancestry unreadable: {exc}",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        if not descendant:
            checks.append(
                _fail(
                    "fast_forward",
                    "Candidate fast-forwards target",
                    f"Candidate {candidate_sha[:8]} is not a fast-forward "
                    f"descendant of target {current_head[:8]}",
                )
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        checks.append(
            _pass(
                "fast_forward",
                "Candidate fast-forwards target",
                f"Candidate {candidate_sha[:8]} descends from target "
                f"{current_head[:8]}",
            )
        )

        # Frozen gate-scratch authority must be absent (legacy DISK default)
        # or syntactically valid; present-but-invalid fails closed.
        try:
            frozen_mode = self._frozen_gate_scratch_mode(run)
        except RuntimeError as exc:
            checks.append(
                _fail("gate_scratch", "Gate scratch policy valid", str(exc))
            )
            return AcceptEligibility(run_id=run_id, eligible=False, checks=tuple(checks))
        checks.append(
            _pass(
                "gate_scratch",
                "Gate scratch policy valid",
                f"Frozen gate scratch mode is {frozen_mode.value}",
            )
        )

        # R0-B.1 parity: there is deliberately NO "accepted only once"
        # projection blocker here.  accept_run() enforces no such
        # pre-CAS policy, so a prior run_accepted event must not make
        # the projection ineligible -- the preserved pre-R0 acceptance
        # policy is authoritative and candidate lifecycle /
        # accepted-state policy belongs to R3.  Prior acceptance
        # remains visible in the run event stream; it is informational,
        # never a static eligibility blocker.

        return AcceptEligibility(run_id=run_id, eligible=True, checks=tuple(checks))

    def accept_run(self, run_id: str, gate_timeout: float = 300.0) -> AcceptResult:
        """Safely fast-forward integrate a completed candidate run and verify canonical gate.

        Pre-R0 acceptance policy is preserved: there is no
        "accepted only once" engine precondition here.  Candidate
        lifecycle / accepted-state policy belongs to R3.
        """
        run = self.db.get_run(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")

        if run.status != RunStatus.COMPLETED:
            raise RuntimeError(
                f"Cannot accept run '{run_id}': status is {run.status.value}, expected COMPLETED"
            )

        attempts = self.db.get_attempts_for_run(run_id)
        final_attempt = _select_final_candidate(attempts)
        if final_attempt is None:
            raise RuntimeError(f"Run '{run_id}' has no successful candidate commit")

        # Fail closed unless this candidate is provably a product of *this*
        # run and *this* objective.  Without it, a candidate from an unrelated
        # run could be integrated as if it satisfied the failed objective.
        try:
            provenance = assert_candidate_provenance(
                run,
                final_attempt,
                lineage_base_sha=_lineage_base_sha(attempts, final_attempt),
                # C11-D: a failover-legitimate final attempt based at
                # its validated required-base anchor (re-verified live
                # against durable decision evidence) remains provably
                # a candidate of this run.
                extra_authorized_bases=failover_authorized_bases(self.db, run_id),
            )
        except ProvenanceError as exc:
            raise RuntimeError(f"Cannot accept run '{run_id}': {exc}") from exc

        candidate_sha = provenance.candidate_sha
        candidate_branch = final_attempt.branch_name

        # A rejected current candidate must not remain acceptable.  Same
        # shared helper as the eligibility projection above: enforced
        # here before any Git mutation, so direct service/engine
        # acceptance after rejection fails with the target HEAD
        # untouched and no run_accepted event added.
        if find_matching_rejection_event(self.db, run_id, candidate_sha) is not None:
            raise RuntimeError(
                f"Cannot accept run '{run_id}': candidate {candidate_sha[:8]} "
                "was rejected by the owner (candidate_rejected event)."
            )

        # The repository is derived from the run's own persisted identity, never
        # from the UI's currently selected project.
        try:
            target_repo = assert_run_repository(run).resolve()
        except ProvenanceError as exc:
            raise RuntimeError(f"Cannot accept run '{run_id}': {exc}") from exc

        # Target repository must be clean
        if not GitManager.is_clean(target_repo):
            raise RuntimeError(
                f"Target repository '{target_repo}' has uncommitted or untracked changes. "
                "Working tree must be clean before accept."
            )

        # Target repository HEAD must match the run's base_commit
        current_head = GitManager.get_head_commit(target_repo)
        if current_head != run.base_commit:
            raise RuntimeError(
                f"Target repository HEAD ({current_head[:8]}) has moved from recorded base commit "
                f"({run.base_commit[:8]}). Refusing accept."
            )

        # Check candidate branch pointer if ref exists
        branch_commit = GitManager.get_ref_commit(target_repo, candidate_branch)
        if branch_commit is not None and branch_commit != candidate_sha:
            raise RuntimeError(
                f"Candidate branch '{candidate_branch}' points to {branch_commit[:8]}, "
                f"expected candidate commit {candidate_sha[:8]}."
            )

        # The persisted run configuration decides whether a review is required.
        # C13-F delegates the answer to the canonical helper
        # :meth:`_resolve_review_required_from_persisted_run` which
        # consults the frozen risk-adaptive decision when present and
        # falls back to the legacy ``review_enabled`` boolean otherwise.
        # The helper fails closed on malformed persisted decisions and
        # never silently invents a skip.  A required review must exist
        # and be substantive; any present non-substantive verdict
        # remains a hard rejection for legacy safety.
        review_required = self._resolve_review_required_from_persisted_run(run_id)
        latest_review = self.db.get_latest_review(run_id)
        if latest_review is None and review_required:
            raise RuntimeError(
                f"Cannot accept run '{run_id}': independent review is required "
                "by the persisted run configuration but no review exists"
            )
        if latest_review is not None and latest_review.verdict not in (
            ReviewVerdict.PASS,
            ReviewVerdict.PASS_WITH_BACKLOG,
        ):
            raise RuntimeError(
                f"Cannot accept run '{run_id}': independent review verdict is "
                f"{latest_review.verdict.value}, not a substantive approval: "
                f"{latest_review.summary}"
            )

        if not GitManager.is_ancestor(target_repo, current_head, candidate_sha):
            raise RuntimeError(
                f"Cannot accept run '{run_id}': candidate {candidate_sha[:8]} "
                f"is not a fast-forward descendant of target {current_head[:8]}"
            )

        # Validate the persisted frozen gate-scratch mode BEFORE the
        # candidate CAS.  The frozen mode is part of the run's
        # authoritative authority; an absent field defaults to DISK
        # (legacy contract) but a PRESENT-but-invalid value MUST fail
        # closed here, before any target HEAD mutation.  Silently
        # repairing a corrupted owner decision would silently weaken
        # what the engine is about to execute.
        frozen_mode = self._frozen_gate_scratch_mode(run)

        # ---- PRE-CAS PREPARATION ------------------------------------
        # Everything that can reasonably be resolved WITHOUT mutating the
        # target HEAD happens BEFORE the candidate CAS: the frozen
        # scratch policy above, the durable gate-evidence directory, and
        # the deterministic GateRunner inputs below.  A failure here
        # fails acceptance with target HEAD still at base and zero gate
        # invocations -- no rollback and no partially installed
        # candidate can ever be involved.
        try:
            evidence_dir = self._gate_evidence_dir(target_repo)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot accept run '{run_id}': cannot resolve the durable "
                f"gate evidence directory: {exc}"
            ) from exc
        try:
            # Creating/validating the evidence directory is a project
            # state-dir mutation OUTSIDE the target repository; doing it
            # pre-CAS means an evidence-dir failure can never leave a
            # half-installed candidate behind.
            evidence_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot accept run '{run_id}': cannot prepare the durable "
                f"gate evidence directory at {evidence_dir}: {exc}"
            ) from exc
        runner = GateRunner()
        runner_request = GateRunnerRequest(
            target_repo=target_repo,
            gate_command=run.gate_command,
            timeout=gate_timeout,
            frozen_gate_scratch_mode=frozen_mode,
            candidate_sha=candidate_sha,
            # The CAS below installs exactly ``candidate_sha`` and fails
            # closed if the post-CAS HEAD is anything else, so the
            # expected post-gate SHA is exactly the candidate SHA.
            expected_post_gate_sha=candidate_sha,
            evidence_dir=evidence_dir,
            log_digest_label=f"gate-{run_id}",
        )

        # Integrate through GitManager's expected-old/new-target CAS primitive.
        # A check followed by an ordinary merge would allow another process to
        # advance the target between inspection and mutation.
        #
        # The compare-and-swap below is the ONE branch-ref mutation of
        # acceptance (base -> candidate, expected-old = run.base_commit).
        # Everything after it synchronizes the index/worktree WITHOUT moving
        # the branch ref again, so an external writer that advances the
        # target inside the CAS-to-sync window is never overwritten by a
        # later unconditional ref move: the post-sync HEAD check fails
        # closed instead, preserving the external revision.
        target_branch = GitManager.get_current_branch(target_repo)
        target_ref = f"refs/heads/{target_branch}" if target_branch else "HEAD"
        try:
            GitManager.compare_and_swap_ref(
                target_repo,
                target_ref,
                expected_old_sha=run.base_commit,
                new_target_sha=candidate_sha,
            )
            GitManager.sync_index_worktree(target_repo)
            new_sha = GitManager.get_head_commit(target_repo)
            if new_sha != candidate_sha:
                raise GitRefRaceError(
                    "target ref changed after compare-and-swap; the "
                    "externally advanced revision is preserved and "
                    "acceptance fails closed"
                )
        except GitRefRaceError as exc:
            raise RuntimeError(f"Cannot accept run '{run_id}': target ref race: {exc}") from exc

        # ---- POST-CAS GATE PHASE: ONE centralized failure boundary ---
        # The candidate has been installed by CAS and the HEAD verified.
        # From here, EVERY gate-phase failure -- RAM preflight block,
        # GateRunnerInfrastructureError, unexpected runner exception,
        # malformed command, a runner returning None, gate receipt
        # recording failure, gate failure receipt, receipt/event
        # persistence failure, or post-gate state-resolution failure --
        # flows through exactly ONE boundary that attempts the exact
        # CAS-safe rollback (expected_current_sha == candidate_sha)
        # exactly once.  Event/observability persistence can never
        # become a reason the rollback is skipped.
        # ---- TOTAL POST-CAS FAILURE BOUNDARY -------------------------
        # The ENTIRE gate phase is protected: any ordinary Exception
        # raised anywhere inside `_execute_gate_phase` (including
        # defensive/verification logic after the runner returned) is
        # converted into the same typed failure and reaches the ONE
        # centralized rollback boundary.  By inspection:
        #
        #   candidate CAS-installed
        #     -> entire gate phase protected
        #     -> verified GateReceipt
        #        OR _fail_closed_after_cas(expected_current_sha=candidate_sha)
        #
        # There is no ordinary Exception path around it.  (BaseException,
        # KeyboardInterrupt and SystemExit are deliberately NOT caught.)
        try:
            outcome = self._execute_gate_phase(
                run_id,
                runner,
                runner_request,
                post_gate_sha_resolver=lambda: (
                    GitManager.get_head_commit(target_repo),
                    GitManager.is_clean(target_repo),
                ),
            )
        except Exception as exc:
            outcome = _GatePhaseFailure(
                classification=UNEXPECTED_GATE_FAILURE,
                unexpected_error=exc,
            )
        if isinstance(outcome, _GatePhaseFailure):
            self._fail_closed_after_cas(
                run_id,
                target_repo,
                run.base_commit,
                candidate_sha=candidate_sha,
                gate_command=run.gate_command,
                failure=outcome,
            )
        gate_receipt = outcome

        final_branch = GitManager.get_current_branch(target_repo) or "HEAD"
        summary = (
            f"Candidate commit {new_sha[:8]} successfully integrated into '{final_branch}'. "
            f"Post-integration gate '{run.gate_command}' PASSED."
        )

        self.db.record_event(
            run_id,
            "run_accepted",
            payload={
                "candidate_sha": new_sha,
                "final_branch": final_branch,
                "gate_command": run.gate_command,
                "provenance": provenance.as_dict(),
                "gate_receipt": gate_receipt.as_dict(),
            },
        )

        return AcceptResult(
            run_id=run_id,
            accepted=True,
            target_repo=str(target_repo),
            final_branch=final_branch,
            final_sha=new_sha,
            summary=summary,
            gate_passed=True,
        )
