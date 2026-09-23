"""RunSupervisor: durable, caller-independent execution ownership for one run.

A run's *execution owner* is the detached process that actually drives the
orchestration lifecycle.  It is launched into its own session with its standard
streams bound to the durable transcript file, so nothing about the caller that
asked for the run -- a browser, an HTTP request, the conversational manager, or
the initiating shell -- can terminate it or corrupt its output when it goes
away.  Caller disconnect is never cancellation; only
:meth:`RunSupervisor.cancel` terminates a worker.

Ownership itself is persisted in project-local SQLite (see
:mod:`saberops.ownership`), claimed atomically *before* any process is
launched, so two independent supervisor processes can never both execute the
same run.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from dataclasses import replace as _frozen_replace
from pathlib import Path
from typing import Any

from saberops.config import (
    RunConfig,
    freeze_owner_binding_registry_payload,
)
from saberops.db import Database, current_iso_timestamp
from saberops.git import _CANDIDATE_BRANCH_RE, GitManager, assert_repository_at_revision
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    Attempt,
    AttemptStatus,
    HealthState,
    ManagerState,
    Run,
    RunStatus,
    StartRunResult,
    SupervisionRecord,
    WorkerActivationState,
)
from saberops.ownership import (
    OWNER_ENV_GENERATION,
    OWNER_ENV_ID,
    ExecutionOwner,
    Liveness,
    OwnershipError,
    OwnershipStoreError,
    ProcessIdentity,
    probe_liveness,
    terminate_owner,
)
from saberops.project_scope.graph import build_project_graph
from saberops.project_scope.persistence import freeze_run_scope
from saberops.project_scope.resolver import resolve_scope
from saberops.provenance import (
    ProvenanceError,
    assert_run_not_retargeted,
    assert_run_repository,
    mint_run_provenance,
)
from saberops.routing import resolve_candidate_chain
from saberops.tooling import ToolRegistry, freeze_tool_contract
from saberops.workers.registry import AdapterRegistry


def assess_worker_health(
    *,
    process_alive: bool,
    terminal_event_exists: bool,
    recent_activity: bool,
    timed_out: bool = False,
    stalled: bool = False,
) -> tuple[HealthState, str]:
    """Apply the 300-second health rule without invoking a model.

    A PID alone deliberately cannot produce ``healthy``.  Callers provide
    verified transcript/event/provider activity evidence and persist the
    returned value before any manager suspension decision.
    """
    if terminal_event_exists:
        return HealthState.TERMINAL, "terminal_event_exists"
    if timed_out:
        return HealthState.STALLED, "known_timeout"
    if stalled:
        return HealthState.STALLED, "known_stall"
    if not process_alive:
        return HealthState.UNCERTAIN, "process_not_alive"
    if not recent_activity:
        return HealthState.UNCERTAIN, "pid_alive_without_verifiable_activity"
    return HealthState.HEALTHY, "alive_nonterminal_recent_activity"


def _get_transcript_path(db: Database, run_id: str) -> Path:
    """Return transcript file path under XDG state dir keyed by run_id."""
    safe = "".join(c if c.isalnum() or c in ("_", "-", ".") else "_" for c in run_id)
    return Path(db.db_path).resolve().parent / "transcripts" / f"{safe}.ansi"


def _canonical_worktree_path(repo_path: Path, branch: str) -> Path | None:
    """Return the canonical registered worktree path for ``branch``.

    Cross-checks ``git worktree list --porcelain`` so the namespace check
    matches what ``git`` itself recognizes (not a free-form path string).
    Returns ``None`` when the branch is not registered as a worktree of
    ``repo_path``.
    """
    try:
        res = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(Path(repo_path).resolve()),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode != 0:
            return None
        current_worktree: Path | None = None
        current_branch: str | None = None
        for raw in res.stdout.splitlines():
            if raw.startswith("worktree "):
                current_worktree = Path(raw[len("worktree ") :].strip()).resolve()
                current_branch = None
            elif raw.startswith("branch "):
                ref = raw[len("branch ") :].strip()
                # refs/heads/<branch>
                if ref.startswith("refs/heads/"):
                    current_branch = ref[len("refs/heads/") :]
                else:
                    current_branch = ref
            if current_worktree is not None and current_branch == branch:
                return current_worktree
    except Exception:
        return None
    return None


@dataclass(frozen=True)
class _RecoveryEligibility:
    """Outcome of :meth:`RunSupervisor.assess_recovery_eligibility`."""

    eligible: bool
    reason: str
    run_id: str


class RunSupervisor:
    """Launches and reconciles the single durable execution owner of a run."""

    def __init__(
        self,
        db: Database,
        worktree_base: Path | None = None,
        registry: AdapterRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.db = db
        self.worktree_base = worktree_base
        self.registry = registry
        self.tool_registry = tool_registry or ToolRegistry.from_environment()
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._lock = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}
        self._run_locks_lock = threading.Lock()

    def _get_run_lock(self, run_id: str) -> threading.Lock:
        with self._run_locks_lock:
            existing = self._run_locks.get(run_id)
            if existing is not None:
                return existing
            new_lock = threading.Lock()
            self._run_locks[run_id] = new_lock
            return new_lock

    def start_supervised_run(
        self,
        config: RunConfig,
    ) -> StartRunResult:
        """Validate via shared RunConfig, create PENDING run, launch the owner.

        This uses the exact same :func:`resolve_run_config` validation and
        PENDING creation as the CLI path; execution is delegated to the
        installed ``orch`` binary via ``--run-id`` adoption in a detached
        session.  Duplicate execution is prevented by the durable ownership
        claim taken before the process is created.
        """
        from saberops.authority import load_authority_settings
        from saberops.control_plane import (
            freeze_control_plane_policy,
            load_control_plane_policy,
        )
        from saberops.gateway import (
            reconstruct_frozen_gateway_config,
        )
        from saberops.routing_config import (
            load_effective_routing,
            snapshot_references_exact_bindings,
            snapshot_to_payload,
        )

        snapshot = load_effective_routing()
        resolve_candidate_chain(
            tier=config.initial_tier,
            model_override=config.model_override,
            provider_override=config.provider_override,
            training_allowed=config.training_allowed,
            snapshot=snapshot,
        )
        # C15-XB-FREEZE-01: when the frozen routing snapshot can produce an
        # exact-binding candidate, freeze the effective owner BindingRegistry
        # with the PENDING Run so every later adoption/recovery dispatch
        # resolves the same execution identity -- a post-admission owner
        # binding-store change must never alter this Run.  A static-only
        # snapshot loads no owner state.
        if (
            config.worker_binding_registry is None
            and snapshot_references_exact_bindings(snapshot)
        ):
            config = _frozen_replace(
                config,
                worker_binding_registry=freeze_owner_binding_registry_payload(),
            )
        repo = Path(config.target_repo)
        if not GitManager.is_git_repo(repo):
            raise ValueError(f"Target path is not a valid Git repository: {repo}")
        if not GitManager.is_clean(repo):
            raise RuntimeError(
                f"Target repository '{repo}' has uncommitted or untracked changes. "
                "Commit, stash, or clean working tree before running orch."
            )
        base_commit = GitManager.get_head_commit(repo)
        provenance = mint_run_provenance(repo, config.task)
        run_id = provenance.run_id
        created_at = current_iso_timestamp()
        routing_payload = snapshot_to_payload(snapshot, created_at)
        frozen_control_plane = freeze_control_plane_policy(
            load_control_plane_policy(),
            required_capabilities=config.required_capabilities,
            reserve_override=config.reserve_override,
            host_access=load_authority_settings().host_access.value,
        )
        frozen_tools = freeze_tool_contract(config.tool_refs, self.tool_registry)
        # C10: freeze the optional gateway contract.  When ``RunConfig`` does
        # not carry a frozen config, the run uses the first-class DIRECT
        # path and no gateway evidence is recorded.
        gateway_snapshot_json: str | None = None
        gateway_snapshot_digest: str | None = None
        if config.gateway_frozen_config:
            frozen_gateway = reconstruct_frozen_gateway_config(
                config.gateway_frozen_config,
                expected_digest=config.gateway_frozen_digest,
            )
            gateway_snapshot_json = frozen_gateway.as_json()
            gateway_snapshot_digest = frozen_gateway.digest
        run = Run(
            id=run_id,
            task=config.task,
            target_repo=str(repo),
            base_commit=base_commit,
            tier=config.initial_tier,
            training_allowed=config.training_allowed,
            gate_command=config.gate_command,
            status=RunStatus.PENDING,
            created_at=created_at,
            config_json=json.dumps(config.as_dict()),
            routing_snapshot_json=json.dumps(routing_payload),
            control_plane_snapshot_json=frozen_control_plane.as_json(),
            control_plane_snapshot_digest=frozen_control_plane.digest,
            tool_contract_json=frozen_tools.to_json(),
            tool_contract_digest=frozen_tools.digest,
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            gateway_snapshot_json=gateway_snapshot_json,
            gateway_snapshot_digest=gateway_snapshot_digest,
        )
        self.db.create_run(run)
        self.db.record_event(
            run_id,
            "run_created",
            payload={
                "task": config.task,
                "target_repo": str(repo),
                "base_commit": base_commit,
                "tier": config.initial_tier.value,
                "training_allowed": config.training_allowed,
                "gate_command": config.gate_command,
                "config": config.as_dict(),
                "routing_snapshot": routing_payload,
                "project_id": provenance.project_id,
                "objective_id": provenance.objective_id,
            },
        )
        self.db.record_event(run_id, "routing_snapshot", payload=routing_payload)

        # C09: the project graph and initial task scope must be frozen against
        # this exact ``base_commit`` -- and the execution owner must never be
        # launched without that frozen contract in place.  Building the graph
        # here, before ``_launch``, closes the race where the repository could
        # otherwise change between PENDING creation and the detached child
        # adopting the run.  If this fails, the run never launches.
        #
        # C09-R2: ``build_project_graph`` reads the working tree while
        # ``base_commit`` is metadata captured a moment earlier -- close that
        # TOCTOU window with a repository re-check both immediately before and
        # immediately after analysis, so a mid-analysis repository move can
        # never be frozen under the wrong revision label.
        scope_failure_phase = "pre_analysis_revision_guard"
        try:
            assert_repository_at_revision(
                repo, base_commit, when="before project-graph analysis"
            )
            scope_failure_phase = "project_graph_construction"
            project_graph = build_project_graph(repo, base_revision=base_commit)
            scope_failure_phase = "post_analysis_revision_guard"
            assert_repository_at_revision(
                repo, base_commit, when="after project-graph analysis"
            )
            scope_failure_phase = "scope_resolution"
            initial_scope = resolve_scope(project_graph, config.task)
            scope_failure_phase = "freeze_run_scope"
            freeze_run_scope(
                self.db,
                run_id,
                repo_root=str(repo),
                graph=project_graph,
                scope=initial_scope,
            )
            self.db.record_event(
                run_id,
                "project_scope_frozen",
                payload={
                    "graph_digest": project_graph.digest,
                    "graph_source": project_graph.graph_source.value,
                    "analyzer": project_graph.analyzer,
                    "confidence": project_graph.confidence.value,
                    "scope_digest": initial_scope.digest,
                    "primary_module": initial_scope.primary_module,
                    "included_modules": list(initial_scope.included_modules),
                },
            )
        except Exception as exc:  # noqa: BLE001 - never launch without frozen scope
            # C09-R3: fatal pre-dispatch scope failure is infrastructure
            # failure -- never a worker/provider/model failure -- and must
            # never leave the run RUNNING with no execution owner.  Bounded
            # durable evidence is recorded before terminalizing so the same
            # ``project_scope_failed`` event/lifecycle contract holds
            # regardless of whether the failure originates here or in the
            # direct synchronous ``Orchestrator.run`` path.
            bounded_message = str(exc).strip()[:2000]
            self.db.record_event(
                run_id,
                "project_scope_failed",
                payload={
                    "phase": scope_failure_phase,
                    "error_class": type(exc).__name__,
                    "message": bounded_message,
                    "base_commit": base_commit,
                    "run_id": run_id,
                },
            )
            self.db.fail_run_if_nonterminal(
                run_id,
                completed_at=current_iso_timestamp(),
                result_summary=f"project scope analysis failed ({scope_failure_phase}): "
                f"{bounded_message}"[:2000],
            )
            raise RuntimeError(
                f"project scope analysis failed for run '{run_id}': {exc}"
            ) from exc

        self._launch(run_id, config)
        return StartRunResult(run_id=run_id)

    def _is_active(self, run_id: str) -> bool:
        with self._lock:
            return self._is_active_locked(run_id)

    def _is_active_locked(self, run_id: str) -> bool:
        proc = self._processes.get(run_id)
        return proc is not None and proc.poll() is None

    def get_transcript_path(self, run_id: str) -> Path:
        """Return transcript file path for a run_id (XDG state dir, keyed)."""
        return _get_transcript_path(self.db, run_id)

    def get_transcript_bytes(self, run_id: str) -> bytes:
        """Return full transcript bytes, or empty if none."""
        p = self.get_transcript_path(run_id)
        try:
            return p.read_bytes()
        except OSError:
            return b""

    def shutdown(self) -> None:
        """Detach the UI/server without cancelling durable execution owners.

        The owner runs in its own session with its streams bound to the
        transcript file, so there is nothing to tear down here: losing the
        server neither signals the worker nor truncates its output.
        """
        with self._lock:
            tracked = list(self._processes.items())
        for run_id, proc in tracked:
            if proc.poll() is not None:
                continue
            self._persist_supervision(
                run_id=run_id,
                pid=getattr(proc, "pid", None),
                manager_state=ManagerState.SUSPENDED,
                health=HealthState.UNCERTAIN,
                reason="server_detached",
            )

    def _persist_supervision(
        self,
        run_id: str,
        pid: int | None,
        manager_state: ManagerState = ManagerState.ATTACHED,
        health: HealthState = HealthState.UNCERTAIN,
        reason: str | None = None,
        worker_identity: str | None = None,
    ) -> None:
        """Store non-secret ownership evidence used by reconnect/orphan checks."""
        run = self.db.get_run(run_id)
        previous = self.db.get_supervision(run_id)
        owner = self.db.get_execution_owner(run_id)
        identity = (
            f"execution-owner:{owner.owner_id}:gen{owner.generation}"
            if owner is not None
            else f"local-setsid:{os.getpid()}"
        )
        self.db.upsert_supervision(
            SupervisionRecord(
                run_id=run_id,
                supervisor_identity=identity,
                supervisor_state="watching",
                worker_pid=pid,
                worker_identity=worker_identity or (previous.worker_identity if previous else None),
                provider=previous.provider if previous else None,
                model=previous.model if previous else None,
                started_at=previous.started_at
                if previous and previous.started_at
                else current_iso_timestamp(),
                last_activity_at=current_iso_timestamp()
                if health == HealthState.HEALTHY
                else (previous.last_activity_at if previous else None),
                health_state=health,
                health_reason=reason,
                current_package_id=previous.current_package_id if previous else None,
                current_attempt_id=previous.current_attempt_id if previous else None,
                deadline_at=previous.deadline_at if previous else None,
                transcript_path=str(self.get_transcript_path(run_id)),
                wake_condition="worker terminal or activity change",
                manager_state=manager_state,
                updated_at=current_iso_timestamp(),
            )
        )
        self.db.record_event(
            run_id,
            "manager_attached" if manager_state == ManagerState.ATTACHED else "manager_suspended",
            payload={"pid": pid, "health": health.value, "reason": reason},
        )
        if run is not None:
            self.db.record_event(run_id, "supervisor_watching", payload={"pid": pid})

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------

    def execution_liveness(self, run_id: str) -> Liveness:
        """Liveness for the *launch* gate: may this process start an owner?

        This answers only "is there something already executing", and absence of
        a claim answers "no".  It is deliberately not the evidence used for
        destructive decisions -- see :meth:`owner_evidence` -- because launching
        is itself guarded by the atomic claim, which rejects a second owner.
        """
        if self._is_active(run_id):
            return Liveness.LIVE
        try:
            owner = self.db.get_execution_owner(run_id)
        except OwnershipStoreError:
            # An unreadable ownership store proves nothing; never launch on it.
            return Liveness.UNKNOWN
        if owner is None or not owner.is_active:
            return Liveness.DEAD
        return probe_liveness(owner.identity)

    def owner_evidence(self, run_id: str) -> tuple[ExecutionOwner | None, Liveness]:
        """Return the durable owner and its liveness, for destructive decisions.

        ``DEAD`` is returned only on positive evidence:

        * an active claim whose exact process incarnation is provably gone;
        * a claim its owner explicitly released;
        * no claim at all on a project-scoped run that was never adopted -- the
          run is still ``PENDING``, and every launch path in this engine claims
          ownership *before* creating a process, so no worker can exist.

        Everything else -- a legacy pre-ownership run, a run that was adopted by
        some executor that left no claim, an unreadable store -- is ``UNKNOWN``,
        which never orphans, kills, or relaunches.
        """
        try:
            owner = self.db.get_execution_owner(run_id)
        except OwnershipStoreError:
            return None, Liveness.UNKNOWN
        if owner is not None:
            if not owner.is_active:
                return owner, Liveness.DEAD
            return owner, probe_liveness(owner.identity)
        run = self.db.get_run(run_id)
        if run is not None and run.status == RunStatus.PENDING:
            return None, Liveness.DEAD
        return None, Liveness.UNKNOWN

    def is_execution_live(self, run_id: str) -> bool:
        """True only when a detached or tracked owner is provably running."""
        return self.execution_liveness(run_id) is Liveness.LIVE

    def _is_recovery_eligible(
        self,
        run: Run,
        inspected_owner: ExecutionOwner | None = None,
    ) -> bool:
        """Return True only when positive durable recovery evidence exists.

        A DEAD execution owner alone is NOT sufficient: C05 cold-recovery must
        only be selected when the persisted state is provably reconstructable
        (an existing checkpoint, dirty partial work, or a reconcilable crash
        window).  Legacy orphan / abort semantics remain the default for every
        other case; UNKNOWN safety evidence fails closed.

        ``inspected_owner`` lets the caller pass the owner row read at the
        start of ``reconcile`` so the predicate can fence against a newer
        generation that appeared during the inspection.
        """
        return self.assess_recovery_eligibility(run, inspected_owner).eligible

    def assess_recovery_eligibility(
        self,
        run: Run,
        inspected_owner: ExecutionOwner | None = None,
    ) -> _RecoveryEligibility:
        """Strict positive-evidence recovery-eligibility predicate.

        A run is positively recovery-eligible only when ALL of the following
        conditions are established:

        1. run status is appropriate for interrupted execution recovery
           (not terminal, not pending, not pre-claim orphan);
        2. run has a persisted active/interrupted attempt;
        3. the attempt belongs to this exact run;
        4. the attempt worktree path exists;
        5. the worktree path is inside the expected Orch worktree namespace
           (``orch/<run_id>/a<n>`` shape);
        6. the Git worktree is valid (HEAD readable);
        7. repository identity/provenance still matches the run;
        8. the attempt's branch/HEAD can be inspected safely;
        9. the old execution owner is provably DEAD (no newer generation);
        10. the worker is provably DEAD (no live worker process);
        11. there is meaningful recoverable execution state: an existing
            C05 checkpoint, dirty partial work, or a deterministically
            reconcilable checkpoint-creation crash window;
        12. no contradictory terminal/cancellation/ownership evidence exists;
        13. no newer execution-owner generation has taken authority.

        Anything missing or contradictory returns ``eligible=False`` with a
        reason; the caller MUST preserve orphan / abort semantics in that
        case.
        """
        if run.status in TERMINAL_RUN_STATUSES:
            return _RecoveryEligibility(False, "terminal_run", run.id)
        if run.status == RunStatus.PENDING:
            return _RecoveryEligibility(False, "pending_no_launch", run.id)
        try:
            assert_run_not_retargeted(run)
        except Exception:
            return _RecoveryEligibility(False, "retargeted_repository", run.id)
        attempts = self.db.get_attempts_for_run(run.id)
        if not attempts:
            return _RecoveryEligibility(False, "no_persisted_attempt", run.id)
        last_attempt = attempts[-1]
        if last_attempt.run_id != run.id:
            return _RecoveryEligibility(False, "attempt_run_mismatch", run.id)
        if last_attempt.status != AttemptStatus.RUNNING:
            return _RecoveryEligibility(False, "attempt_not_running", run.id)
        wt_path = Path(last_attempt.worktree_path)
        if not wt_path.exists():
            return _RecoveryEligibility(False, "worktree_missing", run.id)
        if not GitManager.is_git_repo(wt_path):
            return _RecoveryEligibility(False, "worktree_invalid_git", run.id)
        try:
            head_sha = GitManager.get_head_commit(wt_path)
        except Exception:
            return _RecoveryEligibility(False, "worktree_head_unreadable", run.id)
        branch = GitManager.get_current_branch(wt_path) or last_attempt.branch_name
        if branch and not _CANDIDATE_BRANCH_RE.match(branch):
            return _RecoveryEligibility(False, "worktree_branch_outside_namespace", run.id)
        # Provenance of repository still matches the run's project identity.
        try:
            assert_run_repository(run)
        except Exception:
            return _RecoveryEligibility(False, "repository_identity_mismatch", run.id)

        # Filesystem namespace: the persisted attempt path must resolve
        # to the expected Orch worktree location derived from the
        # authoritative worktree base for this run's project.  A
        # correctly-named branch attached to an arbitrary checkout
        # elsewhere must never qualify for recovery.  This is stricter
        # than ``startswith`` (which trivially accepts ``/tmp/...orch..``
        # unrelated locations): we derive the canonical expected path and
        # compare equality + subpath via ``is_relative_to``.
        expected_wt = self._expected_worktree_path(run, last_attempt)
        if expected_wt is None:
            return _RecoveryEligibility(False, "worktree_base_unresolvable", run.id)
        resolved_actual = wt_path.resolve()
        try:
            if resolved_actual != expected_wt.resolve():
                return _RecoveryEligibility(False, "worktree_path_outside_namespace", run.id)
        except OSError:
            return _RecoveryEligibility(False, "worktree_path_outside_namespace", run.id)
        # Git may corroborate the authoritative path, but it must never be
        # allowed to substitute a separately registered worktree elsewhere.
        registered = _canonical_worktree_path(Path(run.target_repo), last_attempt.branch_name or "")
        if registered is not None and registered != expected_wt.resolve():
            return _RecoveryEligibility(False, "worktree_registered_elsewhere", run.id)

        # Fence against newer owner generation: re-read the owner row
        # immediately before deciding; any newer generation wins.
        try:
            current_owner = self.db.get_execution_owner(run.id)
        except OwnershipStoreError:
            return _RecoveryEligibility(False, "ownership_store_unreadable", run.id)
        baseline_owner = inspected_owner
        if baseline_owner is None:
            try:
                baseline_owner = self.db.get_execution_owner(run.id)
            except OwnershipStoreError:
                return _RecoveryEligibility(False, "ownership_store_unreadable", run.id)
        if (
            current_owner is not None
            and baseline_owner is not None
            and current_owner.generation > baseline_owner.generation
        ):
            return _RecoveryEligibility(False, "ownership_changed_during_reconcile", run.id)
        # Positive recoverable execution state: meaningful partial work
        # with provable lineage (an exact persisted checkpoint or a
        # HEAD-ancestor of the attempt base SHA), or uncommitted edits
        # against a HEAD that is itself an ancestor of the attempt base.
        recoverable = self._has_recoverable_state(run.id, last_attempt, wt_path, head_sha)
        if recoverable:
            return _RecoveryEligibility(True, "recoverable_state", run.id)
        return _RecoveryEligibility(False, "no_recoverable_execution_state", run.id)

    def _expected_worktree_path(self, run: Run, last_attempt: Attempt) -> Path | None:
        """Return the canonical Orch worktree path for this run/attempt.

        Derives the authoritative worktree base for the run's own target
        repository, then returns ``<base>/<run_id>/a<n>``.  A registered Git
        worktree can corroborate that exact path but cannot redefine it.

        Returns ``None`` when the namespace cannot be resolved (so the
        recovery predicate fails closed rather than guessing at a
        ``/tmp``-style fallback).
        """
        from saberops.git import GitManager

        try:
            repo_path = Path(run.target_repo).resolve()
            if not GitManager.is_git_repo(repo_path):
                return None
            branch = last_attempt.branch_name
            if branch is None or not _CANDIDATE_BRANCH_RE.match(branch):
                return None
            if self.worktree_base is not None:
                return Path(self.worktree_base) / run.id / f"a{last_attempt.attempt_number}"
            return (
                GitManager.get_default_worktree_base(repo_path)
                / run.id
                / f"a{last_attempt.attempt_number}"
            )
        except Exception:
            return None

    def _has_recoverable_state(
        self,
        run_id: str,
        last_attempt: Attempt,
        wt_path: Path,
        head_sha: str,
    ) -> bool:
        """Return True only when meaningful + lineage-proven recovery state exists.

        Three positive-evidence shapes are accepted; everything else is
        rejected:

        * **Uncommitted partial work on authorized HEAD** -- HEAD equals
          the attempt base_sha, and ``git diff`` finds dirty edits
          against base.  This preserves the C05-R1 dirty-cold-takeover
          path on a clean HEAD.
        * **Already-created checkpoint commit** -- HEAD is **not** equal
          to base_sha AND ``base_sha`` is an **ancestor** of HEAD.  This
          is the crash-window shape: a worker wrote a checkpoint commit
          but died before the DB row was persisted.
        * **Persisted WorkerCheckpoint for THIS attempt** -- an existing
          ``WorkerCheckpoint`` whose ``source_attempt_id`` matches the
          latest attempt id.  An old checkpoint from a previous attempt
          is NOT positive evidence for THIS run's running attempt.

        Divergent HEAD (``base_sha`` is NOT an ancestor of HEAD) is
        strictly rejected -- a switched/reset worktree is exactly what
        BLOCKER 7 prescribes must fail closed.
        """
        base_sha = last_attempt.base_sha
        # 1. Uncommitted edits on an authorized HEAD == base.
        if base_sha and head_sha == base_sha:
            try:
                if GitManager.has_diff_against_base(wt_path, base_sha):
                    return True
            except Exception:
                pass
        # 2. HEAD != base AND base ancestor HEAD: checkpoint commit
        # already exists in the worktree (DB row may still be missing
        # due to a crash window).
        if base_sha and head_sha != base_sha:
            try:
                if GitManager.is_ancestor(wt_path, base_sha, head_sha):
                    return True
            except Exception:
                pass
        # 3. Persisted WorkerCheckpoint for the *latest* attempt only.
        latest_cp = self.db.get_latest_worker_checkpoint(run_id)
        if (
            latest_cp is not None
            and latest_cp.run_id == run_id
            and latest_cp.source_attempt_id == last_attempt.id
            and bool(last_attempt.base_sha)
            and latest_cp.source_base_sha == last_attempt.base_sha
            and bool(latest_cp.checkpoint_sha)
        ):
            # A database row is only an assertion.  Git must establish that
            # the referenced commit exists on the expected lineage.
            try:
                if GitManager.is_ancestor(wt_path, last_attempt.base_sha, latest_cp.checkpoint_sha):
                    return True
            except Exception:
                pass
        return False

    def reconcile(self, run_id: str, dry_run: bool = False) -> dict[str, object]:
        """Reconcile persisted run state with durable ownership evidence.

        Reconciliation never launches anything.  A live detached owner is kept,
        a provably dead owner orphans the run together with its contradictory
        RUNNING attempts, and anything undecidable is reported as ``UNKNOWN``
        without killing or relaunching.

        The orphaning decision is made about one exact owner generation and
        commits fenced to it, so a newer generation that claims the run while
        this reconcile is deciding is never orphaned, never has its attempts
        failed, and never gets released.
        """
        run = self.db.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        # Recovery acts on the run's own repository, resolved from persisted
        # identity rather than from whatever project is currently selected.
        try:
            assert_run_not_retargeted(run)
        except ProvenanceError as exc:
            raise ValueError(f"Cannot reconcile run '{run_id}': {exc}") from exc
        owner, liveness = self.owner_evidence(run_id)
        result: dict[str, object] = {
            "run_id": run_id,
            "status": run.status.value,
            "owner_id": owner.owner_id if owner is not None else None,
            "generation": owner.generation if owner is not None else None,
            "owner_pid": owner.identity.pid if owner is not None else None,
            "liveness": liveness.value,
            "live": liveness is Liveness.LIVE,
            "action": "none",
        }
        if run.status in TERMINAL_RUN_STATUSES:
            result["reason"] = "terminal_run_immutable"
            return result
        if liveness is Liveness.LIVE:
            result["action"] = "keep_live_owner"
            result["reason"] = "execution_owner_alive"
            return result
        if liveness is Liveness.UNKNOWN:
            result["reason"] = (
                "no_usable_ownership_evidence" if owner is None else "owner_liveness_unknown"
            )
            return result
        if owner is not None and owner.is_cancelling:
            # An authoritative cancellation owns the terminal transition.
            result["reason"] = "cancellation_in_progress"
            return result

        # Probe worker liveness before deciding on dead owner.  The default
        # is ``UNKNOWN``: a missing durable worker identity for a modern
        # C05 worker must never be classified as DEAD, because it is the
        # exact situation C05-INV-3 prescribes must fail closed.  Only an
        # exact persisted ProcessIdentity that probes ``DEAD`` allows a
        # recovery decision; anything else fails closed.
        worker_liveness = Liveness.UNKNOWN
        worker_liveness_reason: str | None = None
        sup = self.db.get_supervision(run_id)
        active_activation = self.db.get_active_worker_activation(run_id)
        if (
            active_activation is not None
            and active_activation.state is WorkerActivationState.TERMINATION_UNCERTAIN
        ):
            # A termination request without a conclusive result remains an
            # active safety hazard even when a stale worker identity happens
            # to probe DEAD.  Do not let that incidental observation authorize
            # a destructive takeover.
            worker_liveness = Liveness.UNKNOWN
            worker_liveness_reason = "worker_termination_uncertain"
        elif sup is not None and sup.worker_identity:
            try:
                proc_id = ProcessIdentity.from_json(sup.worker_identity)
                worker_liveness = probe_liveness(proc_id)
            except Exception:
                worker_liveness = Liveness.UNKNOWN
        elif sup is not None and sup.worker_pid is not None:
            worker_liveness = Liveness.UNKNOWN
        else:
            # No persisted worker identity at all: a modern C05 worker in
            # a mutating active state (STARTING / RUNNING / QUIESCING) is
            # exactly the situation where C05-INV-3 forbids classifying as
            # DEAD.  Probe the worker activation rows to apply the
            # backward-compatible rule: only an UNKNOWN modern C05
            # activation (RUNNING with no identity) raises this branch to
            # DEAD-friendly behavior; legacy pre-C05 runs without any
            # WorkerActivation row keep the older interpretation that
            # allows DEAD.
            if active_activation is None:
                # Genuinely pre-C05 legacy: no modern activation row at
                # all.  The legacy dead-owner orphan semantics still apply.
                worker_liveness = Liveness.DEAD
            elif (
                active_activation.state
                in (
                    WorkerActivationState.STARTING,
                    WorkerActivationState.RUNNING,
                    WorkerActivationState.QUIESCING,
                )
                and active_activation.worker_identity_json is None
            ):
                # Modern C05 worker in a mutating active state but no
                # identity persisted yet.  Per C05-INV-3 this is UNKNOWN,
                # never DEAD -- refuse takeover.
                worker_liveness = Liveness.UNKNOWN
                if not dry_run:
                    self.db.record_event(
                        run_id,
                        "recovery_blocked_unknown_worker_liveness",
                        payload={
                            "activation_state": active_activation.state.value,
                            "activation_generation": active_activation.generation,
                            "reason": "modern_worker_no_identity",
                        },
                    )
            elif active_activation.state in (
                WorkerActivationState.SUCCEEDED,
                WorkerActivationState.REPLACED,
                WorkerActivationState.CHECKPOINTED,
                WorkerActivationState.FAILED,
                WorkerActivationState.TIMED_OUT,
                WorkerActivationState.TERMINATED,
                WorkerActivationState.INTERRUPTED,
            ):
                # Identity-bearing row says it's already terminal/settled;
                # the predecessor worker is no longer a hazard.
                worker_liveness = Liveness.DEAD
            else:
                worker_liveness = Liveness.UNKNOWN

        if worker_liveness is Liveness.LIVE:
            result["action"] = "wait_for_worker"
            result["reason"] = "recovery_waiting_for_worker"
            if not dry_run:
                self.db.record_event(
                    run_id,
                    "recovery_waiting_for_worker",
                    payload={"owner_generation": owner.generation if owner else None},
                )
            return result

        if worker_liveness is Liveness.UNKNOWN:
            result["action"] = "none"
            result["reason"] = worker_liveness_reason or "worker_liveness_unknown"
            if not dry_run:
                self.db.record_event(
                    run_id,
                    "recovery_blocked_unknown_worker_liveness",
                    payload={
                        "owner_generation": owner.generation if owner else None,
                        "reason": result["reason"],
                    },
                )
            return result

        if self._is_recovery_eligible(run, inspected_owner=owner):
            result["action"] = "recover"
            result["reason"] = "recovery_eligible"
            result["status"] = run.status.value
            attempts = self.db.get_attempts_for_run(run_id)
            if not dry_run:
                self.db.record_event(
                    run_id,
                    "recovery_eligible",
                    payload={
                        "owner_generation": owner.generation if owner else None,
                        "reconstructable": True,
                        "attempt_id": attempts[-1].id if attempts else None,
                    },
                )
            return result

        result["action"] = "mark_orphaned"
        result["reason"] = "execution_owner_dead"
        result["status"] = RunStatus.ORPHANED.value
        if dry_run:
            return result
        summary = "execution owner is not alive"
        outcome = self.db.terminalize_run_fenced(
            run_id,
            owner_id=owner.owner_id if owner is not None else None,
            generation=owner.generation if owner is not None else None,
            status=RunStatus.ORPHANED,
            result_summary=summary,
            attempt_reason=f"orphaned: {summary}",
            expected_owner_states=(ExecutionOwner.CLAIMED,),
            release_reason="orphaned",
        )
        if outcome is None:
            # Ownership moved on, or the run became terminal, while we decided.
            current = self.db.get_run(run_id)
            result["action"] = "none"
            result["reason"] = "ownership_changed_during_reconcile"
            result["status"] = current.status.value if current is not None else run.status.value
            self.db.record_event(
                run_id,
                "reconcile_aborted",
                payload={
                    "reason": "ownership_changed_during_reconcile",
                    "inspected_owner_id": owner.owner_id if owner is not None else None,
                    "inspected_generation": owner.generation if owner is not None else None,
                },
            )
            return result
        result["failed_attempts"] = outcome["failed_attempts"]
        self.db.record_event(
            run_id,
            "orphan_detected",
            payload={
                "reason": "execution_owner_dead",
                "owner_id": owner.owner_id if owner is not None else None,
                "generation": owner.generation if owner is not None else None,
                "failed_attempts": outcome["failed_attempts"],
            },
        )
        return result

    def observe_long_worker(
        self,
        run_id: str,
        *,
        terminal_event_exists: bool,
        recent_activity: bool,
        timed_out: bool = False,
        stalled: bool = False,
    ) -> HealthState:
        """Persist the deterministic t=300 observation transition.

        This method is intentionally event/process based; it contains no model
        polling or sleep loop.  A scheduler invokes it once at the observation
        boundary and continues on terminal events before then.
        """
        evidence = self.db.get_supervision(run_id)
        alive = self.execution_liveness(run_id) is Liveness.LIVE
        if evidence is not None and evidence.worker_pid is not None:
            alive = (
                alive
                or probe_liveness(
                    ProcessIdentity(
                        host=os.uname().nodename,
                        boot_id=None,
                        pid=evidence.worker_pid,
                        start_ticks=None,
                        session_id=None,
                        process_group_id=None,
                    )
                )
                is not Liveness.DEAD
            )
        health, reason = assess_worker_health(
            process_alive=alive,
            terminal_event_exists=terminal_event_exists,
            recent_activity=recent_activity,
            timed_out=timed_out,
            stalled=stalled,
        )
        if evidence is None:
            return health
        state = (
            ManagerState.SUSPENDED
            if health == HealthState.HEALTHY
            else ManagerState.ATTENTION_REQUIRED
        )
        self.db.upsert_supervision(
            SupervisionRecord(
                run_id=evidence.run_id,
                supervisor_identity=evidence.supervisor_identity,
                supervisor_state=evidence.supervisor_state,
                worker_pid=evidence.worker_pid,
                provider=evidence.provider,
                model=evidence.model,
                started_at=evidence.started_at,
                last_activity_at=current_iso_timestamp()
                if recent_activity
                else evidence.last_activity_at,
                health_state=health,
                health_reason=reason,
                current_package_id=evidence.current_package_id,
                current_attempt_id=evidence.current_attempt_id,
                deadline_at=evidence.deadline_at,
                transcript_path=evidence.transcript_path,
                wake_condition=evidence.wake_condition,
                manager_state=state,
                updated_at=current_iso_timestamp(),
            )
        )
        if health == HealthState.HEALTHY:
            from saberops.handoff import create_handoffs

            create_handoffs(self.db, run_id)
            self.db.record_event(run_id, "manager_suspended", payload={"health_reason": reason})
            self.db.record_event(run_id, "supervisor_watching", payload={"health": health.value})
        else:
            self.db.record_event(
                run_id, "manager_attention_required", payload={"health_reason": reason}
            )
        return health

    def ensure_running(self, run_id: str, config: RunConfig | None = None) -> bool:
        """Ensure an execution owner exists for run_id; True if one was launched.

        Used for refresh/reconnect idempotency.  A live owner -- in this process
        or a detached one from a previous server lifetime -- is authoritative,
        and undecidable liveness deliberately refuses to launch.
        """
        run_lock = self._get_run_lock(run_id)
        with run_lock:
            if self.execution_liveness(run_id) is not Liveness.DEAD:
                return False
            # Check worker liveness: if worker is live or unknown, do not launch a second owner
            sup = self.db.get_supervision(run_id)
            if sup is not None and sup.worker_identity:
                try:
                    proc_id = ProcessIdentity.from_json(sup.worker_identity)
                    if probe_liveness(proc_id) is not Liveness.DEAD:
                        return False
                except Exception:
                    return False
            elif sup is not None and sup.worker_pid is not None:
                return False

            if config is None:
                run = self.db.get_run(run_id)
                if run is None or run.status in TERMINAL_RUN_STATUSES:
                    return False
                if run.config_json:
                    try:
                        from saberops.config import reconstruct_run_config

                        config = reconstruct_run_config(json.loads(run.config_json))
                    except Exception:
                        return False
                else:
                    return False
            return self._launch_with_lock_held(run_id, config)

    def ensure_recovery_running(
        self,
        run_id: str,
        config: RunConfig | None = None,
    ) -> tuple[ExecutionOwner | None, bool]:
        """C05 cold-takeover: launch a fenced dedicated recovery owner.

        Distinct from :meth:`ensure_running`: ordinary PENDING adoption stays
        PENDING-only and uses ``orch run --run-id``.  A recoverable RUNNING
        run must instead be overtaken by an explicit fenced recovery launch:

        * the supervisor reads the predecessor owner (gen N) and refuses to
          proceed unless that predecessor is provably DEAD;
        * the supervisor atomically claims generation N+1 BEFORE spawning
          any process, so two supervisors racing to recover the same run
          never both succeed;
        * the spawned child invokes ``orch recover <run-id>`` rather than
          ``orch run --run-id`` -- the recovery CLI handler calls
          :meth:`OrchestratorService.recover_sync`, which in turn
          :meth:`Orchestrator.recover_run` is fenced to the parent's
          reservation;
        * no second control plane exists: this method reuses the same
          per-run lock, atomic claim, and detached-process model as
          :meth:`ensure_running`.

        Returns ``(claimed_owner, spawned)``.  ``spawned`` is True only when
        a new detached recovery child process was created; on every refusal
        reason the method records a ``recovery_launch_refused`` event and
        returns ``(None, False)``.
        """
        run_lock = self._get_run_lock(run_id)
        with run_lock:
            run = self.db.get_run(run_id)
            if run is None or run.status in TERMINAL_RUN_STATUSES:
                self.db.record_event(
                    run_id,
                    "recovery_launch_refused",
                    payload={
                        "reason": "run_unknown_or_terminal",
                        "status": None if run is None else run.status.value,
                    },
                )
                return None, False
            if config is None:
                if not run.config_json:
                    self.db.record_event(
                        run_id,
                        "recovery_launch_refused",
                        payload={"reason": "missing_persisted_config"},
                    )
                    return None, False
                try:
                    from saberops.config import (
                        reconstruct_run_config,
                    )

                    config = reconstruct_run_config(json.loads(run.config_json))
                except Exception as exc:
                    self.db.record_event(
                        run_id,
                        "recovery_launch_refused",
                        payload={
                            "reason": "config_reconstruct_failed",
                            "error": str(exc)[:200],
                        },
                    )
                    return None, False

            # Predecessor eligibility must be observed right before the
            # successor claim so the launch is fenced end to end.  The
            # clause-by-clause hard fail conditions mirror
            # :meth:`assess_recovery_eligibility`, but the *only* positive
            # evidence we permit here is a DEAD owner + DEAD worker identity
            # + reconstructable execution state.
            eligibility = self.assess_recovery_eligibility(
                run, inspected_owner=self.db.get_execution_owner(run_id)
            )
            if not eligibility.eligible:
                self.db.record_event(
                    run_id,
                    "recovery_launch_refused",
                    payload={
                        "reason": eligibility.reason,
                        "predecessor_eligibility": False,
                    },
                )
                return None, False

            predecessor_owner = self.db.get_execution_owner(run_id)
            if predecessor_owner is None:
                # No predecessor: launching is fine, but cannot enforce
                # the C05-R2 fenced-generation contract because there is
                # no proven death.  Refuse (the run must be classified
                # by ``reconcile`` first to establish positive evidence).
                self.db.record_event(
                    run_id,
                    "recovery_launch_refused",
                    payload={"reason": "no_predecessor_owner"},
                )
                return None, False
            # The predecessor MAY be in CLAIMED state with a DEAD
            # process incarnation: that's the canonical "crashed
            # previous owner" case and is exactly when we are
            # authorized to claim gen N+1 on its behalf.  The first
            # hard failure point is the predecessor's process
            # liveness, not its ``is_active`` flag.
            if probe_liveness(predecessor_owner.identity) is not Liveness.DEAD:
                self.db.record_event(
                    run_id,
                    "recovery_launch_refused",
                    payload={"reason": "predecessor_not_provably_dead"},
                )
                return None, False

            # Worker-liveness gate: a LIVE or UNKNOWN old worker
            # identity must block any recovery launch even when the
            # eligibility predicate already says "recover" for an
            # eligible state (the predicate intentionally does not
            # double-check worker liveness because ``reconcile`` does).
            # BLOCKER 2 / BLOCKER 3 demand the launch path fail closed
            # in those cases instead of spawning a second owner.
            worker_liveness = Liveness.UNKNOWN
            sup = self.db.get_supervision(run_id)
            active_activation = self.db.get_active_worker_activation(run_id)
            if (
                active_activation is not None
                and active_activation.state is WorkerActivationState.TERMINATION_UNCERTAIN
            ):
                worker_liveness = Liveness.UNKNOWN
            elif sup is not None and sup.worker_identity:
                try:
                    proc_id = ProcessIdentity.from_json(sup.worker_identity)
                    worker_liveness = probe_liveness(proc_id)
                except Exception:
                    worker_liveness = Liveness.UNKNOWN
            elif sup is not None and sup.worker_pid is not None:
                worker_liveness = Liveness.UNKNOWN
            else:
                # No persisted worker identity at all: a modern C05
                # worker in a mutating active state would be flagged
                # UNKNOWN by ``reconcile``.  Mirror that gate here so
                # ``ensure_recovery_running`` cannot bypass it.
                if active_activation is None:
                    worker_liveness = Liveness.DEAD
                elif (
                    active_activation.state
                    in (
                        WorkerActivationState.STARTING,
                        WorkerActivationState.RUNNING,
                        WorkerActivationState.QUIESCING,
                    )
                    and active_activation.worker_identity_json is None
                ):
                    worker_liveness = Liveness.UNKNOWN
                elif active_activation.state in (
                    WorkerActivationState.SUCCEEDED,
                    WorkerActivationState.REPLACED,
                    WorkerActivationState.CHECKPOINTED,
                    WorkerActivationState.FAILED,
                    WorkerActivationState.TIMED_OUT,
                    WorkerActivationState.TERMINATED,
                    WorkerActivationState.INTERRUPTED,
                ):
                    worker_liveness = Liveness.DEAD
            if worker_liveness in (Liveness.LIVE, Liveness.UNKNOWN):
                self.db.record_event(
                    run_id,
                    "recovery_launch_refused",
                    payload={
                        "reason": "worker_liveness_not_dead",
                        "worker_liveness": worker_liveness.value,
                    },
                )
                return None, False

            # Atomically claim generation N+1 as the recovery successor.  If
            # a racer has already taken a newer generation, claim_execution_owner
            # raises OwnershipConflictError, which we translate into a refusal.
            try:
                successor = self.db.claim_execution_owner(
                    run_id,
                    identity=ProcessIdentity.for_self(),
                )
            except OwnershipError as exc:
                self.db.record_event(
                    run_id,
                    "recovery_launch_refused",
                    payload={
                        "reason": "claim_rejected",
                        "error": str(exc)[:200],
                    },
                )
                return None, False
            # Record a durable successor-claim event so the recovery
            # child (and any audit/replay) can prove the supervisor
            # actually claimed THIS exact successor before the child
            # executed.  Without this marker, a predecessor generation
            # could satisfy an unfenced ``recover_run`` call: BLOCKER 4.
            self.db.record_event(
                run_id,
                "recovery_owner_claimed",
                payload={
                    "owner_id": successor.owner_id,
                    "generation": successor.generation,
                    "predecessor_owner_id": predecessor_owner.owner_id,
                    "predecessor_generation": predecessor_owner.generation,
                },
            )

        # We hold per-run lock here.  Spawn the child and hand it the
        # ``successor`` fencing token via environment (mirroring the
        # existing ``--run-id`` path).
        spawned = self._spawn_recovery_owner(run_id, config, successor)
        if not spawned:
            # Release the reservation we just claimed -- nothing else can
            # adopt it (no live owner was here) and a follow-up attempt can
            # try again.  ``release_execution_owner`` is fenced by the same
            # generation so an even newer racer cannot be undone here.
            try:
                self.db.release_execution_owner(
                    run_id,
                    owner_id=successor.owner_id,
                    generation=successor.generation,
                    reason="recovery_launch_failed",
                )
            except Exception:
                pass
            return successor, False
        return successor, True

    def _build_recovery_cli_args(self, run_id: str, config: RunConfig) -> list[str]:
        """Build the dedicated ``recover`` invocation.

        Mirrors :meth:`_build_cli_args` shape (same owner argv prefix, same
        db path, same target repo cwd) but produces a command line that
        :meth:`handle_recover` accepts -- NOT ``run --run-id``.  The
        fencing token (owner_id/generation) is delivered via the standard
        ``ORCH_EXECUTION_OWNER_*`` environment variables so the child can
        adopt the exact reservation the supervisor pre-claimed.
        """
        from saberops.web import resolve_owner_argv_prefix

        prefix = resolve_owner_argv_prefix()
        db_path = str(self.db.db_path)
        return [
            *prefix,
            "recover",
            run_id,
            "--db",
            db_path,
        ]

    def _spawn_recovery_owner(
        self,
        run_id: str,
        config: RunConfig,
        owner: ExecutionOwner,
    ) -> bool:
        """Claim ownership (already done), then detach the recovery child.

        This is the recovery-mode analogue of :meth:`_spawn_owner`: it
        builds dedicated ``orch recover`` CLI args and Popen's the child in
        a fresh session with the fencing reservation handed in via env,
        so a crashed CLI cannot leak the predecessor's progress into an
        uncontrolled subprocess tree.
        """
        args = self._build_recovery_cli_args(run_id, config)
        transcript_path = _get_transcript_path(self.db, run_id)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        src_path = str(Path(__file__).resolve().parent.parent)
        current_pp = env.get("PYTHONPATH", "")
        if src_path not in current_pp.split(os.pathsep):
            env["PYTHONPATH"] = (src_path + os.pathsep + current_pp).rstrip(os.pathsep)
        env[OWNER_ENV_ID] = owner.owner_id
        env[OWNER_ENV_GENERATION] = str(owner.generation)

        proc: subprocess.Popen[Any] | None = None
        failure: Exception | None = None
        log_fh = None
        devnull_fh = None
        try:
            log_fh = open(transcript_path, "ab", buffering=0)
            devnull_fh = open(os.devnull, "rb")
            proc = subprocess.Popen(
                args,
                stdin=devnull_fh,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=str(Path(config.target_repo).resolve()),
                close_fds=True,
                start_new_session=True,
                env=env,
            )
        except Exception as exc:
            failure = exc
        finally:
            for handle in (log_fh, devnull_fh):
                try:
                    if handle is not None:
                        handle.close()
                except OSError:
                    pass

        if failure is not None or proc is None:
            self.db.record_event(
                run_id,
                "recovery_launch_refused",
                payload={
                    "reason": "popen_failed",
                    "error": str(failure)[:200] if failure else "popen_returned_none",
                },
            )
            return False

        pid = getattr(proc, "pid", None)
        if isinstance(pid, int):
            child_identity = ProcessIdentity.for_pid(pid) or ProcessIdentity(
                host=ProcessIdentity.for_self().host,
                boot_id=owner.identity.boot_id,
                pid=pid,
                start_ticks=None,
                session_id=pid,
                process_group_id=pid,
            )
            self.db.adopt_execution_owner_identity(
                run_id,
                owner_id=owner.owner_id,
                generation=owner.generation,
                identity=child_identity,
            )
        with self._lock:
            self._processes[run_id] = proc
        self._persist_supervision(
            run_id,
            pid,
            ManagerState.ATTACHED,
            HealthState.UNCERTAIN,
            "recovery_started",
            worker_identity=None,
        )
        self.db.record_event(
            run_id,
            "recovery_owner_spawned",
            payload={
                "owner_id": owner.owner_id,
                "generation": owner.generation,
                "pid": pid,
            },
        )
        return True

    def _build_cli_args(self, run_id: str, config: RunConfig) -> list[str]:
        from saberops.web import resolve_owner_argv_prefix

        prefix = resolve_owner_argv_prefix()
        db_path = str(self.db.db_path)
        tier_arg = "auto"
        if config.routing_mode == "manual" and config.manual_tier is not None:
            tier_arg = config.manual_tier.value
        args: list[str] = [
            *prefix,
            "run",
            "--repo",
            config.target_repo,
            "--task",
            config.task,
            "--gate",
            config.gate_command,
            "--tier",
            tier_arg,
            "--max-tier",
            config.max_auto_tier.value,
            "--max-attempts",
            str(config.max_attempts),
            "--gate-timeout",
            str(config.gate_timeout),
            "--review-timeout",
            str(config.review_timeout),
            "--review-mode",
            config.review_mode,
            "--review-limit",
            str(config.review_limit),
            "--review-policy",
            # C13-F: project-scoped risk-adaptive review policy mode is
            # frozen at Run creation; the supervisor-launched owner must
            # forward the same value so a fresh CLI process does not
            # silently re-derive it from (possibly changed) live policy.
            config.review_policy_mode.value.lower().replace("_", "-"),
            # The candidate-publication decision was frozen when the run was
            # created; relay it so the detached owner never re-derives it from
            # (possibly changed) live authority policy.
            "--publish-candidate" if config.publish_candidate else "--no-publish-candidate",
            "--db",
            db_path,
            "--run-id",
            run_id,
        ]
        if config.provider_override:
            args.extend(["--provider", config.provider_override])
        if config.model_override:
            args.extend(["--model", config.model_override])
        for capability in config.required_capabilities:
            args.extend(["--require-capability", capability])
        for tool_ref in config.tool_refs:
            args.extend(["--tool", tool_ref])
        if not config.training_allowed:
            args.append("--training-denied")
        if config.review_enabled:
            args.append("--review")
        if config.worker_timeout_explicit:
            args.extend(["--worker-timeout", str(config.worker_timeout)])
        return args

    def _spawn_owner(self, run_id: str, config: RunConfig) -> bool:
        """Claim ownership, then start the detached execution owner process.

        The claim is taken first and atomically: if another live owner exists
        this returns without ever creating a process, so duplicate execution is
        impossible rather than merely unlikely.
        """
        run = self.db.get_run(run_id)
        try:
            owner = self.db.claim_execution_owner(
                run_id,
                identity=ProcessIdentity.for_self(),
                project_id=run.project_id if run is not None else None,
            )
        except OwnershipError as exc:
            self.db.record_event(run_id, "execution_owner_rejected", payload={"reason": str(exc)})
            return False

        args = self._build_cli_args(run_id, config)
        transcript_path = _get_transcript_path(self.db, run_id)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        src_path = str(Path(__file__).resolve().parent.parent)
        current_pp = env.get("PYTHONPATH", "")
        if src_path not in current_pp.split(os.pathsep):
            env["PYTHONPATH"] = (src_path + os.pathsep + current_pp).rstrip(os.pathsep)
        env[OWNER_ENV_ID] = owner.owner_id
        env[OWNER_ENV_GENERATION] = str(owner.generation)

        proc: subprocess.Popen[Any] | None = None
        failure: Exception | None = None
        log_fh = None
        devnull_fh = None
        try:
            log_fh = open(transcript_path, "ab", buffering=0)
            devnull_fh = open(os.devnull, "rb")
            # argv only, never a shell.  ``start_new_session`` puts the owner in
            # its own session and process group, so a SIGHUP or SIGINT aimed at
            # the caller's terminal cannot reach it, and the transcript file
            # keeps its streams valid after every caller has gone.
            proc = subprocess.Popen(
                args,
                stdin=devnull_fh,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=str(Path(config.target_repo).resolve()),
                close_fds=True,
                start_new_session=True,
                env=env,
            )
        except Exception as exc:
            failure = exc
        finally:
            for handle in (log_fh, devnull_fh):
                try:
                    if handle is not None:
                        handle.close()
                except OSError:
                    pass

        if failure is not None or proc is None:
            self.db.release_execution_owner(
                run_id,
                owner_id=owner.owner_id,
                generation=owner.generation,
                reason="launch_failed",
            )
            now = current_iso_timestamp()
            err_str = str(failure)
            err_msg = f"Failed to launch orch subprocess for run {run_id}: {err_str}"
            transitioned = self.db.fail_run_if_nonterminal(
                run_id=run_id,
                completed_at=now,
                result_summary=err_msg,
            )
            self.db.record_event(run_id, "run_exception", payload={"error": err_str})
            if transitioned:
                self.db.record_event(
                    run_id,
                    "run_failed",
                    payload={"summary": err_msg, "error": err_str},
                )
            return False

        pid = getattr(proc, "pid", None)
        if isinstance(pid, int):
            child_identity = ProcessIdentity.for_pid(pid) or ProcessIdentity(
                host=ProcessIdentity.for_self().host,
                boot_id=owner.identity.boot_id,
                pid=pid,
                start_ticks=None,
                # start_new_session guarantees sid == pgid == pid.
                session_id=pid,
                process_group_id=pid,
            )
            self.db.adopt_execution_owner_identity(
                run_id,
                owner_id=owner.owner_id,
                generation=owner.generation,
                identity=child_identity,
            )
        with self._lock:
            self._processes[run_id] = proc
        self._persist_supervision(
            run_id, pid, ManagerState.ATTACHED, HealthState.UNCERTAIN, "started"
        )
        return True

    def _launch_with_lock_held(self, run_id: str, config: RunConfig) -> bool:
        """Assume caller holds per-run lock; perform the atomic claim + launch."""
        with self._lock:
            if self._is_active_locked(run_id):
                return False
        return self._spawn_owner(run_id, config)

    def _launch(self, run_id: str, config: RunConfig) -> None:
        run_lock = self._get_run_lock(run_id)
        with run_lock:
            self._launch_with_lock_held(run_id, config)

    def get_process(self, run_id: str) -> subprocess.Popen[Any] | None:
        """Return tracked process for run_id if alive, else None."""
        with self._lock:
            proc = self._processes.get(run_id)
            if proc is not None and proc.poll() is None:
                return proc
            return None

    def cancel(self, run_id: str) -> bool:
        """Explicitly cancel a run: the only path that terminates a worker.

        Cancellation is fenced end to end.  It first marks the exact inspected
        ownership generation ``CANCELLING`` -- a durable state that blocks any
        takeover -- so no competing process can claim the run in the window
        between "the old owner died" and "the run became terminal".  Only then
        is the process group signalled, and the terminal transition (run,
        attempts, ownership release) commits as one fenced unit.

        Undecidable liveness fails closed: nothing is signalled, nothing is
        mutated, and any cancellation already begun is aborted cleanly.  The
        same holds for the fenced worker-tree termination: an unconfirmed
        worker-group result (identity mismatch, unknown liveness, signal
        failure, incomplete evidence, or members remaining) aborts the
        cancellation, restores CLAIMED, and refuses — the owner is never
        signalled and the run is never terminalized while a worker process
        might still be alive.  A worker group already confirmed extinct is
        safe to proceed past.
        Repeated cancellation is safe.
        """
        run = self.db.get_run(run_id)
        if run is None:
            return False
        try:
            owner, liveness = self.owner_evidence(run_id)
        except OwnershipError:
            return False

        if run.status in TERMINAL_RUN_STATUSES:
            # Idempotent: a terminal run is immutable and already stopped.  Any
            # lingering claim is released fenced to the generation we inspected.
            if owner is not None and owner.is_active and liveness is Liveness.DEAD:
                self.db.release_execution_owner(
                    run_id,
                    owner_id=owner.owner_id,
                    generation=owner.generation,
                    reason="cancel_after_terminal",
                )
            return False

        if liveness is Liveness.UNKNOWN:
            self.db.record_event(
                run_id,
                "cancel_refused",
                payload={
                    "reason": "owner_liveness_unknown",
                    "owner_id": owner.owner_id if owner is not None else None,
                },
            )
            return False

        terminated = False
        termination_reason = "no_execution_owner"
        if owner is not None:
            if not self.db.begin_execution_cancellation(
                run_id,
                owner_id=owner.owner_id,
                generation=owner.generation,
                agent=ProcessIdentity.for_self(),
            ):
                self.db.record_event(
                    run_id,
                    "cancel_refused",
                    payload={
                        "reason": "ownership_changed_before_cancellation",
                        "inspected_owner_id": owner.owner_id,
                        "inspected_generation": owner.generation,
                    },
                )
                return False
            self.db.record_event(
                run_id,
                "cancellation_started",
                payload={"owner_id": owner.owner_id, "generation": owner.generation},
            )
            try:
                if liveness is Liveness.LIVE:
                    # Kill the worker process tree first using fenced identity.
                    # A valid persisted worker_identity is required; no PID-only
                    # fallback is permitted for an active modern worker.
                    supervision = self.db.get_supervision(run_id)
                    if supervision is not None and supervision.worker_identity is not None:
                        from saberops.ownership import (
                            terminate_worker_group_fenced as _tpg_fenced,
                        )

                        worker_ok, worker_reason = _tpg_fenced(supervision.worker_identity)
                        if not worker_ok:
                            # Fail closed: the worker-group termination is
                            # unconfirmed (identity mismatch, unknown
                            # liveness, signal failure, incomplete evidence,
                            # or members remaining).  A provider process may
                            # still be alive — never signal the owner and
                            # never terminalize the run as successfully
                            # cancelled.  Restore CLAIMED and record exactly
                            # why the cancellation was refused.
                            self.db.abort_execution_cancellation(
                                run_id,
                                owner_id=owner.owner_id,
                                generation=owner.generation,
                            )
                            self.db.record_event(
                                run_id,
                                "cancel_refused",
                                payload={
                                    "reason": "worker_termination_not_confirmed",
                                    "worker_termination_reason": worker_reason,
                                },
                            )
                            return False
                    elif supervision is not None and supervision.worker_pid is not None:
                        # Worker started but identity not yet persisted (race).
                        # Refuse to guess from a bare PID — fail closed.
                        self.db.abort_execution_cancellation(
                            run_id,
                            owner_id=owner.owner_id,
                            generation=owner.generation,
                        )
                        self.db.record_event(
                            run_id,
                            "cancel_refused",
                            payload={
                                "reason": "worker_identity_unavailable_or_unknown",
                                "worker_pid": supervision.worker_pid,
                            },
                        )
                        return False
                    # Then terminate the owner's process group.
                    terminated, termination_reason = terminate_owner(owner)
                else:
                    termination_reason = "owner_already_dead"
                if termination_reason == "owner_liveness_unknown":
                    # Became undecidable mid-flight: abort without mutating.
                    self.db.abort_execution_cancellation(
                        run_id, owner_id=owner.owner_id, generation=owner.generation
                    )
                    self.db.record_event(
                        run_id,
                        "cancel_refused",
                        payload={"reason": "owner_liveness_unknown"},
                    )
                    return False
            except BaseException:
                self.db.abort_execution_cancellation(
                    run_id, owner_id=owner.owner_id, generation=owner.generation
                )
                raise

        outcome = self.db.terminalize_run_fenced(
            run_id,
            owner_id=owner.owner_id if owner is not None else None,
            generation=owner.generation if owner is not None else None,
            status=RunStatus.FAILED,
            result_summary="Cancelled by explicit owner request",
            attempt_reason="cancelled by explicit request",
            expected_owner_states=(ExecutionOwner.CANCELLING,),
            release_reason="cancelled",
            completed_at=current_iso_timestamp(),
        )
        if outcome is None:
            if owner is not None:
                self.db.abort_execution_cancellation(
                    run_id, owner_id=owner.owner_id, generation=owner.generation
                )
            self.db.record_event(
                run_id,
                "cancel_refused",
                payload={"reason": "ownership_changed_during_cancellation"},
            )
            return False

        self._persist_supervision(
            run_id,
            owner.identity.pid if owner is not None else None,
            ManagerState.ATTENTION_REQUIRED,
            HealthState.TERMINAL,
            "owner_cancelled",
        )
        self.db.record_event(run_id, "worker_failed", payload={"reason": "owner_cancelled"})
        self.db.record_event(
            run_id,
            "run_cancelled",
            payload={
                "owner_id": owner.owner_id if owner is not None else None,
                "generation": owner.generation if owner is not None else None,
                "terminated": terminated,
                "termination": termination_reason,
                "failed_attempts": outcome["failed_attempts"],
            },
        )
        return True


__all__ = ["ExecutionOwner", "RunSupervisor", "assess_worker_health"]
