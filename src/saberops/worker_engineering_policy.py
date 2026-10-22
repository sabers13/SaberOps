"""Compact, deterministic engineering guidance for implementation workers.

This is advisory model-visible text, never an authorization or a replacement for
frozen project policy, sandbox controls, deterministic gates, or independent review.
It is authored here rather than importing a third-party skill at runtime.
"""

from __future__ import annotations

WORKER_ENGINEERING_POLICY_MARKER = "[WORKER ENGINEERING POLICY v1]"

WORKER_ENGINEERING_POLICY_TEXT = (
    "[WORKER ENGINEERING POLICY v1]\n"
    "1. Clarify only material blockers (unsafe or irreversible ambiguity, conflicting "
    "acceptance criteria, or missing authority). Otherwise proceed on a reasonable, "
    "reversible assumption and report it briefly. Do not invent human checkpoints.\n"
    "2. Choose the smallest correct implementation consistent with existing architecture "
    "and the explicit task contract. Do not add speculative features, layers, or configuration.\n"
    "3. Preserve necessary input validation, error handling, concurrency safety, and "
    "observability at real failure boundaries (I/O, APIs, persistence, and external tools). "
    "Simplicity never justifies deleting required safeguards.\n"
    "4. Keep changes surgical, but address all in-scope causes of a defect, including "
    "necessary cross-file changes. Do not touch unauthorized paths; report a concrete "
    "scope blocker instead of silently expanding ownership or making a partial fix.\n"
    "5. Verify against the provided acceptance criteria using focused checks. Follow "
    "the frozen verification policy; do not create extra gates, reviews, or phases. "
    "Only the engine initiates mandatory independent reviews and full gates.\n"
    "6. Repair failed focused checks within the same assignment and ownership boundary. "
    "Once the defined criteria pass, return the candidate; do not start open-ended "
    "polishing or repeated self-review. If blocked, report exact evidence and stop.\n"
    "Higher-priority task authority, security, scope, and engine validation rules always win."
)
