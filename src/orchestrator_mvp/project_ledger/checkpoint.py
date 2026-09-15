"""Deterministic Project checkpoints.

A checkpoint is a manifest, not a summary.  It contains identities, versions
and digests and nothing else: no prose, no narrative, no model output, no
secret, and no live infrastructure state.  Generating one is pure deterministic
software -- no model is ever asked which facts matter, because a model that
decided that would quietly become the authority the Project is supposed to be.

Verification is correspondingly strict.  A checkpoint must hash to its recorded
digest, must name the state digest the ledger durably recorded at that version,
must belong to the project it claims, must reference only records that exist,
and must sit on an intact ancestry chain.  Every failure is typed, and none of
them is recovered from by trusting whichever row was written most recently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from orchestrator_mvp.db import Database, current_iso_timestamp
from orchestrator_mvp.project_ledger.contracts import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointAncestryError,
    CheckpointDigestError,
    CheckpointStateMismatchError,
    FactCategory,
    ProjectCheckpoint,
    ProjectIdentityMismatchError,
    ProjectLedgerIntegrityError,
    ProjectLedgerNotFoundError,
    ProjectState,
    UnsupportedSchemaVersionError,
    canonical_json,
    digest_of,
)
from orchestrator_mvp.project_ledger.store import ProjectLedger, read_project_state

#: Maximum ancestry links walked before a chain is declared malformed.
_MAX_ANCESTRY_DEPTH = 10_000


def _checkpoint_id(manifest: dict[str, Any]) -> str:
    """Derive the checkpoint identity from its canonical manifest."""
    return f"ckpt_{digest_of(manifest)[:16]}"


def build_checkpoint(
    state: ProjectState,
    *,
    parent_checkpoint_id: str | None,
    created_at: str,
) -> ProjectCheckpoint:
    """Compute the deterministic checkpoint manifest for ``state``.

    Pure: identical state, parent and timestamp always produce an identical
    manifest, digest and identity.
    """
    non_goals = tuple(fact.id for fact in state.accepted_facts(FactCategory.NON_GOAL))
    authoritative = tuple(
        fact.id for fact in state.accepted_facts() if fact.id not in set(non_goals)
    )
    in_flight = state.in_flight_roadmap()
    draft = ProjectCheckpoint(
        checkpoint_id="",
        project_id=state.project_id,
        project_version=state.project_version,
        parent_checkpoint_id=parent_checkpoint_id,
        created_at=created_at,
        state_digest=state.digest,
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        authoritative_refs=authoritative,
        active_roadmap_refs=tuple(item.id for item in state.active_roadmap()),
        active_decision_refs=tuple(record.id for record in state.accepted_decisions()),
        open_question_refs=tuple(record.id for record in state.open_question_records()),
        current_work_refs=tuple(item.id for item in in_flight),
        non_goal_refs=non_goals,
        recovery_refs=tuple(
            item.id
            for item in in_flight
            if item.status.value in ("INTERRUPTED", "OUTCOME_UNCERTAIN")
        ),
        provisional_refs=tuple(fact.id for fact in state.provisional_facts()),
        pending_intake_refs=tuple(record.id for record in state.pending_intake()),
    )
    return ProjectCheckpoint(
        **{**draft.__dict__, "checkpoint_id": _checkpoint_id(draft.manifest())}
    )


def create_checkpoint(
    ledger: ProjectLedger,
    *,
    parent_checkpoint_id: str | None = None,
    created_at: str | None = None,
) -> ProjectCheckpoint:
    """Persist a deterministic checkpoint of the ledger's current state.

    The parent defaults to the project's newest checkpoint, so ancestry is
    explicit and unbroken without the caller having to track it.  Checkpointing
    an unchanged state returns the existing checkpoint rather than forking the
    chain with a duplicate.
    """
    state = ledger.state
    db = ledger.db
    latest = db.get_latest_project_checkpoint(state.project_id)
    if parent_checkpoint_id is None and latest is not None:
        parent_checkpoint_id = str(latest["checkpoint_id"])
    if parent_checkpoint_id is not None:
        parent_row = db.get_project_checkpoint(parent_checkpoint_id)
        if parent_row is None:
            raise CheckpointAncestryError(
                f"parent checkpoint '{parent_checkpoint_id}' does not exist"
            )
        if str(parent_row["project_id"]) != state.project_id:
            raise ProjectIdentityMismatchError(
                f"parent checkpoint '{parent_checkpoint_id}' belongs to project "
                f"'{parent_row['project_id']}', not '{state.project_id}'"
            )
        if int(parent_row["project_version"]) > state.project_version:
            raise CheckpointAncestryError(
                f"parent checkpoint '{parent_checkpoint_id}' is at version "
                f"{parent_row['project_version']}, ahead of state version "
                f"{state.project_version}"
            )
    if (
        latest is not None
        and str(latest["state_digest"]) == state.digest
        and int(latest["project_version"]) == state.project_version
        and str(latest["checkpoint_id"]) == (parent_checkpoint_id or "")
    ):
        return load_checkpoint(db, str(latest["checkpoint_id"]))

    checkpoint = build_checkpoint(
        state,
        parent_checkpoint_id=parent_checkpoint_id,
        created_at=created_at or current_iso_timestamp(),
    )
    db.insert_project_checkpoint(
        checkpoint_id=checkpoint.checkpoint_id,
        project_id=checkpoint.project_id,
        parent_checkpoint_id=checkpoint.parent_checkpoint_id,
        project_version=checkpoint.project_version,
        state_digest=checkpoint.state_digest,
        checkpoint_digest=checkpoint.digest,
        schema_version=checkpoint.schema_version,
        manifest_json=canonical_json(checkpoint.manifest()),
        created_at=checkpoint.created_at,
    )
    return checkpoint


def _checkpoint_from_row(row: dict[str, Any]) -> ProjectCheckpoint:
    """Rebuild a checkpoint from its stored manifest, failing closed."""
    schema_version = int(row["schema_version"])
    if schema_version > CHECKPOINT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"checkpoint '{row['checkpoint_id']}' declares schema version "
            f"{schema_version}; this build understands at most {CHECKPOINT_SCHEMA_VERSION}"
        )
    try:
        manifest = json.loads(str(row["manifest_json"]))
    except json.JSONDecodeError as exc:
        raise CheckpointDigestError(
            f"checkpoint '{row['checkpoint_id']}': manifest is not valid JSON: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise CheckpointDigestError(
            f"checkpoint '{row['checkpoint_id']}': manifest must be a JSON object"
        )
    try:
        return ProjectCheckpoint(
            checkpoint_id=str(row["checkpoint_id"]),
            project_id=str(manifest["project_id"]),
            project_version=int(manifest["project_version"]),
            parent_checkpoint_id=(
                None
                if manifest.get("parent_checkpoint_id") is None
                else str(manifest["parent_checkpoint_id"])
            ),
            created_at=str(manifest["created_at"]),
            state_digest=str(manifest["state_digest"]),
            schema_version=int(manifest["schema_version"]),
            authoritative_refs=tuple(str(ref) for ref in manifest.get("authoritative_refs", ())),
            active_roadmap_refs=tuple(
                str(ref) for ref in manifest.get("active_roadmap_refs", ())
            ),
            active_decision_refs=tuple(
                str(ref) for ref in manifest.get("active_decision_refs", ())
            ),
            open_question_refs=tuple(str(ref) for ref in manifest.get("open_question_refs", ())),
            current_work_refs=tuple(str(ref) for ref in manifest.get("current_work_refs", ())),
            non_goal_refs=tuple(str(ref) for ref in manifest.get("non_goal_refs", ())),
            recovery_refs=tuple(str(ref) for ref in manifest.get("recovery_refs", ())),
            provisional_refs=tuple(str(ref) for ref in manifest.get("provisional_refs", ())),
            pending_intake_refs=tuple(
                str(ref) for ref in manifest.get("pending_intake_refs", ())
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CheckpointDigestError(
            f"checkpoint '{row['checkpoint_id']}': manifest is malformed: {exc}"
        ) from exc


def load_checkpoint(db: Database, checkpoint_id: str) -> ProjectCheckpoint:
    """Rebuild one stored checkpoint without verifying it against state."""
    row = db.get_project_checkpoint(checkpoint_id)
    if row is None:
        raise ProjectLedgerNotFoundError(f"no checkpoint '{checkpoint_id}'")
    return _checkpoint_from_row(row)


@dataclass(frozen=True)
class VerifiedCheckpoint:
    """A checkpoint proven to describe the durable state it claims."""

    checkpoint: ProjectCheckpoint
    ancestry: tuple[str, ...]

    @property
    def checkpoint_id(self) -> str:
        """Identity of the verified checkpoint."""
        return self.checkpoint.checkpoint_id


def verify_checkpoint(
    db: Database,
    checkpoint_id: str,
    *,
    expected_project_id: str | None = None,
) -> VerifiedCheckpoint:
    """Verify a checkpoint's digest, identity, ancestry and state linkage.

    Returns the verified checkpoint and its full ancestry, oldest first.
    Raises a typed :class:`ProjectLedgerIntegrityError` subclass on the first
    failure; nothing here repairs, rebuilds, or falls back to the newest
    available row.
    """
    row = db.get_project_checkpoint(checkpoint_id)
    if row is None:
        raise ProjectLedgerNotFoundError(f"no checkpoint '{checkpoint_id}'")
    checkpoint = _checkpoint_from_row(row)

    stored_digest = str(row["checkpoint_digest"])
    if checkpoint.digest != stored_digest:
        raise CheckpointDigestError(
            f"checkpoint '{checkpoint_id}': manifest hashes to {checkpoint.digest}, "
            f"not the recorded {stored_digest}"
        )
    if _checkpoint_id(checkpoint.manifest()) != checkpoint_id:
        raise CheckpointDigestError(
            f"checkpoint '{checkpoint_id}': manifest derives identity "
            f"{_checkpoint_id(checkpoint.manifest())}"
        )
    for column, value in (
        ("project_id", checkpoint.project_id),
        ("state_digest", checkpoint.state_digest),
    ):
        if str(row[column]) != value:
            raise CheckpointDigestError(
                f"checkpoint '{checkpoint_id}': stored {column} '{row[column]}' "
                f"disagrees with its manifest '{value}'"
            )
    if int(row["project_version"]) != checkpoint.project_version:
        raise CheckpointDigestError(
            f"checkpoint '{checkpoint_id}': stored project_version "
            f"{row['project_version']} disagrees with its manifest "
            f"{checkpoint.project_version}"
        )
    if expected_project_id is not None and checkpoint.project_id != expected_project_id:
        raise ProjectIdentityMismatchError(
            f"checkpoint '{checkpoint_id}' belongs to project "
            f"'{checkpoint.project_id}', not '{expected_project_id}'"
        )

    ledger_row = db.get_project_ledger(checkpoint.project_id)
    if ledger_row is None:
        raise ProjectIdentityMismatchError(
            f"checkpoint '{checkpoint_id}' names project '{checkpoint.project_id}', "
            "which has no durable ledger"
        )

    recorded = db.get_project_state_digest_at(checkpoint.project_id, checkpoint.project_version)
    if recorded is None:
        raise CheckpointStateMismatchError(
            f"checkpoint '{checkpoint_id}': the ledger records no state at version "
            f"{checkpoint.project_version}"
        )
    if recorded != checkpoint.state_digest:
        raise CheckpointStateMismatchError(
            f"checkpoint '{checkpoint_id}': claims state digest {checkpoint.state_digest} "
            f"at version {checkpoint.project_version}, but the ledger recorded {recorded}"
        )

    state = read_project_state(db, checkpoint.project_id)
    if state.project_version == checkpoint.project_version and state.digest != recorded:
        raise CheckpointStateMismatchError(
            f"checkpoint '{checkpoint_id}': durable state at version "
            f"{checkpoint.project_version} now reconstructs to {state.digest}"
        )
    missing = sorted(checkpoint.referenced_ids() - state.record_ids())
    if missing:
        raise CheckpointStateMismatchError(
            f"checkpoint '{checkpoint_id}' references record(s) absent from the "
            f"ledger: {', '.join(missing)}"
        )

    return VerifiedCheckpoint(checkpoint=checkpoint, ancestry=_verify_ancestry(db, checkpoint))


def _verify_ancestry(db: Database, checkpoint: ProjectCheckpoint) -> tuple[str, ...]:
    """Walk a checkpoint's parents to a root, failing closed on any break."""
    chain: list[str] = [checkpoint.checkpoint_id]
    seen = {checkpoint.checkpoint_id}
    current = checkpoint
    while current.parent_checkpoint_id is not None:
        parent_id = current.parent_checkpoint_id
        if parent_id in seen:
            raise CheckpointAncestryError(
                f"checkpoint '{checkpoint.checkpoint_id}': ancestry revisits '{parent_id}'"
            )
        parent_row = db.get_project_checkpoint(parent_id)
        if parent_row is None:
            raise CheckpointAncestryError(
                f"checkpoint '{current.checkpoint_id}' names parent '{parent_id}', "
                "which does not exist"
            )
        parent = _checkpoint_from_row(parent_row)
        if parent.digest != str(parent_row["checkpoint_digest"]):
            raise CheckpointDigestError(
                f"ancestor checkpoint '{parent_id}' does not match its recorded digest"
            )
        if parent.project_id != checkpoint.project_id:
            raise ProjectIdentityMismatchError(
                f"ancestor checkpoint '{parent_id}' belongs to project "
                f"'{parent.project_id}', not '{checkpoint.project_id}'"
            )
        if parent.project_version > current.project_version:
            raise CheckpointAncestryError(
                f"ancestor checkpoint '{parent_id}' is at version "
                f"{parent.project_version}, ahead of its child "
                f"{current.checkpoint_id} at {current.project_version}"
            )
        seen.add(parent_id)
        chain.append(parent_id)
        if len(chain) > _MAX_ANCESTRY_DEPTH:
            raise CheckpointAncestryError(
                f"checkpoint '{checkpoint.checkpoint_id}': ancestry exceeds "
                f"{_MAX_ANCESTRY_DEPTH} links"
            )
        current = parent
    return tuple(reversed(chain))


def latest_verified_checkpoint(db: Database, project_id: str) -> VerifiedCheckpoint:
    """Verify and return the newest checkpoint of ``project_id``."""
    row = db.get_latest_project_checkpoint(project_id)
    if row is None:
        raise ProjectLedgerNotFoundError(f"project '{project_id}' has no checkpoint")
    return verify_checkpoint(db, str(row["checkpoint_id"]), expected_project_id=project_id)


__all__ = [
    "ProjectLedgerIntegrityError",
    "VerifiedCheckpoint",
    "build_checkpoint",
    "create_checkpoint",
    "latest_verified_checkpoint",
    "load_checkpoint",
    "verify_checkpoint",
]
