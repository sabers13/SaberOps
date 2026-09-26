"""R3-B: bounded read-only candidate-diff projection.

Derives the exact current candidate selected by the R3-A
``project_candidate_state`` projection and renders the frozen
``base_commit .. candidate_sha`` diff from the run's persisted
repository.  Projection only: no DB writes, no events, no worktree
creation, no branch mutation, no checkout/reset, no gate/review
execution.

Diff identity rule: ``run -> frozen base_commit ->
project_candidate_state(...).candidate_sha -> validated run
repository -> git diff base_commit candidate_sha``.  A later FAILED
repair attempt carrying its own SHA never replaces the verified
current candidate because candidate identity is never re-derived
here -- it is always the R3-A projection's ``candidate_sha``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from saberops.process import run_process

if TYPE_CHECKING:
    from saberops.db import Database

#: Maximum rendered diff text kept in the projection (chars).  A
#: candidate may contain a huge diff; the endpoint/page must never
#: return an unbounded git diff.
DIFF_MAX_CHARS: int = 65536

#: Maximum changed paths kept in the projection.  Longer name lists
#: are truncated with ``truncated=True`` (never silently complete).
CHANGED_PATHS_MAX: int = 500

_SHA_RE = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class CandidateDiff:
    """Typed read-only diff projection for one run's current candidate."""

    run_id: str
    base_sha: str | None
    candidate_sha: str | None
    available: bool
    changed_paths: tuple[str, ...]
    diff_text: str
    truncated: bool
    unavailable_reason: str | None

    def as_dict(self) -> dict[str, object]:
        """Serialize for the JSON endpoint (no binary content, bounded)."""
        return {
            "run_id": self.run_id,
            "base_sha": self.base_sha,
            "candidate_sha": self.candidate_sha,
            "available": self.available,
            "changed_paths": list(self.changed_paths),
            "diff_text": self.diff_text,
            "truncated": self.truncated,
            "unavailable_reason": self.unavailable_reason,
        }


def _split_null_names(raw: str) -> list[str]:
    return [part for part in raw.split("\0") if part]


def project_candidate_diff(db: Database, run_id: str) -> CandidateDiff:
    """Project the bounded ``base_commit .. candidate_sha`` diff for ``run_id``.

    Pure read path: no writes, no events, no worktree creation, no
    branch mutation, no checkout/reset.  Works after the candidate
    worktree has been removed -- only the persisted run repository's
    Git objects are consulted.
    """
    from saberops.candidate_lifecycle import project_candidate_state
    from saberops.provenance import ProvenanceError, assert_run_repository

    run = db.get_run(run_id)
    if run is None:
        return CandidateDiff(
            run_id=run_id,
            base_sha=None,
            candidate_sha=None,
            available=False,
            changed_paths=(),
            diff_text="",
            truncated=False,
            unavailable_reason="run_unknown",
        )

    base_sha = run.base_commit
    # Candidate authority comes exclusively from the R3-A projection:
    # the diff may proceed only when R3-A proves an owner-visible
    # lifecycle state for the candidate (candidate_sha is not None AND
    # state is not None).  A SHA with state None (e.g.
    # "candidate_without_gate_success") is gate-unproven and must never
    # render a diff.  No gate/check selection logic is recreated here.
    projection = project_candidate_state(db, run_id)
    candidate_sha = projection.candidate_sha
    if candidate_sha is None or projection.state is None:
        return CandidateDiff(
            run_id=run_id,
            base_sha=base_sha,
            candidate_sha=None,
            available=False,
            changed_paths=(),
            diff_text="",
            truncated=False,
            unavailable_reason=projection.reason or "no_trustworthy_candidate",
        )
    if _SHA_RE.fullmatch(base_sha) is None or _SHA_RE.fullmatch(candidate_sha) is None:
        return CandidateDiff(
            run_id=run_id,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            available=False,
            changed_paths=(),
            diff_text="",
            truncated=False,
            unavailable_reason="unresolvable_commit",
        )

    # Repository identity comes from the run's persisted provenance
    # seam -- never from the currently selected UI project.
    try:
        repo = assert_run_repository(run)
    except (ProvenanceError, Exception):
        return CandidateDiff(
            run_id=run_id,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            available=False,
            changed_paths=(),
            diff_text="",
            truncated=False,
            unavailable_reason="repository_unavailable",
        )
    repo_path: Path = Path(repo)

    # Both endpoints must exist as commit objects before diffing.
    for sha in (base_sha, candidate_sha):
        probe = run_process(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_path)
        if not probe.passed:
            return CandidateDiff(
                run_id=run_id,
                base_sha=base_sha,
                candidate_sha=candidate_sha,
                available=False,
                changed_paths=(),
                diff_text="",
                truncated=False,
                unavailable_reason="unresolvable_commit",
            )

    # Changed paths via argv-only plumbing.  ``-z`` keeps odd
    # filenames unambiguous; ``--`` guards SHAs that look like flags.
    names_res = run_process(
        ["git", "diff", "--name-only", "-z", base_sha, candidate_sha, "--"],
        cwd=repo_path,
    )
    if not names_res.passed:
        return CandidateDiff(
            run_id=run_id,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            available=False,
            changed_paths=(),
            diff_text="",
            truncated=False,
            unavailable_reason="diff_failed",
        )
    all_paths = _split_null_names(names_res.stdout)
    paths_truncated = len(all_paths) > CHANGED_PATHS_MAX
    changed_paths = tuple(all_paths[:CHANGED_PATHS_MAX])

    # Default git diff renders binary files as "Binary files ... differ"
    # without dumping binary contents; ``--text`` is deliberately never
    # passed.  ``--no-color``/``--no-ext-diff`` keep output deterministic
    # and free of pager/driver side effects.
    diff_res = run_process(
        [
            "git",
            "--no-pager",
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base_sha,
            candidate_sha,
            "--",
        ],
        cwd=repo_path,
    )
    if not diff_res.passed:
        return CandidateDiff(
            run_id=run_id,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            available=False,
            changed_paths=(),
            diff_text="",
            truncated=False,
            unavailable_reason="diff_failed",
        )
    diff_text = diff_res.stdout
    text_truncated = len(diff_text) > DIFF_MAX_CHARS
    if text_truncated:
        diff_text = diff_text[:DIFF_MAX_CHARS]
    return CandidateDiff(
        run_id=run_id,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        available=True,
        changed_paths=changed_paths,
        diff_text=diff_text,
        truncated=bool(paths_truncated or text_truncated),
        unavailable_reason=None,
    )
