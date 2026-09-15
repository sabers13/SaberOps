"""Read-only Project Ledger observability.

Observability reveals durable state and does nothing else.  It performs no
semantic provider call, no worker call and no mutation; it does not repair a
broken digest, rebuild a missing checkpoint, or promote anything provisional.
When state fails to verify, the snapshot *reports* that as a finding and leaves
the repair decision to a human or to an explicit typed command.

Statements and rationales are Project truth written by the owner and Orch, and
no secret material is ever accepted into a ledger record, so a snapshot carries
counts, identities, digests and statuses rather than free-form payloads.
"""

from __future__ import annotations

from typing import Any

from orchestrator_mvp.db import Database
from orchestrator_mvp.project_ledger.checkpoint import verify_checkpoint
from orchestrator_mvp.project_ledger.contracts import (
    ProjectLedgerError,
    ProjectLedgerIntegrityError,
)
from orchestrator_mvp.project_ledger.store import read_project_state


def project_ledger_snapshot(db: Database, project_id: str) -> dict[str, Any]:
    """Return a read-only, secret-free view of one Project Ledger.

    Never raises for an unhealthy project: an integrity failure is reported as
    a finding so an operator can see it, because an inspection tool that
    crashes on the broken case is useless exactly when it is needed.
    """
    ledger_row = db.get_project_ledger(project_id)
    if ledger_row is None:
        return {"project_id": project_id, "present": False, "findings": ["no durable ledger"]}

    findings: list[str] = []
    try:
        state = read_project_state(db, project_id)
    except ProjectLedgerError as exc:
        return {
            "project_id": project_id,
            "present": True,
            "findings": [f"state does not reconstruct: {exc}"],
        }

    recorded = db.get_project_state_digest_at(project_id, state.project_version)
    if recorded is not None and recorded != state.digest:
        findings.append(
            f"state digest {state.digest} disagrees with the recorded {recorded} "
            f"at version {state.project_version}"
        )

    checkpoints: list[dict[str, Any]] = []
    for row in db.list_project_checkpoints(project_id):
        checkpoint_id = str(row["checkpoint_id"])
        entry: dict[str, Any] = {
            "checkpoint_id": checkpoint_id,
            "parent_checkpoint_id": row["parent_checkpoint_id"],
            "project_version": int(row["project_version"]),
            "state_digest": str(row["state_digest"]),
            "created_at": str(row["created_at"]),
            "verified": True,
        }
        try:
            verify_checkpoint(db, checkpoint_id, expected_project_id=project_id)
        except ProjectLedgerIntegrityError as exc:
            entry["verified"] = False
            entry["finding"] = str(exc)
            findings.append(f"checkpoint {checkpoint_id}: {exc}")
        checkpoints.append(entry)

    return {
        "project_id": project_id,
        "present": True,
        "origin": state.identity.origin.value,
        "canonical_path": state.identity.canonical_path,
        "root_commit": state.identity.root_commit,
        "schema_version": state.identity.schema_version,
        "project_version": state.project_version,
        "state_digest": state.digest,
        "counts": {
            "facts": len(state.facts),
            "accepted_facts": len(state.accepted_facts()),
            "provisional_facts": len(state.provisional_facts()),
            "decisions": len(state.decisions),
            "accepted_decisions": len(state.accepted_decisions()),
            "roadmap_items": len(state.roadmap),
            "active_roadmap_items": len(state.active_roadmap()),
            "in_flight_roadmap_items": len(state.in_flight_roadmap()),
            "open_questions": len(state.open_question_records()),
            "change_intake": len(state.change_intake),
            "pending_change_intake": len(state.pending_intake()),
            "events": len(db.list_project_ledger_events(project_id)),
        },
        "checkpoints": checkpoints,
        "findings": findings,
    }
