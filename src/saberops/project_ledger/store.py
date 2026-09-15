"""The Project Ledger store: typed commands over durable Project truth.

Every authoritative change to a Project goes through one of the typed commands
on :class:`ProjectLedger`.  There is deliberately no generic "update this
record with this dictionary" entry point: an invariant that only holds when the
caller remembers to hold it is not an invariant, so supersession, acceptance,
retirement and disposition each exist as their own command with their own
guard.

Each command performs exactly one authoritative mutation:

1. it builds the post-mutation :class:`ProjectState` in memory;
2. it takes that state's canonical digest;
3. it persists the affected rows, the new project version and one append-only
   event carrying that digest, atomically;
4. it re-reads durable state and fails closed unless the reconstruction
   reproduces the digest it just recorded.

Step 4 is what makes "the ledger reconstructs" an executed property rather than
a claim: a row that cannot be read back into the same canonical state is an
integrity failure at the moment it is written, not a surprise a year later.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any

from saberops.db import (
    Database,
    ProjectLedgerEventRow,
    ProjectLedgerRow,
    current_iso_timestamp,
)
from saberops.project_ledger.contracts import (
    LEDGER_SCHEMA_VERSION,
    ChangeIntake,
    FactCategory,
    IntakeDisposition,
    LedgerEvent,
    LedgerEventType,
    OpenQuestion,
    ProjectDecision,
    ProjectFact,
    ProjectIdentityRecord,
    ProjectLedgerCommandError,
    ProjectLedgerConflictError,
    ProjectLedgerIntegrityError,
    ProjectLedgerNotFoundError,
    ProjectOrigin,
    ProjectState,
    Provenance,
    QuestionStatus,
    RecordOrigin,
    RecordStatus,
    RoadmapItem,
    RoadmapStatus,
    UnsupportedSchemaVersionError,
    digest_of,
)

_MUTABLE_FACT_STATUSES = frozenset({RecordStatus.ACCEPTED, RecordStatus.PROVISIONAL})


def _record_id(prefix: str, project_id: str, kind: str, material: str, version: int) -> str:
    """Mint a stable, content-derived record identity.

    Identities are derived rather than random so the same authoritative
    mutation always produces the same identity, and never collide because the
    project version is part of the material and advances with every mutation.
    """
    return f"{prefix}_{digest_of([project_id, kind, material, version])[:16]}"


def _json_list(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise ProjectLedgerIntegrityError(f"list column is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ProjectLedgerIntegrityError("list column must hold a JSON array")
    return tuple(str(item) for item in payload)


def _text(row: dict[str, Any], column: str) -> str:
    value = row.get(column)
    return "" if value is None else str(value)


def _optional_text(row: dict[str, Any], column: str) -> str | None:
    value = row.get(column)
    return None if value is None else str(value)


def _optional_int(row: dict[str, Any], column: str) -> int | None:
    value = row.get(column)
    return None if value is None else int(value)


def _row_to_fact(row: dict[str, Any]) -> ProjectFact:
    return ProjectFact(
        id=_text(row, "id"),
        project_id=_text(row, "project_id"),
        category=FactCategory(_text(row, "category")),
        statement=_text(row, "statement"),
        reference=_text(row, "reference"),
        origin=RecordOrigin(_text(row, "origin")),
        status=RecordStatus(_text(row, "status")),
        provenance=Provenance.from_json(_text(row, "provenance_json")),
        introduced_version=int(row["introduced_version"]),
        introduced_at=_text(row, "introduced_at"),
        supersedes=_optional_text(row, "supersedes"),
        superseded_by=_optional_text(row, "superseded_by"),
        retired_version=_optional_int(row, "retired_version"),
        retired_at=_optional_text(row, "retired_at"),
    )


def _fact_values(fact: ProjectFact) -> dict[str, str | int | None]:
    return {
        "id": fact.id,
        "project_id": fact.project_id,
        "category": fact.category.value,
        "statement": fact.statement,
        "reference": fact.reference,
        "origin": fact.origin.value,
        "status": fact.status.value,
        "provenance_json": json.dumps(fact.provenance.to_dict(), sort_keys=True),
        "supersedes": fact.supersedes,
        "superseded_by": fact.superseded_by,
        "introduced_version": fact.introduced_version,
        "introduced_at": fact.introduced_at,
        "retired_version": fact.retired_version,
        "retired_at": fact.retired_at,
    }


def _row_to_decision(row: dict[str, Any]) -> ProjectDecision:
    return ProjectDecision(
        id=_text(row, "id"),
        project_id=_text(row, "project_id"),
        statement=_text(row, "statement"),
        rationale=_text(row, "rationale"),
        reference=_text(row, "reference"),
        origin=RecordOrigin(_text(row, "origin")),
        status=RecordStatus(_text(row, "status")),
        provenance=Provenance.from_json(_text(row, "provenance_json")),
        introduced_version=int(row["introduced_version"]),
        introduced_at=_text(row, "introduced_at"),
        supersedes=_optional_text(row, "supersedes"),
        superseded_by=_optional_text(row, "superseded_by"),
        retired_version=_optional_int(row, "retired_version"),
        retired_at=_optional_text(row, "retired_at"),
    )


def _decision_values(decision: ProjectDecision) -> dict[str, str | int | None]:
    return {
        "id": decision.id,
        "project_id": decision.project_id,
        "statement": decision.statement,
        "rationale": decision.rationale,
        "reference": decision.reference,
        "origin": decision.origin.value,
        "status": decision.status.value,
        "provenance_json": json.dumps(decision.provenance.to_dict(), sort_keys=True),
        "supersedes": decision.supersedes,
        "superseded_by": decision.superseded_by,
        "introduced_version": decision.introduced_version,
        "introduced_at": decision.introduced_at,
        "retired_version": decision.retired_version,
        "retired_at": decision.retired_at,
    }


def _row_to_roadmap_item(row: dict[str, Any]) -> RoadmapItem:
    return RoadmapItem(
        id=_text(row, "id"),
        project_id=_text(row, "project_id"),
        title=_text(row, "title"),
        status=RoadmapStatus(_text(row, "status")),
        ordering=int(row["ordering"]),
        depends_on=_json_list(row.get("depends_on_json")),
        acceptance_ref=_text(row, "acceptance_ref"),
        result_ref=_text(row, "result_ref"),
        run_ref=_optional_text(row, "run_ref"),
        origin=RecordOrigin(_text(row, "origin")),
        provenance=Provenance.from_json(_text(row, "provenance_json")),
        revision=int(row["revision"]),
        introduced_version=int(row["introduced_version"]),
        introduced_at=_text(row, "introduced_at"),
        updated_version=int(row["updated_version"]),
        updated_at=_text(row, "updated_at"),
    )


def _roadmap_values(item: RoadmapItem) -> dict[str, str | int | None]:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "title": item.title,
        "status": item.status.value,
        "ordering": item.ordering,
        "depends_on_json": json.dumps(sorted(item.depends_on)),
        "acceptance_ref": item.acceptance_ref,
        "result_ref": item.result_ref,
        "run_ref": item.run_ref,
        "origin": item.origin.value,
        "provenance_json": json.dumps(item.provenance.to_dict(), sort_keys=True),
        "revision": item.revision,
        "introduced_version": item.introduced_version,
        "introduced_at": item.introduced_at,
        "updated_version": item.updated_version,
        "updated_at": item.updated_at,
    }


def _row_to_open_question(row: dict[str, Any]) -> OpenQuestion:
    return OpenQuestion(
        id=_text(row, "id"),
        project_id=_text(row, "project_id"),
        question=_text(row, "question"),
        status=QuestionStatus(_text(row, "status")),
        resolution=_text(row, "resolution"),
        resolution_ref=_text(row, "resolution_ref"),
        origin=RecordOrigin(_text(row, "origin")),
        provenance=Provenance.from_json(_text(row, "provenance_json")),
        introduced_version=int(row["introduced_version"]),
        introduced_at=_text(row, "introduced_at"),
        resolved_version=_optional_int(row, "resolved_version"),
        resolved_at=_optional_text(row, "resolved_at"),
    )


def _open_question_values(question: OpenQuestion) -> dict[str, str | int | None]:
    return {
        "id": question.id,
        "project_id": question.project_id,
        "question": question.question,
        "status": question.status.value,
        "resolution": question.resolution,
        "resolution_ref": question.resolution_ref,
        "origin": question.origin.value,
        "provenance_json": json.dumps(question.provenance.to_dict(), sort_keys=True),
        "introduced_version": question.introduced_version,
        "introduced_at": question.introduced_at,
        "resolved_version": question.resolved_version,
        "resolved_at": question.resolved_at,
    }


def _row_to_change_intake(row: dict[str, Any]) -> ChangeIntake:
    return ChangeIntake(
        id=_text(row, "id"),
        project_id=_text(row, "project_id"),
        summary=_text(row, "summary"),
        detail=_text(row, "detail"),
        origin=RecordOrigin(_text(row, "origin")),
        disposition=IntakeDisposition(_text(row, "disposition")),
        provenance=Provenance.from_json(_text(row, "provenance_json")),
        recorded_version=int(row["recorded_version"]),
        recorded_at=_text(row, "recorded_at"),
        linked_fact_id=_optional_text(row, "linked_fact_id"),
        linked_decision_id=_optional_text(row, "linked_decision_id"),
        linked_roadmap_id=_optional_text(row, "linked_roadmap_id"),
        decided_version=_optional_int(row, "decided_version"),
        decided_at=_optional_text(row, "decided_at"),
    )


def _intake_values(intake: ChangeIntake) -> dict[str, str | int | None]:
    return {
        "id": intake.id,
        "project_id": intake.project_id,
        "summary": intake.summary,
        "detail": intake.detail,
        "origin": intake.origin.value,
        "disposition": intake.disposition.value,
        "provenance_json": json.dumps(intake.provenance.to_dict(), sort_keys=True),
        "linked_fact_id": intake.linked_fact_id,
        "linked_decision_id": intake.linked_decision_id,
        "linked_roadmap_id": intake.linked_roadmap_id,
        "recorded_version": intake.recorded_version,
        "recorded_at": intake.recorded_at,
        "decided_version": intake.decided_version,
        "decided_at": intake.decided_at,
    }


def _identity_from_row(row: dict[str, Any]) -> ProjectIdentityRecord:
    schema_version = int(row["schema_version"])
    if schema_version > LEDGER_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"project '{row['project_id']}' declares ledger schema version "
            f"{schema_version}; this build understands at most {LEDGER_SCHEMA_VERSION}"
        )
    try:
        genesis = json.loads(_text(row, "genesis_json"))
    except json.JSONDecodeError as exc:
        raise ProjectLedgerIntegrityError(
            f"project '{row['project_id']}': genesis metadata is not valid JSON: {exc}"
        ) from exc
    if not isinstance(genesis, dict):
        raise ProjectLedgerIntegrityError(
            f"project '{row['project_id']}': genesis metadata must be a JSON object"
        )
    return ProjectIdentityRecord(
        project_id=_text(row, "project_id"),
        canonical_path=_text(row, "canonical_path"),
        remote_identity=_text(row, "remote_identity"),
        root_commit=_text(row, "root_commit"),
        origin=ProjectOrigin(_text(row, "origin")),
        genesis=genesis,
        project_version=int(row["project_version"]),
        schema_version=schema_version,
        created_at=_text(row, "created_at"),
        updated_at=_text(row, "updated_at"),
    )


def read_project_state(db: Database, project_id: str) -> ProjectState:
    """Reconstruct the complete authoritative state of one Project.

    Reconstruction reads durable rows only.  It never consults a live process,
    a provider session, a cached bootstrap, or any Orch transcript.
    """
    row = db.get_project_ledger(project_id)
    if row is None:
        raise ProjectLedgerNotFoundError(f"no Project Ledger for project '{project_id}'")
    return ProjectState(
        identity=_identity_from_row(row),
        facts=tuple(
            _row_to_fact(record)
            for record in db.list_project_ledger_rows("project_facts", project_id)
        ),
        decisions=tuple(
            _row_to_decision(record)
            for record in db.list_project_ledger_rows("project_decisions", project_id)
        ),
        roadmap=tuple(
            _row_to_roadmap_item(record)
            for record in db.list_project_ledger_rows("project_roadmap_items", project_id)
        ),
        open_questions=tuple(
            _row_to_open_question(record)
            for record in db.list_project_ledger_rows("project_open_questions", project_id)
        ),
        change_intake=tuple(
            _row_to_change_intake(record)
            for record in db.list_project_ledger_rows("project_change_intake", project_id)
        ),
    )


class ProjectLedger:
    """Typed command surface over one Project's durable authoritative state."""

    def __init__(self, db: Database, state: ProjectState) -> None:
        self._db = db
        self._state = state

    @property
    def db(self) -> Database:
        """The durable store backing this ledger."""
        return self._db

    @property
    def project_id(self) -> str:
        """Stable identity of the project this ledger owns."""
        return self._state.project_id

    @property
    def project_version(self) -> int:
        """Current authoritative project-state version."""
        return self._state.project_version

    @property
    def state(self) -> ProjectState:
        """The complete authoritative state as last reconstructed."""
        return self._state

    @property
    def state_digest(self) -> str:
        """Canonical digest of the current authoritative state."""
        return self._state.digest

    def reload(self) -> ProjectState:
        """Re-read durable state from the store."""
        self._state = read_project_state(self._db, self.project_id)
        return self._state

    def events(self) -> tuple[LedgerEvent, ...]:
        """Every append-only mutation event, oldest first."""
        return tuple(
            LedgerEvent(
                seq=int(row["seq"]),
                project_id=str(row["project_id"]),
                event_type=LedgerEventType(str(row["event_type"])),
                subject_kind=str(row["subject_kind"]),
                subject_id=str(row["subject_id"]),
                project_version=int(row["project_version"]),
                state_digest=str(row["state_digest"]),
                payload=json.loads(str(row["payload_json"])),
                created_at=str(row["created_at"]),
            )
            for row in self._db.list_project_ledger_events(self.project_id)
        )

    # ------------------------------------------------------------------
    # facts
    # ------------------------------------------------------------------

    def record_fact(
        self,
        *,
        category: FactCategory,
        statement: str,
        provenance: Provenance,
        reference: str = "",
        provisional: bool = False,
    ) -> ProjectFact:
        """Record a new Project fact.

        ``provisional=True`` captures something believed but not established --
        a takeover inference, for instance.  A provisional fact is durable and
        attributable but is never Project truth until
        :meth:`accept_fact` promotes it.
        """
        statement = _require_text(statement, "fact statement")
        version = self.project_version + 1
        now = current_iso_timestamp()
        fact = ProjectFact(
            id=_record_id("fact", self.project_id, category.value, statement, version),
            project_id=self.project_id,
            category=category,
            statement=statement,
            reference=reference,
            origin=provenance.origin,
            status=RecordStatus.PROVISIONAL if provisional else RecordStatus.ACCEPTED,
            provenance=provenance,
            introduced_version=version,
            introduced_at=now,
        )
        self._apply(
            version,
            rows=[ProjectLedgerRow("project_facts", "insert", _fact_values(fact))],
            event_type=LedgerEventType.FACT_RECORDED,
            subject_kind="fact",
            subject_id=fact.id,
            payload={"category": category.value, "status": fact.status.value},
            projected=self._with_facts(self._state.facts + (fact,)),
        )
        return self._fact(fact.id)

    def supersede_fact(
        self,
        fact_id: str,
        *,
        statement: str,
        provenance: Provenance,
        reference: str = "",
        category: FactCategory | None = None,
    ) -> ProjectFact:
        """Replace an accepted fact with a new one, preserving both.

        The superseded fact keeps its identity, statement and introduction
        version forever; only its status and back-reference change.  History is
        never edited into agreement with the present.
        """
        previous = self._fact(fact_id)
        if previous.status is not RecordStatus.ACCEPTED:
            raise ProjectLedgerCommandError(
                f"fact '{fact_id}' is {previous.status.value}; only an accepted fact "
                "can be superseded"
            )
        statement = _require_text(statement, "fact statement")
        version = self.project_version + 1
        now = current_iso_timestamp()
        resolved_category = category if category is not None else previous.category
        replacement = ProjectFact(
            id=_record_id("fact", self.project_id, resolved_category.value, statement, version),
            project_id=self.project_id,
            category=resolved_category,
            statement=statement,
            reference=reference,
            origin=provenance.origin,
            status=RecordStatus.ACCEPTED,
            provenance=provenance,
            introduced_version=version,
            introduced_at=now,
            supersedes=previous.id,
        )
        retired = ProjectFact(
            **{
                **previous.__dict__,
                "status": RecordStatus.SUPERSEDED,
                "superseded_by": replacement.id,
                "retired_version": version,
                "retired_at": now,
            }
        )
        self._apply(
            version,
            rows=[
                ProjectLedgerRow("project_facts", "insert", _fact_values(replacement)),
                ProjectLedgerRow(
                    "project_facts",
                    "update",
                    {
                        "id": retired.id,
                        "status": retired.status.value,
                        "superseded_by": retired.superseded_by,
                        "retired_version": retired.retired_version,
                        "retired_at": retired.retired_at,
                    },
                ),
            ],
            event_type=LedgerEventType.FACT_SUPERSEDED,
            subject_kind="fact",
            subject_id=replacement.id,
            payload={"supersedes": previous.id},
            projected=self._with_facts(
                _replace_record(self._state.facts, retired) + (replacement,)
            ),
        )
        return self._fact(replacement.id)

    def accept_fact(self, fact_id: str, *, provenance: Provenance) -> ProjectFact:
        """Promote a provisional fact to Project truth.

        This is the only path from inference to authority, and it is always an
        explicit act.  Nothing promotes an assumption implicitly.
        """
        previous = self._fact(fact_id)
        if previous.status is not RecordStatus.PROVISIONAL:
            raise ProjectLedgerCommandError(
                f"fact '{fact_id}' is {previous.status.value}; only a provisional fact "
                "can be accepted"
            )
        version = self.project_version + 1
        accepted = ProjectFact(
            **{**previous.__dict__, "status": RecordStatus.ACCEPTED, "provenance": provenance}
        )
        self._apply(
            version,
            rows=[
                ProjectLedgerRow(
                    "project_facts",
                    "update",
                    {
                        "id": accepted.id,
                        "status": accepted.status.value,
                        "provenance_json": json.dumps(provenance.to_dict(), sort_keys=True),
                    },
                )
            ],
            event_type=LedgerEventType.FACT_ACCEPTED,
            subject_kind="fact",
            subject_id=accepted.id,
            payload={"previous_status": previous.status.value},
            projected=self._with_facts(_replace_record(self._state.facts, accepted)),
        )
        return self._fact(accepted.id)

    def retire_fact(
        self, fact_id: str, *, provenance: Provenance, rejected: bool = False
    ) -> ProjectFact:
        """Explicitly retire a fact (or reject a provisional one).

        Non-goals in particular survive every continuation unless they are
        retired here, deliberately, by name.
        """
        previous = self._fact(fact_id)
        if previous.status not in _MUTABLE_FACT_STATUSES:
            raise ProjectLedgerCommandError(
                f"fact '{fact_id}' is already {previous.status.value}"
            )
        if rejected and previous.status is not RecordStatus.PROVISIONAL:
            raise ProjectLedgerCommandError(
                f"fact '{fact_id}' is accepted; an accepted fact is retired, not rejected"
            )
        version = self.project_version + 1
        now = current_iso_timestamp()
        status = RecordStatus.REJECTED if rejected else RecordStatus.RETIRED
        retired = ProjectFact(
            **{
                **previous.__dict__,
                "status": status,
                "retired_version": version,
                "retired_at": now,
                "provenance": provenance,
            }
        )
        self._apply(
            version,
            rows=[
                ProjectLedgerRow(
                    "project_facts",
                    "update",
                    {
                        "id": retired.id,
                        "status": status.value,
                        "retired_version": version,
                        "retired_at": now,
                        "provenance_json": json.dumps(provenance.to_dict(), sort_keys=True),
                    },
                )
            ],
            event_type=LedgerEventType.FACT_RETIRED,
            subject_kind="fact",
            subject_id=retired.id,
            payload={"status": status.value},
            projected=self._with_facts(_replace_record(self._state.facts, retired)),
        )
        return self._fact(retired.id)

    # ------------------------------------------------------------------
    # decisions
    # ------------------------------------------------------------------

    def record_decision(
        self,
        *,
        statement: str,
        rationale: str,
        provenance: Provenance,
        reference: str = "",
        provisional: bool = False,
    ) -> ProjectDecision:
        """Record a Project decision together with why it was made."""
        statement = _require_text(statement, "decision statement")
        version = self.project_version + 1
        decision = ProjectDecision(
            id=_record_id("dec", self.project_id, "decision", statement, version),
            project_id=self.project_id,
            statement=statement,
            rationale=rationale,
            reference=reference,
            origin=provenance.origin,
            status=RecordStatus.PROVISIONAL if provisional else RecordStatus.ACCEPTED,
            provenance=provenance,
            introduced_version=version,
            introduced_at=current_iso_timestamp(),
        )
        self._apply(
            version,
            rows=[ProjectLedgerRow("project_decisions", "insert", _decision_values(decision))],
            event_type=LedgerEventType.DECISION_RECORDED,
            subject_kind="decision",
            subject_id=decision.id,
            payload={"status": decision.status.value},
            projected=self._with_decisions(self._state.decisions + (decision,)),
        )
        return self._decision(decision.id)

    def supersede_decision(
        self,
        decision_id: str,
        *,
        statement: str,
        rationale: str,
        provenance: Provenance,
        reference: str = "",
    ) -> ProjectDecision:
        """Replace an accepted decision, keeping the original in history."""
        previous = self._decision(decision_id)
        if previous.status is not RecordStatus.ACCEPTED:
            raise ProjectLedgerCommandError(
                f"decision '{decision_id}' is {previous.status.value}; only an accepted "
                "decision can be superseded"
            )
        statement = _require_text(statement, "decision statement")
        version = self.project_version + 1
        now = current_iso_timestamp()
        replacement = ProjectDecision(
            id=_record_id("dec", self.project_id, "decision", statement, version),
            project_id=self.project_id,
            statement=statement,
            rationale=rationale,
            reference=reference,
            origin=provenance.origin,
            status=RecordStatus.ACCEPTED,
            provenance=provenance,
            introduced_version=version,
            introduced_at=now,
            supersedes=previous.id,
        )
        retired = ProjectDecision(
            **{
                **previous.__dict__,
                "status": RecordStatus.SUPERSEDED,
                "superseded_by": replacement.id,
                "retired_version": version,
                "retired_at": now,
            }
        )
        self._apply(
            version,
            rows=[
                ProjectLedgerRow("project_decisions", "insert", _decision_values(replacement)),
                ProjectLedgerRow(
                    "project_decisions",
                    "update",
                    {
                        "id": retired.id,
                        "status": retired.status.value,
                        "superseded_by": retired.superseded_by,
                        "retired_version": retired.retired_version,
                        "retired_at": retired.retired_at,
                    },
                ),
            ],
            event_type=LedgerEventType.DECISION_SUPERSEDED,
            subject_kind="decision",
            subject_id=replacement.id,
            payload={"supersedes": previous.id},
            projected=self._with_decisions(
                _replace_record(self._state.decisions, retired) + (replacement,)
            ),
        )
        return self._decision(replacement.id)

    # ------------------------------------------------------------------
    # roadmap
    # ------------------------------------------------------------------

    def add_roadmap_item(
        self,
        *,
        title: str,
        provenance: Provenance,
        ordering: int | None = None,
        depends_on: Iterable[str] = (),
        acceptance_ref: str = "",
    ) -> RoadmapItem:
        """Add a planned roadmap item."""
        title = _require_text(title, "roadmap title")
        version = self.project_version + 1
        now = current_iso_timestamp()
        resolved_ordering = (
            ordering
            if ordering is not None
            else (max((item.ordering for item in self._state.roadmap), default=0) + 10)
        )
        item = RoadmapItem(
            id=_record_id("road", self.project_id, "roadmap", title, version),
            project_id=self.project_id,
            title=title,
            status=RoadmapStatus.PLANNED,
            ordering=resolved_ordering,
            depends_on=tuple(sorted(str(dep) for dep in depends_on)),
            acceptance_ref=acceptance_ref,
            result_ref="",
            run_ref=None,
            origin=provenance.origin,
            provenance=provenance,
            revision=1,
            introduced_version=version,
            introduced_at=now,
            updated_version=version,
            updated_at=now,
        )
        self._apply(
            version,
            rows=[ProjectLedgerRow("project_roadmap_items", "insert", _roadmap_values(item))],
            event_type=LedgerEventType.ROADMAP_ITEM_ADDED,
            subject_kind="roadmap_item",
            subject_id=item.id,
            payload={"status": item.status.value, "ordering": item.ordering},
            projected=self._with_roadmap(self._state.roadmap + (item,)),
        )
        return self._roadmap_item(item.id)

    def update_roadmap_item(
        self,
        item_id: str,
        *,
        provenance: Provenance,
        status: RoadmapStatus | None = None,
        ordering: int | None = None,
        depends_on: Iterable[str] | None = None,
        acceptance_ref: str | None = None,
        result_ref: str | None = None,
        run_ref: str | None = None,
    ) -> RoadmapItem:
        """Advance a roadmap item, producing a durable versioned change.

        Completion is guarded: an item may only become ``COMPLETED`` when it
        carries a result reference.  Interruption therefore cannot drift into
        completion, because nothing can mark work done without naming what was
        actually produced.
        """
        previous = self._roadmap_item(item_id)
        resolved_result = previous.result_ref if result_ref is None else result_ref
        resolved_status = previous.status if status is None else status
        if resolved_status is RoadmapStatus.COMPLETED and not resolved_result.strip():
            raise ProjectLedgerCommandError(
                f"roadmap item '{item_id}' cannot be completed without a result reference"
            )
        version = self.project_version + 1
        now = current_iso_timestamp()
        updated = RoadmapItem(
            **{
                **previous.__dict__,
                "status": resolved_status,
                "ordering": previous.ordering if ordering is None else ordering,
                "depends_on": (
                    previous.depends_on
                    if depends_on is None
                    else tuple(sorted(str(dep) for dep in depends_on))
                ),
                "acceptance_ref": (
                    previous.acceptance_ref if acceptance_ref is None else acceptance_ref
                ),
                "result_ref": resolved_result,
                "run_ref": previous.run_ref if run_ref is None else run_ref,
                "provenance": provenance,
                "revision": previous.revision + 1,
                "updated_version": version,
                "updated_at": now,
            }
        )
        self._apply(
            version,
            rows=[
                ProjectLedgerRow(
                    "project_roadmap_items",
                    "update",
                    {
                        key: value
                        for key, value in _roadmap_values(updated).items()
                        if key
                        not in ("project_id", "introduced_version", "introduced_at", "title")
                    },
                )
            ],
            event_type=LedgerEventType.ROADMAP_ITEM_UPDATED,
            subject_kind="roadmap_item",
            subject_id=updated.id,
            payload={
                "from_status": previous.status.value,
                "to_status": updated.status.value,
                "revision": updated.revision,
            },
            projected=self._with_roadmap(_replace_record(self._state.roadmap, updated)),
        )
        return self._roadmap_item(updated.id)

    # ------------------------------------------------------------------
    # open questions
    # ------------------------------------------------------------------

    def record_open_question(self, *, question: str, provenance: Provenance) -> OpenQuestion:
        """Record a question the Project has not answered yet."""
        question = _require_text(question, "open question")
        version = self.project_version + 1
        record = OpenQuestion(
            id=_record_id("q", self.project_id, "question", question, version),
            project_id=self.project_id,
            question=question,
            status=QuestionStatus.OPEN,
            resolution="",
            resolution_ref="",
            origin=provenance.origin,
            provenance=provenance,
            introduced_version=version,
            introduced_at=current_iso_timestamp(),
        )
        self._apply(
            version,
            rows=[
                ProjectLedgerRow(
                    "project_open_questions", "insert", _open_question_values(record)
                )
            ],
            event_type=LedgerEventType.OPEN_QUESTION_RECORDED,
            subject_kind="open_question",
            subject_id=record.id,
            payload={"status": record.status.value},
            projected=self._with_questions(self._state.open_questions + (record,)),
        )
        return self._open_question(record.id)

    def resolve_open_question(
        self,
        question_id: str,
        *,
        resolution: str,
        provenance: Provenance,
        resolution_ref: str = "",
        withdrawn: bool = False,
    ) -> OpenQuestion:
        """Answer or withdraw an open question, keeping the question itself."""
        previous = self._open_question(question_id)
        if previous.status is not QuestionStatus.OPEN:
            raise ProjectLedgerCommandError(
                f"open question '{question_id}' is already {previous.status.value}"
            )
        if not withdrawn:
            resolution = _require_text(resolution, "resolution")
        version = self.project_version + 1
        now = current_iso_timestamp()
        status = QuestionStatus.WITHDRAWN if withdrawn else QuestionStatus.RESOLVED
        resolved = OpenQuestion(
            **{
                **previous.__dict__,
                "status": status,
                "resolution": resolution,
                "resolution_ref": resolution_ref,
                "provenance": provenance,
                "resolved_version": version,
                "resolved_at": now,
            }
        )
        self._apply(
            version,
            rows=[
                ProjectLedgerRow(
                    "project_open_questions",
                    "update",
                    {
                        "id": resolved.id,
                        "status": status.value,
                        "resolution": resolved.resolution,
                        "resolution_ref": resolved.resolution_ref,
                        "provenance_json": json.dumps(provenance.to_dict(), sort_keys=True),
                        "resolved_version": version,
                        "resolved_at": now,
                    },
                )
            ],
            event_type=LedgerEventType.OPEN_QUESTION_RESOLVED,
            subject_kind="open_question",
            subject_id=resolved.id,
            payload={"status": status.value},
            projected=self._with_questions(_replace_record(self._state.open_questions, resolved)),
        )
        return self._open_question(resolved.id)

    # ------------------------------------------------------------------
    # change intake
    # ------------------------------------------------------------------

    def record_change_intake(
        self, *, summary: str, provenance: Provenance, detail: str = ""
    ) -> ChangeIntake:
        """Capture a new idea, requirement or correction as *pending*.

        Capture is never acceptance.  An intake record is durable and visible
        immediately, but it carries no authority until
        :meth:`dispose_change_intake` says what became of it.
        """
        summary = _require_text(summary, "change intake summary")
        version = self.project_version + 1
        record = ChangeIntake(
            id=_record_id("intake", self.project_id, "intake", summary, version),
            project_id=self.project_id,
            summary=summary,
            detail=detail,
            origin=provenance.origin,
            disposition=IntakeDisposition.PENDING,
            provenance=provenance,
            recorded_version=version,
            recorded_at=current_iso_timestamp(),
        )
        self._apply(
            version,
            rows=[ProjectLedgerRow("project_change_intake", "insert", _intake_values(record))],
            event_type=LedgerEventType.CHANGE_INTAKE_RECORDED,
            subject_kind="change_intake",
            subject_id=record.id,
            payload={"disposition": record.disposition.value},
            projected=self._with_intake(self._state.change_intake + (record,)),
        )
        return self._change_intake(record.id)

    def dispose_change_intake(
        self,
        intake_id: str,
        *,
        disposition: IntakeDisposition,
        provenance: Provenance,
        linked_fact_id: str | None = None,
        linked_decision_id: str | None = None,
        linked_roadmap_id: str | None = None,
    ) -> ChangeIntake:
        """Record what became of a captured change."""
        previous = self._change_intake(intake_id)
        if previous.disposition is not IntakeDisposition.PENDING:
            raise ProjectLedgerCommandError(
                f"change intake '{intake_id}' is already {previous.disposition.value}"
            )
        if disposition is IntakeDisposition.PENDING:
            raise ProjectLedgerCommandError("disposing an intake requires a real disposition")
        known = self._state.record_ids()
        for link in (linked_fact_id, linked_decision_id, linked_roadmap_id):
            if link is not None and link not in known:
                raise ProjectLedgerCommandError(
                    f"change intake '{intake_id}' cannot link to unknown record '{link}'"
                )
        version = self.project_version + 1
        now = current_iso_timestamp()
        disposed = ChangeIntake(
            **{
                **previous.__dict__,
                "disposition": disposition,
                "provenance": provenance,
                "linked_fact_id": linked_fact_id,
                "linked_decision_id": linked_decision_id,
                "linked_roadmap_id": linked_roadmap_id,
                "decided_version": version,
                "decided_at": now,
            }
        )
        self._apply(
            version,
            rows=[
                ProjectLedgerRow(
                    "project_change_intake",
                    "update",
                    {
                        "id": disposed.id,
                        "disposition": disposition.value,
                        "provenance_json": json.dumps(provenance.to_dict(), sort_keys=True),
                        "linked_fact_id": linked_fact_id,
                        "linked_decision_id": linked_decision_id,
                        "linked_roadmap_id": linked_roadmap_id,
                        "decided_version": version,
                        "decided_at": now,
                    },
                )
            ],
            event_type=LedgerEventType.CHANGE_INTAKE_DISPOSED,
            subject_kind="change_intake",
            subject_id=disposed.id,
            payload={"disposition": disposition.value},
            projected=self._with_intake(_replace_record(self._state.change_intake, disposed)),
        )
        return self._change_intake(disposed.id)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _apply(
        self,
        version: int,
        *,
        rows: Sequence[ProjectLedgerRow],
        event_type: LedgerEventType,
        subject_kind: str,
        subject_id: str,
        payload: dict[str, Any],
        projected: ProjectState,
    ) -> None:
        """Persist one authoritative mutation and prove it reconstructs."""
        digest = projected.digest
        try:
            self._db.apply_project_ledger_mutation(
                project_id=self.project_id,
                project_version=version,
                rows=rows,
                event=ProjectLedgerEventRow(
                    project_id=self.project_id,
                    event_type=event_type.value,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    project_version=version,
                    state_digest=digest,
                    payload_json=json.dumps(payload, sort_keys=True),
                ),
            )
        except ValueError as exc:
            raise ProjectLedgerConflictError(
                f"project '{self.project_id}': mutation to version {version} was "
                f"refused by the store: {exc}"
            ) from exc
        reloaded = self.reload()
        if reloaded.digest != digest:
            raise ProjectLedgerIntegrityError(
                f"project '{self.project_id}': durable state at version {version} "
                f"reconstructs to {reloaded.digest}, not the recorded {digest}"
            )

    def _with_facts(self, facts: tuple[ProjectFact, ...]) -> ProjectState:
        return self._project(facts=facts)

    def _with_decisions(self, decisions: tuple[ProjectDecision, ...]) -> ProjectState:
        return self._project(decisions=decisions)

    def _with_roadmap(self, roadmap: tuple[RoadmapItem, ...]) -> ProjectState:
        return self._project(roadmap=roadmap)

    def _with_questions(self, questions: tuple[OpenQuestion, ...]) -> ProjectState:
        return self._project(open_questions=questions)

    def _with_intake(self, intake: tuple[ChangeIntake, ...]) -> ProjectState:
        return self._project(change_intake=intake)

    def _project(self, **replacements: Any) -> ProjectState:
        """The state this ledger will hold once the pending mutation lands."""
        identity = ProjectIdentityRecord(
            **{**self._state.identity.__dict__, "project_version": self.project_version + 1}
        )
        return ProjectState(
            identity=identity,
            facts=replacements.get("facts", self._state.facts),
            decisions=replacements.get("decisions", self._state.decisions),
            roadmap=replacements.get("roadmap", self._state.roadmap),
            open_questions=replacements.get("open_questions", self._state.open_questions),
            change_intake=replacements.get("change_intake", self._state.change_intake),
        )

    def _fact(self, fact_id: str) -> ProjectFact:
        for fact in self._state.facts:
            if fact.id == fact_id:
                return fact
        raise ProjectLedgerNotFoundError(f"project '{self.project_id}' has no fact '{fact_id}'")

    def _decision(self, decision_id: str) -> ProjectDecision:
        for decision in self._state.decisions:
            if decision.id == decision_id:
                return decision
        raise ProjectLedgerNotFoundError(
            f"project '{self.project_id}' has no decision '{decision_id}'"
        )

    def _roadmap_item(self, item_id: str) -> RoadmapItem:
        for item in self._state.roadmap:
            if item.id == item_id:
                return item
        raise ProjectLedgerNotFoundError(
            f"project '{self.project_id}' has no roadmap item '{item_id}'"
        )

    def _open_question(self, question_id: str) -> OpenQuestion:
        for question in self._state.open_questions:
            if question.id == question_id:
                return question
        raise ProjectLedgerNotFoundError(
            f"project '{self.project_id}' has no open question '{question_id}'"
        )

    def _change_intake(self, intake_id: str) -> ChangeIntake:
        for intake in self._state.change_intake:
            if intake.id == intake_id:
                return intake
        raise ProjectLedgerNotFoundError(
            f"project '{self.project_id}' has no change intake '{intake_id}'"
        )


def _require_text(value: str, label: str) -> str:
    text = value.strip()
    if not text:
        raise ProjectLedgerCommandError(f"{label} must not be empty")
    return text


type LedgerRecord = ProjectFact | ProjectDecision | RoadmapItem | OpenQuestion | ChangeIntake


def _replace_record[RecordT: LedgerRecord](
    records: tuple[RecordT, ...], updated: RecordT
) -> tuple[RecordT, ...]:
    """Return ``records`` with the record sharing ``updated``'s id replaced."""
    return tuple(updated if record.id == updated.id else record for record in records)


def load_project(db: Database, project_id: str) -> ProjectLedger:
    """Load a Project Ledger and verify durable state reconstructs correctly.

    The recorded digest for the project's current version is authoritative: if
    the reconstructed state disagrees with it, the ledger fails closed rather
    than trusting whichever row was written most recently.
    """
    state = read_project_state(db, project_id)
    recorded = db.get_project_state_digest_at(project_id, state.project_version)
    if recorded is not None and recorded != state.digest:
        raise ProjectLedgerIntegrityError(
            f"project '{project_id}': durable state at version {state.project_version} "
            f"reconstructs to {state.digest}, but the ledger recorded {recorded}"
        )
    return ProjectLedger(db, state)
