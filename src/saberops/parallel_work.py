"""Dependency-aware teams, using the existing governed worker lifecycle.

Planning/review practices are adapted from Superpowers; isolated sessions from
Agent Orchestrator; ordered integration from Gas Town. No upstream code is run.
Worker candidates are internal. Only the parent run can become owner-acceptable.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from saberops.config import RunConfig, reconstruct_run_config
from saberops.db import Database, current_iso_timestamp
from saberops.git import GitManager, assert_repository_at_revision
from saberops.models import TERMINAL_RUN_STATUSES, PlanStepStatus, Run, RunStatus
from saberops.planning import mark_step, ready_dependents
from saberops.process import run_process
from saberops.slicing import ProposedPackage, validate_proposal

if TYPE_CHECKING:
    from saberops.supervisor import RunSupervisor


def validate_work_config(config: RunConfig) -> None:
    """The same fail-closed execution contract at admission and adoption."""
    if config.execution_mode not in ("single", "parallel"):
        raise ValueError("Unknown work execution mode.")
    if type(config.max_parallel_workers) is not int or not 2 <= config.max_parallel_workers <= 4:
        raise ValueError("Choose two to four parallel workers.")
    if config.execution_mode == "parallel":
        validate_team_plan(config.parallel_plan)
        if config.parallel_parent_run_id or config.autonomous_runtime is not None:
            raise ValueError("Parallel teams require a top-level supervised Work thread.")
    elif config.parallel_plan:
        raise ValueError("Single-worker work cannot carry a parallel plan.")
    if config.parallel_parent_run_id is not None:
        if (
            not isinstance(config.parallel_parent_run_id, str)
            or not config.parallel_parent_run_id
            or type(config.parallel_task_index) is not int
            or not 0 <= config.parallel_task_index < 6
            or config.continuation
        ):
            raise ValueError("Invalid internal team worker reference.")
    elif config.parallel_task_index is not None:
        raise ValueError("A task index requires a parent team.")


def validate_team_plan(raw: object) -> tuple[dict[str, Any], ...]:
    """Validate model output before persistence or any worker launch."""
    if not isinstance(raw, (list, tuple)) or not 2 <= len(raw) <= 6:
        raise ValueError("Parallel work needs a plan with two to six tasks.")
    proposals: list[ProposedPackage] = []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each parallel task must be an object.")
        title, goal = item.get("title"), item.get("goal")
        criteria, paths = item.get("acceptance_criteria"), item.get("relevant_paths")
        deps = item.get("dependency_indexes", [])
        if (
            not isinstance(title, str)
            or not isinstance(goal, str)
            or len(title) > 120
            or len(goal) > 8000
            or not isinstance(criteria, list)
            or not 1 <= len(criteria) <= 12
            or not all(isinstance(c, str) and 0 < len(c) <= 1000 for c in criteria)
            or not isinstance(paths, list)
            or not 1 <= len(paths) <= 40
            or not isinstance(deps, list)
            or not all(type(d) is int for d in deps)
        ):
            raise ValueError("Tasks need a title, bounded goal, checks, paths and dependencies.")
        for path in paths:
            if not isinstance(path, str) or not path or len(path) > 250:
                raise ValueError("Task paths must be relative files or directory/** scopes.")
            literal = path[:-3] if path.endswith("/**") else path
            parts = literal.split("/")
            if (
                PurePosixPath(literal).is_absolute()
                or any(p in ("", ".", "..") for p in parts)
                or any(c in literal for c in "*?[]\\\x00")
                or parts[0] in (".git", ".orch", ".orch-worktrees", ".worktrees")
            ):
                raise ValueError(f"Unsafe or ambiguous task path: {path}")
        proposal = ProposedPackage(title, goal, tuple(criteria), tuple(deps), tuple(paths))
        proposals.append(proposal)
        normalized.append(
            {
                "title": title.strip(),
                "goal": goal.strip(),
                "acceptance_criteria": criteria,
                "relevant_paths": paths,
                "dependency_indexes": sorted(set(deps)),
            }
        )
    validate_proposal(proposals)
    return tuple(normalized)


def team_proposals(config: RunConfig) -> list[ProposedPackage]:
    return [
        ProposedPackage(
            p["title"],
            p["goal"],
            tuple(p["acceptance_criteria"]),
            tuple(p["dependency_indexes"]),
            tuple(p["relevant_paths"]),
        )
        for p in validate_team_plan(config.parallel_plan)
    ]


def path_owned(path: str, scope: str) -> bool:
    return path == scope or (scope.endswith("/**") and path.startswith(scope[:-2]))


def scopes_overlap(left: list[str], right: list[str]) -> bool:
    return any(
        path_owned(a[:-3] if a.endswith("/**") else a, b)
        or path_owned(b[:-3] if b.endswith("/**") else b, a)
        for a in left
        for b in right
    )


def team_children(db: Database, run_id: str) -> list[str]:
    return list(
        dict.fromkeys(
            str(e.payload["child_run_id"])
            for e in db.get_complete_events_for_run(run_id)
            if e.event_type == "team_worker_created"
            and e.payload
            and isinstance(e.payload.get("child_run_id"), str)
        )
    )


def team_authorized_bases(db: Database, run_id: str) -> set[str]:
    """Prove the combined start against every worker and the actual Git merge chain.

    A team event alone grants no candidate authority. Acceptance/publication
    must independently reconstruct this proof, just as they do for failover.
    """
    from saberops.candidate_diff import CHANGED_PATHS_MAX, project_candidate_diff
    from saberops.provenance import assert_candidate_provenance, failover_authorized_bases

    run = db.get_run(run_id)
    if not run or not run.config_json:
        return set()
    try:
        config = json.loads(run.config_json)
        if config.get("execution_mode") != "parallel" or config.get("continuation"):
            return set()
        plan = validate_team_plan(config.get("parallel_plan"))
        events = db.get_complete_events_for_run(run_id)
        finished = [e.payload for e in events if e.event_type == "team_integrated"]
        merged = [e.payload for e in events if e.event_type == "team_worker_integrated"]
        if len(finished) != 1 or not finished[0] or len(merged) != len(plan):
            return set()
        previous = run.base_commit
        anchors = {previous}
        seen: set[int] = set()
        for evidence in merged:
            if not evidence:
                return set()
            index = evidence.get("task_index")
            if type(index) is not int or index in seen or not 0 <= index < len(plan):
                return set()
            if not set(plan[index]["dependency_indexes"]) <= seen:
                return set()
            child = db.get_run(str(evidence.get("child_run_id", "")))
            if (
                not child
                or child.status != RunStatus.COMPLETED
                or not child.config_json
                or child.project_id != run.project_id
                or child.base_commit not in anchors
            ):
                return set()
            worker_config = json.loads(child.config_json)
            if (
                worker_config.get("parallel_parent_run_id") != run_id
                or worker_config.get("parallel_task_index") != index
            ):
                return set()
            expected_policy = replace(
                reconstruct_run_config(worker_config),
                task=str(config["task"]),
                target_repo=str(config["target_repo"]),
                execution_mode="parallel",
                parallel_plan=tuple(config["parallel_plan"]),
                parallel_parent_run_id=None,
                parallel_task_index=None,
                publish_candidate=bool(config.get("publish_candidate", False)),
            )
            if json.loads(json.dumps(expected_policy.as_dict())) != config:
                return set()
            if not any(
                e.event_type == "team_worker_created"
                and e.payload
                and e.payload.get("child_run_id") == child.id
                and e.payload.get("task_index") == index
                for e in events
            ):
                return set()
            diff = project_candidate_diff(db, child.id)
            if (
                not diff.available
                or len(diff.changed_paths) >= CHANGED_PATHS_MAX
                or not diff.candidate_sha
                or diff.candidate_sha != evidence.get("candidate_sha")
            ):
                return set()
            if any(
                not any(path_owned(path, scope) for scope in plan[index]["relevant_paths"])
                for path in diff.changed_paths
            ):
                return set()
            attempts = db.get_attempts_for_run(child.id)
            attempt = next(a for a in reversed(attempts) if a.commit_sha == diff.candidate_sha)
            assert_candidate_provenance(
                child,
                attempt,
                extra_authorized_bases=failover_authorized_bases(db, child.id)
                | {a.commit_sha for a in attempts if a.commit_sha and a.id != attempt.id},
            )
            merged_sha = evidence.get("integration_sha")
            if not isinstance(merged_sha, str):
                return set()
            parents = run_process(
                ["git", "rev-list", "--parents", "-n", "1", merged_sha],
                cwd=Path(run.target_repo),
                timeout=10,
            )
            if not parents.passed:
                return set()
            if merged_sha == previous:
                if not GitManager.is_ancestor(run.target_repo, diff.candidate_sha, previous):
                    return set()
            elif parents.stdout.split() != [merged_sha, previous, diff.candidate_sha]:
                return set()
            previous = merged_sha
            anchors.add(previous)
            seen.add(index)
        if (
            finished[0].get("candidate_sha") != previous
            or finished[0].get("branch") != f"orch/{run_id}/integration"
        ):
            return set()
        return {previous}
    except (ValueError, TypeError, KeyError, RuntimeError, StopIteration):
        return set()


def validate_team_child(
    db: Database, config: RunConfig, *, existing_run_id: str | None = None
) -> Run | None:
    if not config.parallel_parent_run_id:
        return None
    parent = db.get_run(config.parallel_parent_run_id)
    if parent is None or parent.status != RunStatus.RUNNING or not parent.config_json:
        raise ValueError("The parent team is no longer running.")
    owner = db.get_execution_owner(parent.id)
    if owner is not None and owner.is_cancelling:
        raise ValueError("The parent team is stopping.")
    frozen = json.loads(parent.config_json)
    if frozen.get("execution_mode") != "parallel" or config.execution_mode != "single":
        raise ValueError("Invalid internal team worker.")
    plan = validate_team_plan(frozen.get("parallel_plan"))
    if type(config.parallel_task_index) is not int or not 0 <= config.parallel_task_index < len(
        plan
    ):
        raise ValueError("Team worker references an unknown task.")
    for event in db.get_complete_events_for_run(parent.id):
        if (
            event.event_type == "team_worker_created"
            and event.payload
            and event.payload.get("task_index") == config.parallel_task_index
            and event.payload.get("child_run_id") != existing_run_id
        ):
            raise ValueError("This team task already has a worker. Retry the parent Work.")
    target = Path(config.target_repo)
    if GitManager.get_current_branch(target) != f"orch/{parent.id}/integration":
        raise ValueError("A team worker must start from its parent's integration worktree.")
    original_common = run_process(
        ["git", "rev-parse", "--git-common-dir"], cwd=Path(parent.target_repo)
    )
    child_common = run_process(["git", "rev-parse", "--git-common-dir"], cwd=target)
    if (
        not original_common.passed
        or not child_common.passed
        or (Path(parent.target_repo) / original_common.stdout.strip()).resolve()
        != (target / child_common.stdout.strip()).resolve()
    ):
        raise ValueError("Team worker repository does not match its parent.")
    expected = replace(
        config,
        task=str(frozen["task"]),
        target_repo=str(frozen["target_repo"]),
        execution_mode="parallel",
        parallel_plan=tuple(frozen["parallel_plan"]),
        parallel_parent_run_id=None,
        parallel_task_index=None,
        publish_candidate=bool(frozen.get("publish_candidate", False)),
    )
    # All execution identity, gate, review, and data decisions remain run-frozen.
    if json.loads(json.dumps(expected.as_dict())) != frozen:
        raise ValueError("Team worker policy differs from its parent's frozen policy.")
    return parent


def execute_team(
    db: Database, config: RunConfig, run_id: str, worktree_base: Path, supervisor: RunSupervisor
) -> str:
    """Launch durable workers in bounded waves; integrate only verified commits.

    The supplied supervisor is the normal RunSupervisor. The parent retains its
    original base and runs its ordinary worker/gate/reviewer on the resulting
    commit after this stage. Source checkout and target branch never move here.
    """
    plan = validate_team_plan(config.parallel_plan)
    if not 2 <= config.max_parallel_workers <= 4:
        raise ValueError("Choose two to four parallel workers.")
    parent = db.get_run(run_id)
    assert parent is not None
    supervision = db.get_supervision(run_id)
    owner = db.get_execution_owner(run_id)
    if (
        supervision
        and owner
        and supervision.worker_identity is None
        and supervision.worker_pid == owner.identity.pid
    ):
        # The launch record's PID is the manager, not a dispatched worker.
        # Retain custody on execution_owners; cancellation must inspect workers
        # through the team's child owners during this stage.
        db.upsert_supervision(
            replace(
                supervision,
                worker_pid=None,
                health_reason="supervising_parallel_team",
                updated_at=current_iso_timestamp(),
            )
        )
    original = Path(parent.target_repo)
    assert_repository_at_revision(original, parent.base_commit, when="before parallel team")
    branch = f"orch/{run_id}/integration"
    integration = GitManager.create_worktree(
        original, branch, worktree_base / run_id / "integration", parent.base_commit
    )
    db.record_event(
        run_id,
        "team_started",
        payload={
            "branch": branch,
            "worktree": str(integration),
            "base_sha": parent.base_commit,
            "max_workers": config.max_parallel_workers,
            "task_count": len(plan),
        },
    )
    completed: set[int] = set()
    active: list[tuple[int, str]] = []
    try:
        while len(completed) < len(plan):
            current = db.get_run(run_id)
            if current is None or current.status != RunStatus.RUNNING:
                raise RuntimeError("Team stopped before integration completed.")
            ready = [
                i
                for i, p in enumerate(plan)
                if i not in completed and set(p["dependency_indexes"]) <= completed
            ]
            wave: list[int] = []
            for index in ready:
                if len(wave) >= config.max_parallel_workers:
                    break
                if not any(
                    scopes_overlap(plan[index]["relevant_paths"], plan[other]["relevant_paths"])
                    for other in wave
                ):
                    wave.append(index)
            if not wave:
                raise RuntimeError("No dependency-safe task is ready.")
            base_sha = GitManager.get_head_commit(integration)
            db.record_event(
                run_id,
                "team_wave_started",
                payload={"tasks": wave, "base_sha": base_sha, "concurrency": len(wave)},
            )
            steps = db.get_plan_steps(run_id)
            active = []
            for index in wave:
                spec = plan[index]
                brief = (
                    f"Team task: {spec['title']}\n\n{spec['goal']}\n\n"
                    "Implement only this task. Other workers own the other tasks.\n"
                    "Allowed changed paths: "
                    + json.dumps(spec["relevant_paths"])
                    + "\nAcceptance checks: "
                    + json.dumps(spec["acceptance_criteria"])
                    + "\nReport changes, test evidence, and remaining concerns. "
                    "Do not merge, push, or accept any branch."
                )
                child_config = replace(
                    config,
                    task=brief,
                    target_repo=str(integration),
                    execution_mode="single",
                    parallel_plan=(),
                    parallel_parent_run_id=run_id,
                    parallel_task_index=index,
                    publish_candidate=False,
                )
                child_id = supervisor.start_supervised_run(config=child_config).run_id
                active.append((index, child_id))
                mark_step(db, steps[index], PlanStepStatus.RUNNING)
            observed: dict[str, str] = {}
            reconciled_at = time.monotonic()
            while True:
                if time.monotonic() - reconciled_at >= 5:
                    for _, child_id in active:
                        supervisor.reconcile(child_id)
                    reconciled_at = time.monotonic()
                children = [db.get_run(child_id) for _, child_id in active]
                if any(child is None for child in children):
                    raise RuntimeError("A durable team worker record is missing.")
                for child in children:
                    if child and observed.get(child.id) != child.status.value:
                        observed[child.id] = child.status.value
                        db.record_event(
                            run_id,
                            "team_worker_status",
                            payload={
                                "child_run_id": child.id,
                                "status": child.status.value,
                            },
                        )
                if any(
                    child
                    and child.status in TERMINAL_RUN_STATUSES
                    and child.status != RunStatus.COMPLETED
                    for child in children
                ):
                    failed = next(
                        child
                        for child in children
                        if child
                        and child.status in TERMINAL_RUN_STATUSES
                        and child.status != RunStatus.COMPLETED
                    )
                    failed_index = next(i for i, identity in active if identity == failed.id)
                    mark_step(db, steps[failed_index], PlanStepStatus.FAILED,
                              blocker=failed.result_summary or "Worker failed")
                    raise RuntimeError(f"Worker failed: {failed.result_summary or failed.id}")
                if all(child and child.status == RunStatus.COMPLETED for child in children):
                    break
                parent_now = db.get_run(run_id)
                if parent_now is None or parent_now.status != RunStatus.RUNNING:
                    raise RuntimeError("Team stopped while workers were running.")
                time.sleep(0.25)
            # Stable plan order, never completion order. Never merge a live branch.
            for index, child_id in active:
                from saberops.candidate_diff import CHANGED_PATHS_MAX, project_candidate_diff

                diff = project_candidate_diff(db, child_id)
                if not diff.available or not diff.candidate_sha:
                    raise RuntimeError(f"Worker {plan[index]['title']} has no verified candidate.")
                if len(diff.changed_paths) >= CHANGED_PATHS_MAX:
                    raise RuntimeError(
                        "Worker changed too many files to validate its complete scope."
                    )
                if any(
                    not any(path_owned(path, scope) for scope in plan[index]["relevant_paths"])
                    for path in diff.changed_paths
                ):
                    raise RuntimeError(
                        f"Worker {plan[index]['title']} changed another task's files."
                    )
                if not GitManager.is_ancestor(original, base_sha, diff.candidate_sha):
                    raise RuntimeError("Worker candidate is not descended from its wave base.")
                db.record_event(
                    run_id,
                    "team_merge_started",
                    payload={
                        "task_index": index,
                        "child_run_id": child_id,
                        "candidate_sha": diff.candidate_sha,
                    },
                )
                merged = run_process(
                    [
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "merge",
                        "--no-ff",
                        "--no-edit",
                        diff.candidate_sha,
                    ],
                    cwd=integration,
                    timeout=60,
                )
                if not merged.passed:
                    conflicts = run_process(
                        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=integration
                    ).stdout.splitlines()
                    run_process(["git", "merge", "--abort"], cwd=integration)
                    db.record_event(
                        run_id,
                        "team_merge_conflict",
                        payload={
                            "task_index": index,
                            "child_run_id": child_id,
                            "files": conflicts,
                            "reason": (merged.stderr or merged.stdout)[-2000:],
                        },
                    )
                    raise RuntimeError("Integration conflict; worker branches are preserved.")
                sha = GitManager.get_head_commit(integration)
                db.record_event(
                    run_id,
                    "team_worker_integrated",
                    payload={
                        "task_index": index,
                        "child_run_id": child_id,
                        "candidate_sha": diff.candidate_sha,
                        "integration_sha": sha,
                        "changed_paths": list(diff.changed_paths),
                    },
                )
                mark_step(db, steps[index], PlanStepStatus.COMPLETED)
                completed.add(index)
            active = []
            ready_dependents(db, run_id)
        result = GitManager.get_head_commit(integration)
        assert_repository_at_revision(original, parent.base_commit, when="after team integration")
        db.record_event(
            run_id,
            "team_integrated",
            payload={
                "candidate_sha": result,
                "branch": branch,
                "worktree": str(integration),
                "validation": "pending_parent_gate_and_review",
            },
        )
        return result
    except Exception as exc:
        for _, child_id in active:
            child = db.get_run(child_id)
            if child and child.status not in TERMINAL_RUN_STATUSES:
                supervisor.cancel(child_id)
        for step in db.get_plan_steps(run_id):
            if step.status not in (PlanStepStatus.COMPLETED, PlanStepStatus.FAILED):
                mark_step(db, step, PlanStepStatus.BLOCKED, blocker=str(exc)[:1000])
        db.record_event(
            run_id,
            "team_failed",
            payload={
                "reason": str(exc)[:2000],
                "worktree": str(integration),
                "branches_preserved": True,
            },
        )
        raise
