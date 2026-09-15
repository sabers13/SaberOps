"""Minimal durable provenance binding objective -> run -> candidate.

The single failure this exists to make impossible is::

    objective A -> failed run A -> candidate produced by unrelated run B
                -> accepted as objective A

Every run persists the project identity it was created for plus an immutable
objective identity, and every attempt inherits both from its run.  Acceptance
and recovery re-derive the expectation from the run row and fail closed on any
mismatch.  This is intentionally *not* a governance framework: it is four
fields and one assertion.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from orchestrator_mvp.db import Database
from orchestrator_mvp.git import GitManager
from orchestrator_mvp.models import Attempt, Run
from orchestrator_mvp.project import (
    ProjectIdentity,
    ProjectIdentityError,
    canonical_repo_root,
    compute_project_identity,
    new_run_id,
    objective_id_for,
    validate_project_identity,
)


class ProvenanceError(RuntimeError):
    """Raised when a candidate cannot be proven to belong to the run/objective."""


@dataclass(frozen=True)
class CandidateProvenance:
    """The complete traceable identity of one candidate commit."""

    project_id: str | None
    objective_id: str | None
    run_id: str
    attempt_id: str
    attempt_number: int
    base_sha: str
    candidate_sha: str

    def as_dict(self) -> dict[str, object]:
        """Serialize for durable acceptance evidence."""
        return {
            "project_id": self.project_id,
            "objective_id": self.objective_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "base_sha": self.base_sha,
            "candidate_sha": self.candidate_sha,
        }


def run_project_identity(run: Run) -> ProjectIdentity | None:
    """Return the project identity persisted with ``run``, if any."""
    identity = ProjectIdentity.from_json(run.project_identity_json)
    if identity is not None and run.project_id and identity.project_id != run.project_id:
        raise ProvenanceError(
            f"Run '{run.id}' stores project '{run.project_id}' but its identity "
            f"record names '{identity.project_id}'"
        )
    return identity


def assert_run_repository(run: Run) -> Path:
    """Verify the repository at the run's persisted location is still its own.

    The location always comes from the run row -- never from the UI's active
    project, a branch label, or a directory basename.  Returns the validated
    repository path.
    """
    target = Path(run.target_repo).expanduser()
    identity = run_project_identity(run)
    if identity is None:
        # Legacy run recorded before project identity existed.  It is still
        # pinned to its own persisted repository, but there is nothing to
        # cryptographically validate against, so only the weak check applies.
        if not GitManager.is_git_repo(target):
            raise ProvenanceError(
                f"Run '{run.id}' targets '{target}', which is not a Git repository"
            )
        return target.resolve()
    try:
        validate_project_identity(identity, target)
    except ProjectIdentityError as exc:
        raise ProvenanceError(f"Run '{run.id}' repository validation failed: {exc}") from exc
    return Path(identity.canonical_path)


def assert_run_not_retargeted(run: Run) -> None:
    """Confirm the run's repository, where it still exists, is its own.

    This is the check for actions that mutate only durable run state --
    reconciliation and orphan marking -- and never touch a working tree.  A
    repository that has been moved or deleted must not block recovery, but a
    path that now holds a *different* project must fail closed rather than
    letting one project's recovery act on another project's run.
    """
    identity = run_project_identity(run)
    if identity is None:
        return
    if canonical_repo_root(run.target_repo) is None:
        return
    try:
        validate_project_identity(identity, run.target_repo)
    except ProjectIdentityError as exc:
        raise ProvenanceError(f"Run '{run.id}' repository validation failed: {exc}") from exc


def expected_objective_id(run: Run) -> str | None:
    """Return the objective identity a run must carry, if it is derivable."""
    if run.project_id:
        return objective_id_for(run.project_id, run.task)
    return run.objective_id


def assert_candidate_provenance(
    run: Run,
    attempt: Attempt,
    *,
    lineage_base_sha: str | None = None,
    extra_authorized_bases: Iterable[str] | None = None,
) -> CandidateProvenance:
    """Fail closed unless ``attempt`` is provably a candidate of ``run``.

    Missing provenance on a *legacy* row is tolerated (there is nothing to
    compare), but present-and-different is always a hard failure, and a
    provenanced run never accepts an attempt lacking provenance.

    ``extra_authorized_bases`` carries additional bases the caller has
    re-verified as failover-authorized for this run (the canonical
    FailoverEngine required-base anchor, validated against the source
    Attempt's actual base through the supervisor decision seam and
    re-checked live by the caller).  It is never ambient input: only
    durably evidenced, re-verified anchors may be supplied.
    """
    if attempt.run_id != run.id:
        raise ProvenanceError(
            f"Candidate attempt '{attempt.id}' belongs to run '{attempt.run_id}', not '{run.id}'"
        )
    if not attempt.commit_sha:
        raise ProvenanceError(f"Candidate attempt '{attempt.id}' has no candidate commit")

    if run.project_id:
        if not attempt.project_id:
            raise ProvenanceError(
                f"Candidate attempt '{attempt.id}' has no project provenance but run "
                f"'{run.id}' belongs to project '{run.project_id}'"
            )
        if attempt.project_id != run.project_id:
            raise ProvenanceError(
                f"Candidate attempt '{attempt.id}' was produced for project "
                f"'{attempt.project_id}', not run project '{run.project_id}'"
            )

    derived_objective = expected_objective_id(run)
    if run.objective_id and derived_objective and run.objective_id != derived_objective:
        raise ProvenanceError(
            f"Run '{run.id}' objective identity '{run.objective_id}' does not match its "
            f"recorded task; the objective was altered after the run was created"
        )
    run_objective = run.objective_id or derived_objective
    if run_objective:
        if not attempt.objective_id:
            raise ProvenanceError(
                f"Candidate attempt '{attempt.id}' has no objective provenance but run "
                f"'{run.id}' pursues objective '{run_objective}'"
            )
        if attempt.objective_id != run_objective:
            raise ProvenanceError(
                f"Candidate attempt '{attempt.id}' was produced for objective "
                f"'{attempt.objective_id}', not run objective '{run_objective}'"
            )

    authorized_bases = {run.base_commit}
    if lineage_base_sha is not None:
        authorized_bases.add(lineage_base_sha)
    if extra_authorized_bases is not None:
        authorized_bases.update(
            base for base in extra_authorized_bases if isinstance(base, str) and base
        )
    if attempt.base_sha and attempt.base_sha not in authorized_bases:
        raise ProvenanceError(
            f"Candidate attempt '{attempt.id}' was based on {attempt.base_sha[:8]}, "
            f"not the authorized base {run.base_commit[:8]} of run '{run.id}'"
        )

    return CandidateProvenance(
        project_id=run.project_id,
        objective_id=run_objective,
        run_id=run.id,
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        base_sha=attempt.base_sha or run.base_commit,
        candidate_sha=attempt.commit_sha,
    )


@dataclass(frozen=True)
class RunProvenance:
    """Freshly minted identity for a run about to be created."""

    identity: ProjectIdentity
    run_id: str
    objective_id: str

    @property
    def project_id(self) -> str:
        """Convenience accessor for the owning project."""
        return self.identity.project_id


def mint_run_provenance(repo_path: Path | str, task: str) -> RunProvenance:
    """Derive the project identity, run ID, and objective ID for a new run.

    The run ID embeds the project so that every later action can resolve the
    run back to its repository without consulting the UI's active project.
    """
    identity = compute_project_identity(repo_path)
    return RunProvenance(
        identity=identity,
        run_id=new_run_id(identity),
        objective_id=objective_id_for(identity.project_id, task),
    )


def failover_authorized_bases(db: Database, run_id: str) -> set[str]:
    """Return the run's durably failover-authorized base anchors.

    Reads this run's ``autonomous_supervisor_decision`` events and
    keeps a ``required_base_sha`` anchor only when the decision names
    its failover source Attempt AND that Attempt's durable base still
    equals the anchor (re-verified live, never trusted from the event
    alone).  A proven checkpoint continuation additionally authorizes
    its exact start SHA, but only when the lifecycle's
    ``autonomous_failover_start_authorized`` record names an
    authorized anchor for the same source AND the start is the
    anchor itself or is proven by repository ancestry to derive from
    it.  Publication and acceptance consult this set so a
    failover-legitimate Attempt B (based at its validated anchor or
    continuation start, not at the run base) remains provably a
    candidate of its run.  Anything unverifiable contributes nothing.
    """
    authorized: set[str] = set()
    try:
        events = db.get_complete_events_for_run(run_id)
    except Exception:
        return authorized
    for event in events:
        if event.event_type != "autonomous_supervisor_decision":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        anchor = payload.get("required_base_sha")
        source_id = payload.get("failover_from_attempt_id")
        if not anchor or not isinstance(anchor, str) or not source_id:
            continue
        try:
            source = db.get_attempt(str(source_id))
        except Exception:
            continue
        if source is not None and source.base_sha == anchor:
            authorized.add(anchor)
    if not authorized:
        return authorized
    for event in events:
        if event.event_type != "autonomous_failover_start_authorized":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        anchor = payload.get("required_base_sha")
        start = payload.get("start_sha")
        source_id = payload.get("failover_from_attempt_id")
        if (
            not anchor
            or not isinstance(anchor, str)
            or anchor not in authorized
            or not start
            or not isinstance(start, str)
            or not source_id
        ):
            continue
        try:
            source = db.get_attempt(str(source_id))
        except Exception:
            continue
        if source is None or source.base_sha != anchor:
            continue
        if start == anchor:
            authorized.add(start)
            continue
        try:
            run = db.get_run(run_id)
            repo = run.target_repo if run is not None else None
            if repo and GitManager.is_ancestor(repo, anchor, start):
                authorized.add(start)
        except Exception:
            continue
    return authorized
