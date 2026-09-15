"""Genesis and takeover: how a Project Ledger comes into existence.

Both paths produce exactly the same durable artefacts -- one identity, durable
facts, a first checkpoint and a bootstrap -- through the same canonical
schemas.  There is no special first-time representation to migrate away from
later, because there is no special first-time representation at all.

The two paths differ in one respect only, and it is the important one:

*Genesis* starts from a repository nobody has claimed yet, so everything
recorded is deterministically observed.

*Takeover* starts from a repository with a history and no ledger.  What the
repository states is observed and accepted; what anyone believes it is *for*
enters as a ``PROVISIONAL`` fact and stays provisional until someone accepts it
by name.  An assumption never becomes a requirement by being written down.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from orchestrator_mvp.db import Database, ProjectLedgerEventRow, current_iso_timestamp
from orchestrator_mvp.project_ledger.checkpoint import create_checkpoint
from orchestrator_mvp.project_ledger.contracts import (
    LEDGER_SCHEMA_VERSION,
    FactCategory,
    LedgerEventType,
    ProjectCheckpoint,
    ProjectFact,
    ProjectIdentityRecord,
    ProjectLedgerCommandError,
    ProjectOrigin,
    ProjectState,
    Provenance,
    RecordOrigin,
    canonical_json,
)
from orchestrator_mvp.project_ledger.observation import (
    DEFAULT_MARKER_PATHS,
    RepositoryObservation,
    observe_repository,
)
from orchestrator_mvp.project_ledger.store import ProjectLedger, load_project

#: Provenance for anything a machine read rather than anybody concluded.
OBSERVED = Provenance(origin=RecordOrigin.DETERMINISTIC_OBSERVATION, actor="devscope-observer")


@dataclass(frozen=True)
class InferredAssumption:
    """One belief about a taken-over repository that nothing proves.

    A presumed product goal, roadmap or rationale arrives here.  It is durable
    and attributable, and it is explicitly not Project truth.
    """

    statement: str
    category: FactCategory = FactCategory.ASSUMPTION
    reference: str = ""


@dataclass(frozen=True)
class ProjectBootstrapResult:
    """What genesis or takeover produced."""

    ledger: ProjectLedger
    checkpoint: ProjectCheckpoint
    observation: RepositoryObservation
    observed_facts: tuple[ProjectFact, ...] = ()
    provisional_facts: tuple[ProjectFact, ...] = ()

    @property
    def project_id(self) -> str:
        """Identity of the project that now has a ledger."""
        return self.ledger.project_id


def _create_ledger_row(
    db: Database,
    observation: RepositoryObservation,
    origin: ProjectOrigin,
    created_at: str,
) -> bool:
    """Write the identity row plus its creation event, atomically."""
    identity = ProjectIdentityRecord(
        project_id=observation.identity.project_id,
        canonical_path=observation.identity.canonical_path,
        remote_identity=observation.identity.remote_identity,
        root_commit=observation.identity.root_commit,
        origin=origin,
        genesis=observation.to_dict(),
        project_version=1,
        schema_version=LEDGER_SCHEMA_VERSION,
        created_at=created_at,
        updated_at=created_at,
    )
    return db.create_project_ledger(
        project_id=identity.project_id,
        canonical_path=identity.canonical_path,
        remote_identity=identity.remote_identity,
        root_commit=identity.root_commit,
        origin=origin.value,
        genesis_json=canonical_json(identity.genesis),
        project_version=identity.project_version,
        schema_version=identity.schema_version,
        created_at=created_at,
        event=ProjectLedgerEventRow(
            project_id=identity.project_id,
            event_type=LedgerEventType.PROJECT_CREATED.value,
            subject_kind="project",
            subject_id=identity.project_id,
            project_version=1,
            state_digest=ProjectState(identity=identity).digest,
            payload_json=canonical_json({"origin": origin.value}),
        ),
    )


def _record_observations(
    ledger: ProjectLedger, observation: RepositoryObservation
) -> tuple[ProjectFact, ...]:
    """Record the deterministic observations as accepted Project facts."""
    existing = {(fact.category, fact.statement) for fact in ledger.state.facts}
    recorded: list[ProjectFact] = []
    for statement, reference in observation.fact_statements():
        if (FactCategory.OBSERVATION, statement) in existing:
            continue
        recorded.append(
            ledger.record_fact(
                category=FactCategory.OBSERVATION,
                statement=statement,
                reference=reference,
                provenance=OBSERVED,
            )
        )
    return tuple(recorded)


def _open_or_create(
    db: Database,
    repo_path: Path | str,
    origin: ProjectOrigin,
    marker_paths: tuple[str, ...],
) -> tuple[ProjectLedger, RepositoryObservation, tuple[ProjectFact, ...], bool]:
    now = current_iso_timestamp()
    observation = observe_repository(repo_path, observed_at=now, marker_paths=marker_paths)
    created = _create_ledger_row(db, observation, origin, now)
    ledger = load_project(db, observation.identity.project_id)
    facts = _record_observations(ledger, observation) if created else ()
    return ledger, observation, facts, created


def create_project(
    db: Database,
    repo_path: Path | str,
    *,
    marker_paths: tuple[str, ...] = DEFAULT_MARKER_PATHS,
) -> ProjectBootstrapResult:
    """Create a Project Ledger for a repository that has none (idempotent).

    Re-running genesis on an existing project is a no-op that returns the
    existing ledger: initialization never overwrites accepted state, and never
    mints a second identity for the same repository.
    """
    ledger, observation, facts, _created = _open_or_create(
        db, repo_path, ProjectOrigin.GENESIS, marker_paths
    )
    return ProjectBootstrapResult(
        ledger=ledger,
        checkpoint=create_checkpoint(ledger),
        observation=observation,
        observed_facts=facts,
    )


def takeover_project(
    db: Database,
    repo_path: Path | str,
    *,
    assumptions: Sequence[InferredAssumption] = (),
    assumed_by: str = "orch",
    marker_paths: tuple[str, ...] = DEFAULT_MARKER_PATHS,
) -> ProjectBootstrapResult:
    """Take over an existing repository that has no Project Ledger yet.

    Deterministic observations become accepted facts.  Every entry in
    ``assumptions`` becomes a ``PROVISIONAL`` fact attributed to
    ``RecordOrigin.INFERENCE``: visible, durable, reviewable, and excluded from
    every authoritative reference until somebody accepts it explicitly.
    """
    ledger, observation, facts, created = _open_or_create(
        db, repo_path, ProjectOrigin.TAKEOVER, marker_paths
    )
    provisional: list[ProjectFact] = []
    if created:
        inferred = Provenance(
            origin=RecordOrigin.INFERENCE,
            actor=assumed_by,
            reference="takeover",
        )
        for assumption in assumptions:
            if assumption.category is FactCategory.OBSERVATION:
                raise ProjectLedgerCommandError(
                    "an inferred assumption may not be recorded as a deterministic "
                    "observation"
                )
            provisional.append(
                ledger.record_fact(
                    category=assumption.category,
                    statement=assumption.statement,
                    reference=assumption.reference,
                    provenance=inferred,
                    provisional=True,
                )
            )
    return ProjectBootstrapResult(
        ledger=ledger,
        checkpoint=create_checkpoint(ledger),
        observation=observation,
        observed_facts=facts,
        provisional_facts=tuple(provisional),
    )


__all__ = [
    "OBSERVED",
    "InferredAssumption",
    "ProjectBootstrapResult",
    "create_project",
    "takeover_project",
]
