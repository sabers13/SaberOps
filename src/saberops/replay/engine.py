"""Execution engine for C12-A real-project replay.

Executes the replay semantic shape:
authoritative Project
    ↓
freeze exact starting identity
    ↓
create disposable replay workspace
    ↓
execute replay-defined task/strategy
    ↓
deterministic verification
    ↓
collect measurement record
    ↓
preserve replay evidence
    ↓
discard disposable mutable workspace
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from saberops.git import GitManager
from saberops.replay.contracts import (
    DeterministicVerifier,
    FailureClassification,
    FrozenStartingState,
    MeasurementRecord,
    ReplayDefinition,
    ReplayExecutionRecord,
    ReplayProjectMismatchError,
    ReplaySafetyViolationError,
    ReplayStateMismatchError,
)
from saberops.replay.registry import RealProjectRegistry
from saberops.replay.toolchain import fingerprint_or_unavailable
from saberops.replay.workspace import (
    DisposableWorkspace,
    compute_workspace_result_digest,
)


def _verify_and_resolve_ledger_state(
    db: Any | None,
    project_id: str,
    checkpoint_id: str | None,
    state_digest: str | None,
) -> tuple[str | None, str | None]:
    """Authoritatively verify and resolve checkpoint and state digest against Project Ledger.

    Semantics:
    A. checkpoint_id + state_digest:
       verify checkpoint belongs to live Project and digest matches.
    B. checkpoint_id only:
       verify checkpoint and derive authoritative state_digest.
    C. state_digest only:
       load authoritative Project Ledger and require exact digest match.
    D. neither:
       remain explicitly absent.

    Raises:
        ReplaySafetyViolationError: If checkpoint or state digest is claimed without db.
        ReplayStateMismatchError: If checkpoint cannot be verified, does not match project,
            or if state digest does not match the authoritative ledger / checkpoint digest.
    """
    if checkpoint_id is None and state_digest is None:
        return None, None

    if db is None:
        raise ReplaySafetyViolationError(
            "Cannot claim Project Ledger checkpoint or state digest without "
            "verification against authoritative database."
        )

    if checkpoint_id is not None:
        from saberops.project_ledger.checkpoint import verify_checkpoint

        try:
            verified_ckpt = verify_checkpoint(
                db, checkpoint_id, expected_project_id=project_id
            )
        except Exception as exc:
            raise ReplayStateMismatchError(
                f"Checkpoint verification failed for '{checkpoint_id}': {exc}"
            ) from exc

        auth_state_digest = verified_ckpt.checkpoint.state_digest
        if state_digest is not None and auth_state_digest != state_digest:
            raise ReplayStateMismatchError(
                f"Supplied state digest '{state_digest}' does not match verified "
                f"checkpoint state digest '{auth_state_digest}'."
            )
        return verified_ckpt.checkpoint_id, auth_state_digest

    # state_digest only (checkpoint_id is None)
    from saberops.project_ledger.store import load_project

    try:
        loaded_ledger = load_project(db, project_id)
    except Exception as exc:
        raise ReplayStateMismatchError(
            f"Failed to load ledger for project '{project_id}': {exc}"
        ) from exc

    if loaded_ledger.state_digest != state_digest:
        raise ReplayStateMismatchError(
            f"Supplied state digest '{state_digest}' does not match authoritative "
            f"ledger state digest '{loaded_ledger.state_digest}'."
        )
    return None, state_digest


class ReplayEngine:
    """Orchestrates bounded replay executions and collects comparable measurements."""

    def __init__(
        self,
        repo_path: Path | str,
        registry: RealProjectRegistry | None = None,
        db: Any | None = None,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.registry = registry or RealProjectRegistry()
        self.db = db

    def freeze_starting_state(
        self,
        project_key: str,
        starting_sha: str | None = None,
        checkpoint_id: str | None = None,
        state_digest: str | None = None,
        db: Any | None = None,
    ) -> FrozenStartingState:
        """Freeze exact starting identity of the project repository.

        Validates against registered project identity and verifies any claimed
        checkpoint or state digest against the Project Ledger database.
        """
        ident = self.registry.verify_project_repository(
            project_key=project_key,
            repo_path=self.repo_path,
            require_clean=True,
        )

        resolved_db = db if db is not None else self.db
        verified_checkpoint_id, verified_state_digest = _verify_and_resolve_ledger_state(
            db=resolved_db,
            project_id=ident.project_id,
            checkpoint_id=checkpoint_id,
            state_digest=state_digest,
        )

        resolved_sha = (
            starting_sha
            if starting_sha is not None
            else GitManager.get_head_commit(self.repo_path)
        )

        return FrozenStartingState(
            project_id=ident.project_id,
            repository_path=str(self.repo_path),
            starting_git_sha=resolved_sha,
            root_commit=ident.root_commit,
            checkpoint_id=verified_checkpoint_id,
            state_digest=verified_state_digest,
        )

    def run_verifier(
        self,
        workspace_path: Path,
        verifier: DeterministicVerifier,
        classifier_fn: Callable[[dict[str, Any]], FailureClassification | None] | None = None,
    ) -> tuple[bool, FailureClassification | None, dict[str, Any]]:
        """Execute deterministic verification checks within the disposable workspace.

        Executes through existing run_sandboxed_process with real bwrap containment,
        hermetic explicit environment, and read-only repository view.

        Preserves raw verification failure evidence and does NOT assume CANDIDATE_DEFECT.
        Leaves classification UNKNOWN/unclassified (None) unless an explicit evidence-based
        classifier is supplied.
        """
        from saberops.process import run_sandboxed_process
        from saberops.sandbox import (
            NetworkPolicy,
            ProcessPolicy,
            SandboxPolicy,
            bwrap_available,
        )

        if not bwrap_available():
            raise ReplaySafetyViolationError(
                "Real bwrap containment required for verifier execution; "
                "uncontained fallback forbidden."
            )

        passed = True
        evidence: dict[str, Any] = {
            "command": list(verifier.command),
            "expected_exit_code": verifier.expected_exit_code,
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "passed": False,
            "timed_out": False,
            "launch_failed": False,
            "termination_incomplete": False,
            "duration": 0.0,
            "missing_files": [],
        }

        if verifier.command:
            raw_cmd = list(verifier.command)
            if raw_cmd and raw_cmd[0] == "pytest":
                cmd = [
                    "/usr/bin/python3",
                    "-m",
                    "pytest",
                    "-o",
                    "cache_dir=/tmp/.pytest_cache",
                    *raw_cmd[1:],
                ]
            elif raw_cmd and raw_cmd[0] == "python3":
                cmd = ["/usr/bin/python3", *raw_cmd[1:]]
            else:
                cmd = raw_cmd

            policy = SandboxPolicy(
                repo_root=str(workspace_path),
                readable_paths=("**",),
                writable_paths=(),
                deny_paths=(".git/**", ".github/**"),
                network=NetworkPolicy(mode="DENY"),
                credential_refs=(),
                process=ProcessPolicy(
                    allow_host_shell=False,
                    sandboxed_shell=True,
                ),
            )
            env = {
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": f"/usr/local/lib/orch-verifier:{workspace_path}/src",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            res = run_sandboxed_process(
                cmd,
                cwd=workspace_path,
                policy=policy,
                env=env,
                inherit_env=False,
            )

            passed_exit = (res.exit_code == verifier.expected_exit_code)
            passed_clean = (
                not res.timed_out
                and not res.launch_failed
                and not res.termination_incomplete
            )
            cmd_passed = passed_exit and passed_clean

            evidence["exit_code"] = res.exit_code
            evidence["stdout"] = res.stdout
            evidence["stderr"] = res.stderr
            evidence["passed"] = cmd_passed
            evidence["timed_out"] = res.timed_out
            evidence["launch_failed"] = res.launch_failed
            evidence["termination_incomplete"] = res.termination_incomplete
            evidence["duration"] = res.duration_seconds

            if not cmd_passed:
                passed = False

        if passed and verifier.expected_files:
            missing: list[str] = []
            for expected_rel_path in verifier.expected_files:
                target_file = workspace_path / expected_rel_path
                if not target_file.exists():
                    passed = False
                    missing.append(expected_rel_path)
            evidence["missing_files"] = missing

        if passed:
            return True, None, evidence

        classification: FailureClassification | None = None
        if classifier_fn is not None:
            classification = classifier_fn(evidence)

        return False, classification, evidence

    def _verify_supplied_starting_state(
        self,
        starting_state: FrozenStartingState,
        live_ident: Any,
        definition: ReplayDefinition,
    ) -> FrozenStartingState:
        """Independently verify a caller-supplied FrozenStartingState against live authority.

        Ensures:
        - project_id matches live repository
        - root_commit matches live repository
        - repository_path matches live engine repo_path
        - starting_git_sha matches definition.frozen_start_sha (if defined)
        - checkpoint_id and state_digest match authoritative Project Ledger via shared helper
        - derives authoritative state_digest if checkpoint_id is provided without state_digest
        """
        if starting_state.project_id != live_ident.project_id:
            raise ReplayProjectMismatchError(
                f"Supplied starting_state project_id '{starting_state.project_id}' "
                f"does not match live repository project_id '{live_ident.project_id}'."
            )
        if starting_state.root_commit and starting_state.root_commit != live_ident.root_commit:
            raise ReplayProjectMismatchError(
                f"Supplied starting_state root_commit '{starting_state.root_commit}' "
                f"does not match live repository root_commit '{live_ident.root_commit}'."
            )
        if Path(starting_state.repository_path).resolve() != self.repo_path:
            raise ReplayProjectMismatchError(
                f"Supplied starting_state repository_path '{starting_state.repository_path}' "
                f"does not match live repository path '{self.repo_path}'."
            )
        if (
            definition.frozen_start_sha
            and starting_state.starting_git_sha != definition.frozen_start_sha
        ):
            raise ReplayStateMismatchError(
                f"Supplied starting_state SHA '{starting_state.starting_git_sha}' "
                f"does not match definition frozen_start_sha '{definition.frozen_start_sha}'."
            )

        v_ckpt_id, v_state_digest = _verify_and_resolve_ledger_state(
            db=self.db,
            project_id=live_ident.project_id,
            checkpoint_id=starting_state.checkpoint_id,
            state_digest=starting_state.state_digest,
        )

        return FrozenStartingState(
            project_id=starting_state.project_id,
            repository_path=str(self.repo_path),
            starting_git_sha=starting_state.starting_git_sha,
            root_commit=starting_state.root_commit,
            checkpoint_id=v_ckpt_id,
            state_digest=v_state_digest,
        )

    def bind_definition(
        self,
        definition: ReplayDefinition,
        starting_state: FrozenStartingState | None = None,
    ) -> ReplayDefinition:
        """Bind a ReplayDefinition to verified live Project starting state.

        If starting_state is provided, verifies it against live repository and
        Project Ledger. If not provided, verifies if definition already contains
        a frozen_starting_state, or freezes fresh state from the live project.

        Returns an immutable effective ReplayDefinition bound to the verified state.
        """
        live_ident = self.registry.verify_project_repository(
            project_key=definition.project_key,
            repo_path=self.repo_path,
            require_clean=True,
        )

        if starting_state is not None:
            resolved_state = self._verify_supplied_starting_state(
                starting_state, live_ident, definition
            )
        elif definition.frozen_starting_state is not None:
            resolved_state = self._verify_supplied_starting_state(
                definition.frozen_starting_state, live_ident, definition
            )
        else:
            resolved_state = self.freeze_starting_state(
                project_key=definition.project_key,
                starting_sha=definition.frozen_start_sha or None,
                checkpoint_id=definition.checkpoint_id,
                state_digest=definition.state_digest,
                db=self.db,
            )

        return definition.bind_starting_state(resolved_state)

    def execute_replay(
        self,
        definition: ReplayDefinition,
        *,
        starting_state: FrozenStartingState | None = None,
        evidence_dir: Path | str | None = None,
        classifier_fn: Callable[[dict[str, Any]], FailureClassification | None] | None = None,
        _test_fixture_strategy_fn: Callable[[DisposableWorkspace], Any] | None = None,
    ) -> ReplayExecutionRecord:
        """Execute one replay run from frozen state and return its measurement record.

        Enforces all Section 1-7 invariants:
        - Creates canonical effective ReplayDefinition bound to verified frozen starting state.
        - Re-verifies live repository identity before workspace creation (no bypass).
        - Re-verifies supplied FrozenStartingState and claims against live repository and ledger.
        - Validates evidence_dir is outside authoritative repository and worktrees before execution.
        - Confines production mutating execution via existing C11 sandbox containment.
        - Prohibits production callback bypass (only explicit private _test_fixture_strategy_fn).
        - Captures strategy execution result as durable evidence;
          failed command prevents verified_success.
        - Measures only directly observed quantities; leaves unobserved metrics UNKNOWN.
        - Preserves raw verification evidence; does not misclassify plain failures
          as CANDIDATE_DEFECT.
        """
        # 0. Safety check: Validate evidence_dir BEFORE executing or creating workspace
        if evidence_dir is not None:
            ev_path = Path(evidence_dir).resolve()
            auth_repo = self.repo_path.resolve()
            if ev_path == auth_repo or ev_path.is_relative_to(auth_repo):
                raise ReplaySafetyViolationError(
                    f"Refusing evidence destination inside authoritative repository: {ev_path}"
                )
            from saberops.replay.workspace import get_replay_worktree_base

            wt_base = get_replay_worktree_base(auth_repo).resolve()
            if ev_path == wt_base or ev_path.is_relative_to(wt_base):
                raise ReplaySafetyViolationError(
                    f"Refusing evidence destination inside transient replay workspaces: {ev_path}"
                )

        # 1. Bind definition to verified frozen state (enforcing all identity and ledger invariants)
        bound_definition = self.bind_definition(definition, starting_state=starting_state)
        frozen_state = bound_definition.frozen_starting_state
        assert frozen_state is not None

        # Snapshot authoritative baseline to verify Invariants A, F, G afterwards
        auth_head_before = GitManager.get_head_commit(self.repo_path)

        # 2. Setup execution identity (separate from content identity)
        execution_id = f"replay_{uuid.uuid4().hex[:12]}"
        start_time_iso = datetime.now(UTC).isoformat()
        t0 = time.perf_counter()

        verification_passed = False
        failure_class: FailureClassification | None = None
        candidate_sha: str | None = None
        workspace_path_str: str = ""
        tool_execution_seconds: float = 0.0
        verifier_evidence: dict[str, Any] = {}
        strategy_passed = True
        strategy_evidence: dict[str, Any] | None = None

        # 3. Create disposable replay workspace & execute
        overhead_start = time.perf_counter()
        with DisposableWorkspace.create(
            repo_path=self.repo_path,
            starting_state=frozen_state,
            execution_id=execution_id,
        ) as ws:
            deterministic_overhead = time.perf_counter() - overhead_start
            workspace_path_str = str(ws.worktree_path)

            tool_exec_start = time.perf_counter()

            if _test_fixture_strategy_fn is not None:
                # Private test-only fixture callback seam: NOT for production or CLI replay.
                with ws.quarantined_git():
                    _test_fixture_strategy_fn(ws)
                strategy_evidence = {
                    "test_fixture": True,
                    "passed": True,
                }
            elif "command" in bound_definition.strategy_params:
                # Production mutating strategy: executes via existing C11 sandbox containment
                from saberops.process import run_sandboxed_process
                from saberops.sandbox import (
                    NetworkPolicy,
                    ProcessPolicy,
                    SandboxPolicy,
                    VerifiedTraceMechanism,
                    attach_sandbox_interceptor,
                    grant_for_sandbox,
                    retire_boundary_installation,
                )

                cmd = list(bound_definition.strategy_params["command"])
                expected_exit = int(bound_definition.strategy_params.get("expected_exit_code", 0))
                policy = SandboxPolicy(
                    repo_root=str(ws.worktree_path),
                    readable_paths=("**",),
                    writable_paths=("**",),
                    deny_paths=(".git/**", ".github/**"),
                    network=NetworkPolicy(mode="DENY"),
                    credential_refs=(),
                    process=ProcessPolicy(
                        allow_host_shell=False,
                        sandboxed_shell=True,
                    ),
                )
                grant = grant_for_sandbox(policy)
                interceptor = attach_sandbox_interceptor(
                    policy=policy,
                    grant=grant,
                    attempt_id=execution_id,
                    run_id=execution_id,
                )
                trace_mech = VerifiedTraceMechanism(interceptor=interceptor)
                with ws.quarantined_git():
                    try:
                        proc_res = run_sandboxed_process(
                            cmd,
                            cwd=ws.worktree_path,
                            policy=policy,
                            sandbox_grant=grant,
                            trace_mechanism=trace_mech,
                            attempt_id=execution_id,
                            run_id=execution_id,
                        )
                    finally:
                        retire_boundary_installation(
                            attempt_id=execution_id,
                            run_id=execution_id,
                            policy=policy,
                            grant=grant,
                        )

                passed_exit = (proc_res.exit_code == expected_exit)
                passed_clean = (
                    not proc_res.timed_out
                    and not proc_res.launch_failed
                    and not proc_res.termination_incomplete
                )
                strategy_passed = passed_exit and passed_clean

                strategy_evidence = {
                    "command": cmd,
                    "exit_code": proc_res.exit_code,
                    "expected_exit_code": expected_exit,
                    "passed": strategy_passed,
                    "execution_failed": not strategy_passed,
                    "timed_out": proc_res.timed_out,
                    "launch_failed": proc_res.launch_failed,
                    "termination_incomplete": proc_res.termination_incomplete,
                    "stdout": proc_res.stdout,
                    "stderr": proc_res.stderr,
                    "duration_seconds": proc_res.duration_seconds,
                }
                tool_execution_seconds = proc_res.duration_seconds

            # Candidate commit identity: None unless trusted Orch commit exists
            candidate_sha = None
            if tool_execution_seconds == 0.0:
                tool_execution_seconds = time.perf_counter() - tool_exec_start

            # Capture workspace result digest BEFORE verifier execution
            workspace_result_digest_before = compute_workspace_result_digest(ws.worktree_path)

            # 4. Deterministic verification with quarantined Git pointer
            validation_start = time.perf_counter()
            with ws.quarantined_git():
                verification_passed, failure_class, verifier_evidence = self.run_verifier(
                    workspace_path=ws.worktree_path,
                    verifier=bound_definition.verifier,
                    classifier_fn=classifier_fn,
                )
            validation_seconds = time.perf_counter() - validation_start

            # Invariant: Verifier must NOT change measured result
            workspace_result_digest_after = compute_workspace_result_digest(ws.worktree_path)
            if workspace_result_digest_after != workspace_result_digest_before:
                raise ReplaySafetyViolationError(
                    f"CRITICAL: Contained verifier mutated workspace result! "
                    f"Before: {workspace_result_digest_before}, "
                    f"After: {workspace_result_digest_after}"
                )
            workspace_result_digest = workspace_result_digest_before

        # Overall verified success requires every required execution and
        # verification condition to pass
        overall_verified_success = strategy_passed and verification_passed

        # 5. Measure wall clock & construct measurement record
        total_wall_clock = time.perf_counter() - t0
        end_time_iso = datetime.now(UTC).isoformat()

        # Invariant checks: Authoritative Project truth must NOT be mutated!
        auth_head_after = GitManager.get_head_commit(self.repo_path)
        if auth_head_before != auth_head_after:
            raise ReplaySafetyViolationError(
                f"CRITICAL: Authoritative repository HEAD moved from {auth_head_before} "
                f"to {auth_head_after} during replay execution!"
            )

        if not GitManager.is_clean(self.repo_path):
            raise ReplaySafetyViolationError(
                "CRITICAL: Authoritative repository checkout is dirty after replay execution!"
            )

        # Measure only what is actually observed (no fabricated attempts/turns/interventions).
        # F-04: the dedicated verifier-toolchain fingerprint is captured as
        # execution evidence.  A missing/invalid dedicated toolchain yields the
        # TOOLCHAIN_UNAVAILABLE sentinel — never a SHA over an empty package set.
        measurement = MeasurementRecord(
            verified_success=overall_verified_success,
            failure_classification=failure_class,
            total_wall_clock_seconds=round(total_wall_clock, 4),
            deterministic_overhead_seconds=round(deterministic_overhead, 4),
            tool_execution_seconds=round(tool_execution_seconds, 4),
            deterministic_validation_seconds=round(validation_seconds, 4),
            verifier_toolchain_fingerprint=fingerprint_or_unavailable(),
        )

        record = ReplayExecutionRecord(
            execution_id=execution_id,
            task_id=bound_definition.task_id,
            project_key=bound_definition.project_key,
            definition_digest=bound_definition.definition_digest,
            strategy_id=bound_definition.strategy_id,
            start_time=start_time_iso,
            end_time=end_time_iso,
            starting_sha=frozen_state.starting_git_sha,
            candidate_sha=candidate_sha,
            workspace_path=workspace_path_str,
            workspace_result_digest=workspace_result_digest,
            verified_success=overall_verified_success,
            failure_classification=failure_class,
            measurement=measurement,
            frozen_starting_state=frozen_state,
            frozen_state_digest=frozen_state.digest,
            task_definition_digest=bound_definition.task_definition_digest,
            effective_definition=bound_definition,
            strategy_evidence=strategy_evidence,
            verifier_evidence=verifier_evidence,
        )

        # 6. Preserve replay evidence if requested (already verified safe outside repo)
        if evidence_dir is not None:
            ev_path = Path(evidence_dir).resolve()
            ev_path.mkdir(parents=True, exist_ok=True)
            (ev_path / f"{execution_id}_measurement.json").write_text(
                json.dumps(measurement.to_dict(), indent=2), encoding="utf-8"
            )
            (ev_path / f"{execution_id}_record.json").write_text(
                json.dumps(record.to_dict(), indent=2), encoding="utf-8"
            )

        return record
