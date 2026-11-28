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

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from saberops.db import Database
from saberops.git import GitManager
from saberops.models import Attempt, Run
from saberops.project import (
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

    The ordinary (non-autonomous) continuation that a scheduler-dispatched
    job takes after quota exhaustion records ``failover_start_authorized``
    with the same anchor / start / checkpoint triple.  It is verified by the
    same rules here, so both paths share one proof and one reader.
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
    for event in events:
        if event.event_type == "failover_start_authorized":
            _authorize_ordinary_continuation_base(db, run_id, event, authorized)
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


def _authorize_ordinary_continuation_base(
    db: Database, run_id: str, event: Any, authorized: set[str]
) -> None:
    """Verify one ``failover_start_authorized`` record, fail-closed.

    The record must name its source Attempt, that Attempt's durable base must
    still equal the named anchor, a worker checkpoint of this run must exist
    for the source with exactly that checkpoint SHA, and the start SHA must be
    the anchor itself or a descendant of it in the run's repository.  Any gap
    contributes nothing; unverifiable evidence never grants candidate
    authority.
    """
    payload = event.payload
    if not isinstance(payload, dict):
        return
    anchor = payload.get("required_base_sha")
    start = payload.get("start_sha")
    source_id = payload.get("failover_from_attempt_id")
    checkpoint_sha = payload.get("checkpoint_sha")
    if not all(
        isinstance(value, str) and value
        for value in (anchor, start, source_id, checkpoint_sha)
    ):
        return
    try:
        source = db.get_attempt(str(source_id))
    except Exception:
        return
    if source is None or source.base_sha != anchor:
        return
    if source.run_id != run_id:
        return
    try:
        checkpoint = db.get_worker_checkpoint_by_sha(run_id, str(checkpoint_sha))
    except Exception:
        return
    if (
        checkpoint is None
        or checkpoint.source_attempt_id != str(source_id)
        or checkpoint.source_base_sha != anchor
    ):
        return
    if start == anchor:
        authorized.add(str(start))
        return
    try:
        run = db.get_run(run_id)
        repo = run.target_repo if run is not None else None
        if repo and GitManager.is_ancestor(repo, anchor, str(start)):
            authorized.add(str(start))
    except Exception:
        return


def execution_authorized_bases(db: Database, run_id: str) -> set[str]:
    """Shared publication/acceptance proof for governed execution start anchors."""
    authorized = failover_authorized_bases(db, run_id)
    authorized.update(continuation_authorized_bases(db, run_id))
    return authorized


def continuation_authorized_bases(db: Database, run_id: str) -> set[str]:
    """Return the saved candidate a continuation run may legitimately build on.

    A continuation run (``resume_job from=last_candidate`` or the
    reject-with-a-note flow) starts its first attempt at the *parent* run's
    saved candidate rather than at the successor's own base commit, so
    acceptance and publication must recognize that start.  The reference is
    durable in the successor's frozen configuration; it authorizes exactly one
    base and only when every link re-verifies:

    * the reference names a parent run, attempt, candidate and base,
    * the successor's own base is that same base,
    * the parent run records the same base, belongs to the same durable job
      and the same repository,
    * the named parent attempt's commit is exactly the referenced candidate,
    * that candidate descends from the base in the repository.

    Anything missing or inconsistent contributes nothing, so a candidate
    built on unrelated work is still refused.  The caller's check remains an
    exact membership test (the attempt's own base must be in the set).
    """
    authorized: set[str] = set()
    run = db.get_run(run_id)
    if run is None or not run.config_json:
        return authorized
    try:
        config = json.loads(run.config_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return authorized
    if not isinstance(config, dict):
        return authorized
    reference = config.get("continuation")
    if not isinstance(reference, dict):
        return authorized
    parent_id = reference.get("parent_run_id")
    attempt_id = reference.get("attempt_id")
    candidate_sha = reference.get("candidate_sha")
    base_sha = reference.get("base_sha")
    if not (
        isinstance(parent_id, str)
        and parent_id
        and isinstance(attempt_id, str)
        and attempt_id
        and isinstance(candidate_sha, str)
        and candidate_sha
        and isinstance(base_sha, str)
        and base_sha
    ):
        return authorized
    if base_sha != run.base_commit:
        return authorized
    parent = db.get_run(parent_id)
    if parent is None or parent.base_commit != base_sha:
        return authorized
    from saberops.jobs.store import find_job_for_run

    def _job_of(record: Run) -> str | None:
        if record.job_id:
            return record.job_id
        job = find_job_for_run(db, record.id)
        return job.job_id if job is not None else None

    if _job_of(parent) != _job_of(run):
        return authorized
    try:
        if Path(parent.target_repo).resolve() != Path(run.target_repo).resolve():
            return authorized
        attempt = db.get_attempt(attempt_id)
    except Exception:
        return authorized
    if (
        attempt is None
        or attempt.run_id != parent.id
        or attempt.commit_sha != candidate_sha
    ):
        return authorized
    try:
        if not GitManager.is_ancestor(run.target_repo, base_sha, candidate_sha):
            return authorized
    except Exception:
        return authorized
    authorized.add(candidate_sha)
    return authorized


# ---------------------------------------------------------------------------
# Git-visible provenance (lane H44, ADR-0102)
#
# The store already holds every fact about an accepted candidate; Git held
# only a commit message.  Two artifacts make "why does this line exist?"
# answerable from a checkout:
#
# * the candidate commit carries a trailer block written at commit time with
#   what is already known then (job, run, attempt, base, worker, model), and
# * acceptance adds a note under :data:`NOTES_REF` with the facts that can
#   only exist afterwards (gate outcome and receipt digest, review and
#   verdict, transcript path, accepted-at, final branch).
#
# The commit can never be reworded after the fact -- acceptance installs that
# exact SHA and review is bound to it -- so nothing after the gate can live in
# the message.  Trailers travel with the commit into every clone; notes are
# local until the owner fetches or pushes the notes ref (default: not pushed).
# ---------------------------------------------------------------------------

#: One dedicated notes ref for SaberOps acceptance evidence.
NOTES_REF: Final[str] = "refs/notes/saberops"

KEY_JOB: Final[str] = "SaberOps-Job"
KEY_RUN: Final[str] = "SaberOps-Run"
KEY_ATTEMPT: Final[str] = "SaberOps-Attempt"
KEY_BASE: Final[str] = "SaberOps-Base"
KEY_WORKER: Final[str] = "SaberOps-Worker"
KEY_MODEL: Final[str] = "SaberOps-Model"
KEY_CANDIDATE: Final[str] = "SaberOps-Candidate"
KEY_FINAL_BRANCH: Final[str] = "SaberOps-Final-Branch"
KEY_ACCEPTED_AT: Final[str] = "SaberOps-Accepted-At"
KEY_GATE: Final[str] = "SaberOps-Gate"
KEY_GATE_RECEIPT: Final[str] = "SaberOps-Gate-Receipt"
KEY_REVIEW: Final[str] = "SaberOps-Review"
KEY_REVIEW_VERDICT: Final[str] = "SaberOps-Review-Verdict"
KEY_REVIEW_FINDINGS: Final[str] = "SaberOps-Review-Findings"
KEY_TRANSCRIPT: Final[str] = "SaberOps-Transcript"
KEY_TRANSCRIPT_STATE: Final[str] = "SaberOps-Transcript-State"

#: Every key the trailer block or the acceptance note may carry.
PROVENANCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        KEY_JOB,
        KEY_RUN,
        KEY_ATTEMPT,
        KEY_BASE,
        KEY_WORKER,
        KEY_MODEL,
        KEY_CANDIDATE,
        KEY_FINAL_BRANCH,
        KEY_ACCEPTED_AT,
        KEY_GATE,
        KEY_GATE_RECEIPT,
        KEY_REVIEW,
        KEY_REVIEW_VERDICT,
        KEY_REVIEW_FINDINGS,
        KEY_TRANSCRIPT,
        KEY_TRANSCRIPT_STATE,
    }
)

#: Transcript states reported by the read path.
TRANSCRIPT_PRESENT: Final[str] = "present"
TRANSCRIPT_REMOVED: Final[str] = "removed"
TRANSCRIPT_ABSENT: Final[str] = "absent"

_TRANSCRIPT_REMOVED_MESSAGE: Final[str] = (
    "the transcript is removed: accepted-run transcripts are deleted by storage "
    "retention 90 days after acceptance (storage_cleanup)"
)


def _trailer_value(value: object) -> str:
    """Flatten one value onto a single trailer line (Git trailers never wrap)."""
    return " ".join(str(value).split())


def candidate_commit_message(
    *,
    run_id: str,
    attempt_id: str,
    attempt_number: int,
    base_sha: str,
    provider: str,
    model: str,
    job_id: str | None = None,
) -> str:
    """Return the candidate commit message with its Git-visible provenance.

    The headline keeps the historical ``orch(<run>): candidate changes ...``
    shape; the trailer paragraph names the job that dispatched the run (absent
    for a plain ``orch run``), the run, the attempt, the base it descends from
    and the worker CLI/model that produced it.
    """
    headline = (
        f"orch({run_id}): candidate changes for attempt {attempt_number} "
        f"({provider}/{model})"
    )
    trailers = []
    if job_id:
        trailers.append(f"{KEY_JOB}: {_trailer_value(job_id)}")
    trailers.extend(
        [
            f"{KEY_RUN}: {_trailer_value(run_id)}",
            f"{KEY_ATTEMPT}: {_trailer_value(attempt_id)}",
            f"{KEY_BASE}: {_trailer_value(base_sha)}",
            f"{KEY_WORKER}: {_trailer_value(provider)}",
            f"{KEY_MODEL}: {_trailer_value(model)}",
        ]
    )
    return f"{headline}\n\n" + "\n".join(trailers)


def acceptance_note_text(
    *,
    run_id: str,
    attempt_id: str,
    base_sha: str,
    candidate_sha: str,
    provider: str,
    model: str,
    final_branch: str,
    accepted_at: str,
    gate: str,
    gate_receipt_digest: str | None,
    review_id: str | None,
    review_verdict: str | None,
    finding_count: int,
    transcript_path: str | None,
    transcript_present: bool,
    job_id: str | None = None,
) -> str:
    """Render the acceptance note body: the facts that exist only after the commit."""
    lines = [
        "SaberOps acceptance provenance (ADR-0102). Written by AcceptEngine; "
        "the commit object is never rewritten.",
        f"{KEY_RUN}: {_trailer_value(run_id)}",
    ]
    if job_id:
        lines.append(f"{KEY_JOB}: {_trailer_value(job_id)}")
    lines.extend(
        [
            f"{KEY_ATTEMPT}: {_trailer_value(attempt_id)}",
            f"{KEY_BASE}: {_trailer_value(base_sha)}",
            f"{KEY_CANDIDATE}: {_trailer_value(candidate_sha)}",
            f"{KEY_WORKER}: {_trailer_value(provider)}",
            f"{KEY_MODEL}: {_trailer_value(model)}",
            f"{KEY_FINAL_BRANCH}: {_trailer_value(final_branch)}",
            f"{KEY_ACCEPTED_AT}: {_trailer_value(accepted_at)}",
            f"{KEY_GATE}: {_trailer_value(gate)}",
            f"{KEY_GATE_RECEIPT}: {_trailer_value(gate_receipt_digest or 'none')}",
            f"{KEY_REVIEW}: {_trailer_value(review_id or 'none')}",
            f"{KEY_REVIEW_VERDICT}: {_trailer_value(review_verdict or 'none')}",
            f"{KEY_REVIEW_FINDINGS}: {finding_count}",
            f"{KEY_TRANSCRIPT}: {_trailer_value(transcript_path or 'none')}",
            f"{KEY_TRANSCRIPT_STATE}: "
            f"{TRANSCRIPT_PRESENT if transcript_present else TRANSCRIPT_ABSENT}",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_provenance(text: str) -> dict[str, str]:
    """Return the ``SaberOps-*`` key/value lines of a commit message or note."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        key = key.strip()
        if separator and key in PROVENANCE_KEYS:
            found[key] = value.strip()
    return found


@dataclass(frozen=True)
class CommitProvenanceRecord:
    """One commit resolved through its Git-visible provenance to stored records."""

    repo: str
    commit: str
    trailers: dict[str, str]
    note: dict[str, str] | None
    note_ref: str
    #: True when the stored attempt row's commit is exactly this commit; False
    #: when a trailer/note names a record that does not bind to it (a
    #: worker-written commit message can claim anything); None when no attempt
    #: was resolvable.
    verified: bool | None
    run: dict[str, Any] | None
    attempt: dict[str, Any] | None
    job: dict[str, Any] | None
    review: dict[str, Any] | None
    transcript: dict[str, Any]
    #: Set when the owning store could not be opened (a clone on another
    #: machine); the Git-side facts above are still reported.
    store_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Serialize for the CLI's JSON output."""
        return {
            "repo": self.repo,
            "commit": self.commit,
            "trailers": dict(self.trailers),
            "note": dict(self.note) if self.note is not None else None,
            "note_ref": self.note_ref,
            "verified": self.verified,
            "run": self.run,
            "attempt": self.attempt,
            "job": self.job,
            "review": self.review,
            "transcript": self.transcript,
            "store_error": self.store_error,
        }


def resolve_commit_provenance(
    repo_path: Path | str,
    rev: str,
    *,
    db: Database | None = None,
) -> CommitProvenanceRecord:
    """Resolve one commit (or ``path:line`` blame target) to its stored records.

    Only the Git side is required: the trailer block names the run and attempt,
    so a clone that never fetched the notes ref still resolves.  The note adds
    the gate/review facts when it is present.  Stored rows are never deleted, so
    a run and attempt stay resolvable after their transcript is gone; the
    transcript state is read live instead of trusted from the note.
    """
    repo = Path(repo_path).resolve()
    if not GitManager.is_git_repo(repo):
        raise ProvenanceError(f"'{repo}' is not a Git repository")
    commit = _resolve_revision(repo, rev)
    message = GitManager.commit_message(repo, commit)
    trailers = parse_provenance(message)
    note_text = GitManager.show_note(repo, commit, ref=NOTES_REF)
    note = parse_provenance(note_text) if note_text else None
    if not trailers and not note:
        raise ProvenanceError(
            f"commit {commit} has no SaberOps provenance (no trailer and no "
            f"{NOTES_REF} note); commits made before lane H44 have none"
        )

    merged = {**trailers, **(note or {})}
    run_id = merged.get(KEY_RUN)
    attempt_id = merged.get(KEY_ATTEMPT)
    job_id = merged.get(KEY_JOB)
    run: Run | None = None
    attempt: Attempt | None = None
    job: Any = None
    review: Any = None
    store_error: str | None = None
    transcript: dict[str, Any] = {
        "path": merged.get(KEY_TRANSCRIPT),
        "present": False,
        "state": TRANSCRIPT_ABSENT,
        "message": None,
    }
    if run_id:
        try:
            run_db = db if db is not None else _open_run_database(run_id)
        except ProvenanceError as exc:
            run_db = None
            store_error = str(exc)
        if run_db is not None:
            run = run_db.get_run(run_id)
            if run is None:
                store_error = (
                    f"run '{run_id}' was not found in the store at {run_db.db_path}"
                )
            if attempt_id:
                attempt = run_db.get_attempt(attempt_id)
            review, transcript = _read_acceptance_evidence(
                run_db, run_id, merged, transcript
            )
            if not job_id and run is not None:
                job_id = run.job_id
            if job_id:
                from saberops.jobs.store import get_job

                job = get_job(run_db, job_id)
            if job is None:
                from saberops.jobs.store import find_job_for_run

                job = find_job_for_run(run_db, run_id)
    return CommitProvenanceRecord(
        repo=str(repo),
        commit=commit,
        trailers=trailers,
        note=note,
        note_ref=NOTES_REF,
        verified=(
            None if attempt is None or run is None else attempt.commit_sha == commit
        ),
        run=_run_view(run),
        attempt=_attempt_view(attempt),
        job=_job_view(job),
        review=_review_view(review, merged),
        transcript=transcript,
        store_error=store_error,
    )


def _resolve_revision(repo: Path, rev: str) -> str:
    """Resolve ``rev`` to a commit SHA, accepting a ``path:line`` blame target."""
    text = rev.strip()
    head, separator, tail = text.rpartition(":")
    if separator and head and tail.isdigit():
        return GitManager.blame_commit(repo, head, int(tail))
    return text


def _open_run_database(run_id: str) -> Database | None:
    """Open the project store that owns ``run_id`` (legacy IDs use the global store)."""
    from saberops.db import get_default_db_path
    from saberops.runtime import db_path_for_run

    try:
        resolved = db_path_for_run(run_id)
    except Exception as exc:  # noqa: BLE001 - reported as one provenance failure
        raise ProvenanceError(
            f"run '{run_id}' has no readable runtime state: {exc}"
        ) from exc
    owner = get_default_db_path()
    if resolved is None:
        return Database(owner)
    return Database(resolved, owner_db=Database(owner))


def _read_acceptance_evidence(
    db: Database,
    run_id: str,
    merged: dict[str, str],
    transcript: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """Return the note-named review row and the live transcript state."""
    review: Any = None
    review_id = merged.get(KEY_REVIEW)
    if review_id and review_id != "none":
        review = next(
            (item for item in db.get_reviews_for_run(run_id) if item.id == review_id),
            None,
        )
    supervision = db.get_supervision(run_id)
    recorded = supervision.transcript_path if supervision is not None else None
    path = recorded or merged.get(KEY_TRANSCRIPT)
    if path in (None, "", "none"):
        path = None
    if path is None:
        transcript.update(
            {"path": None, "present": False, "state": TRANSCRIPT_ABSENT,
             "message": "no transcript was recorded for this run"}
        )
        return review, transcript
    present = Path(path).is_file()
    transcript.update(
        {
            "path": path,
            "present": present,
            "state": TRANSCRIPT_PRESENT if present else TRANSCRIPT_REMOVED,
            "message": None if present else _TRANSCRIPT_REMOVED_MESSAGE,
        }
    )
    return review, transcript


def _run_view(run: Run | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "run_id": run.id,
        "status": run.status.value,
        "task": run.task,
        "target_repo": run.target_repo,
        "base_commit": run.base_commit,
        "job_id": run.job_id,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }


def _attempt_view(attempt: Attempt | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    return {
        "attempt_id": attempt.id,
        "run_id": attempt.run_id,
        "attempt_number": attempt.attempt_number,
        "status": attempt.status.value,
        "provider": attempt.provider,
        "model": attempt.model,
        "base_sha": attempt.base_sha,
        "commit_sha": attempt.commit_sha,
        "created_at": attempt.created_at,
    }


def _job_view(job: Any) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "job_id": job.job_id,
        "state": job.state.value,
        "goal": job.goal,
        "repo": job.repo,
        "current_run_id": job.current_run_id,
        "run_ids": list(job.run_ids),
        "created_at": job.created_at,
    }


def _review_view(review: Any, merged: dict[str, str]) -> dict[str, Any] | None:
    review_id = merged.get(KEY_REVIEW)
    verdict = merged.get(KEY_REVIEW_VERDICT)
    findings = merged.get(KEY_REVIEW_FINDINGS)
    if review_id in (None, "", "none"):
        review_id = review.id if review is not None else None
    if verdict in (None, "", "none"):
        verdict = review.verdict.value if review is not None else None
    if review_id is None and verdict is None and findings is None:
        return None
    return {
        "review_id": review_id,
        "verdict": verdict,
        "findings": findings,
        "summary": review.summary if review is not None else None,
    }
