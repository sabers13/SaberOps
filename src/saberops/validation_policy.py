"""Compact worker validation policy injected into Orch worker task contracts.

The human-readable canonical policy lives in ``VALIDATION_POLICY.md`` at the
repository root. This module carries the compact operational version that is
appended to every Orch-generated worker task contract (implementation, retry,
repair, and final-repair) so that prompt builders never duplicate slightly
different copies of the rule.

Ownership invariant (UNAMBIGUOUS — one meaning only):

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
boundary explicit so prompt builders never drift back to a worker running
a full gate and the Orchestrator re-running it.
"""

from __future__ import annotations

VALIDATION_POLICY_MARKER = "VALIDATION POLICY:"

#: Engine-generated worker instructions appended to every build-role attempt
#: Build role only: a scout keeps its own section and a reviewer or advisor
#: keeps its fixed instructions (ADR-0089).
WORKER_INSTRUCTIONS_MARKER = "[HOW TO WORK]"

#: The main skill and this section are the only guidance the planner governs;
#: both are bounded by tests.
MAX_WORKER_INSTRUCTION_LINES = 16

WORKER_INSTRUCTIONS = (
    "[HOW TO WORK]\n"
    "1. Make the smallest complete change: does it need to exist, is it already\n"
    "   here, does the standard library do it, does an installed dependency do it,\n"
    "   can it be one clear line; only then new code.\n"
    "2. Never cut validation at a trust boundary, error handling that prevents\n"
    "   data loss, security, or accessibility.\n"
    "3. Edit surgically. Mention unrelated dead code in your report; never delete\n"
    "   or reformat it.\n"
    "4. A deliberate shortcut gets a comment: `shortcut: <limit>, <when to upgrade>`.\n"
    "5. On a repair attempt, check each review finding against the code before\n"
    "   changing anything. Put a disagreement in your report for the planner; do\n"
    "   not start a rebuttal loop.\n"
    "6. End your final report with three lines: what you skipped, what you did not\n"
    "   check, and the risks.\n"
    "7. Add nothing without a caller: no unused function, parameter, option or\n"
    "   alias; reuse the existing helper and name it in your report."
)


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



def verification_contract(
    gate_command: str, gate_enabled: bool, review_disabled: bool
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
        lines.append("Independent engine review is off for this manual run by the owner switch.")
    else:
        lines.append("Independent SaberOps review is mandatory for every accepted candidate.")
    lines.append("A worker's own subagent review is not an independent SaberOps reviewer dispatch.")
    return "\n".join(lines)
