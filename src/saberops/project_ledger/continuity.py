"""Composition with C05 runtime lifecycle state.

C11 owns *Project* continuity: what the Project is for, what it decided, what
it is doing, and what remains true after every session ends.  C05 already owns
*run* continuity: activations, interruptions, worker checkpoints and the
side-effect journal that makes recovery exactly-once.

Those are different questions, so this module reads C05's durable records
rather than re-deriving them.  Nothing here writes runtime state, re-classifies
a run, decides a recovery action, or keeps a second copy of a lifecycle field:
a roadmap item stores only the run reference, and the runtime answer is looked
up from C05 every time it is needed.
"""

from __future__ import annotations

from saberops.db import Database
from saberops.models import SideEffectState, WorkerActivationState
from saberops.project_ledger.contracts import RuntimeComposition

#: Activation states that mean a worker stopped without a settled outcome.
_INTERRUPTED_STATES = frozenset(
    {
        WorkerActivationState.INTERRUPTED,
        WorkerActivationState.QUIESCING,
        WorkerActivationState.CHECKPOINTED,
        WorkerActivationState.TERMINATION_UNCERTAIN,
    }
)


def runtime_composition(db: Database, run_ref: str) -> RuntimeComposition:
    """Read C05 runtime facts about ``run_ref`` without changing anything.

    A run this store has never heard of yields ``run_known=False`` rather than
    an invented status: an unknown run is unknown, never assumed finished.
    """
    run = db.get_run(run_ref)
    if run is None:
        return RuntimeComposition(run_ref=run_ref, run_known=False)
    activations = db.get_worker_activations_for_run(run_ref)
    latest = activations[-1] if activations else None
    uncertain = tuple(
        record.idempotency_key
        for record in db.get_side_effects_for_run(run_ref)
        if record.state is SideEffectState.UNCERTAIN
    )
    return RuntimeComposition(
        run_ref=run_ref,
        run_known=True,
        run_status=run.status.value,
        latest_activation_state=None if latest is None else latest.state.value,
        uncertain_side_effects=uncertain,
        interrupted=latest is not None and latest.state in _INTERRUPTED_STATES,
        termination_uncertain=(
            latest is not None and latest.state is WorkerActivationState.TERMINATION_UNCERTAIN
        ),
    )
