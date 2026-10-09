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


def verification_contract(
    gate_command: str, gate_enabled: bool, review_enabled: bool, review_disabled: bool
) -> str:
    """Describe the frozen engine policy, independently of model-authored guidance."""
    lines = ["[RUN VERIFICATION]"]
    if gate_enabled:
        lines.extend(
            [
                f"Frozen authoritative gate command (from the project root): {gate_command}",
                "Preserve or create the entrypoint required by this exact command. "
                "A successful worker report or a different test script does not replace it.",
            ]
        )
        if gate_command.strip() == "make gate":
            lines.append(
                "The project root must have a Makefile with a gate target. "
                "You may create that root Makefile and delegate to an application test script."
            )
        lines.append(
            "Use focused checks; SaberOps runs the authoritative full gate on the candidate."
        )
    else:
        lines.append("The authoritative gate is disabled by the frozen project policy.")
    if review_disabled:
        lines.append("Independent engine review is disabled by the project policy.")
    elif review_enabled:
        lines.append("Independent SaberOps review is requested for this run.")
    else:
        lines.append("Independent SaberOps review follows the frozen review policy.")
    lines.append("A worker's own subagent review is not an independent SaberOps reviewer dispatch.")
    return "\n".join(lines)
