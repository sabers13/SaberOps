"""Compact worker validation policy injected into Orch worker task contracts.

The human-readable canonical policy lives in ``VALIDATION_POLICY.md`` at the
repository root. This module carries the compact operational version that is
appended to every Orch-generated worker task contract (implementation, retry,
repair, and final-repair) so that prompt builders never duplicate slightly
different copies of the rule.

C03 ownership invariant (UNAMBIGUOUS — one meaning only):

  WORKER        — focused validation only:
                    * smallest relevant focused tests
                    * nearby regressions where warranted
                    * selective Ruff/mypy/type checks where warranted
                    * returns the candidate to Orch when focused checks
                      are satisfactory
                  The worker must NEVER run the authoritative full repository
                  gate (no ``make gate``, no repository-wide pytest, no
                  ``git diff --check`` followed by the full gate).

  ORCHESTRATOR  — sole owner of the authoritative full repository gate.
                  Orch executes it deterministically and waits for it
                  without dispatching another model.

The compact policy appended to every worker contract makes that ownership
boundary explicit so prompt builders never drift back to the historical
B1-failure pattern of a worker running a full gate and an Orchestrator
re-running it.
"""

from __future__ import annotations

VALIDATION_POLICY_MARKER = "VALIDATION POLICY:"

VALIDATION_POLICY_TEXT = (
    "VALIDATION POLICY:\n"
    "OWNERSHIP: WORKER = focused validation only.\n"
    "OWNERSHIP: ORCHESTRATOR owns the authoritative full repository gate.\n"
    "During implementation/repair, use focused tests and nearby regression checks.\n"
    "Use selective Ruff/mypy/type checks when useful.\n"
    "Do NOT run the authoritative complete repository gate; that is Orchestrator's job.\n"
    "Do NOT run a complete repository-wide pytest suite merely as final submission validation.\n"
    "Do NOT run make gate, do NOT run git diff --check followed by the gate.\n"
    "When your candidate is believed final, return it to the Orchestrator.\n"
    "Do not run the authoritative full repository gate yourself.\n"
    "Orch will execute that gate deterministically.\n"
    "If Orch reports a real validation failure, repair with focused tests and "
    "return the next candidate; Orch will run the authoritative full gate again.\n"
    "No candidate may be accepted without a successful authoritative full gate for its final SHA."
)


def with_validation_policy(task: str) -> str:
    """Return ``task`` with the compact validation policy appended exactly once.

    Idempotent: if the policy marker is already present the task is returned
    unchanged, so composed prompt builders can never duplicate the policy.
    """
    stripped = task.rstrip()
    if VALIDATION_POLICY_MARKER in stripped:
        return stripped
    return stripped + "\n\n" + VALIDATION_POLICY_TEXT
