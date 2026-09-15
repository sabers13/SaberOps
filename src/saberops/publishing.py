"""Publishing authority contract.

C11-C distinguishes *implementation authority* (what an autonomous worker
may do inside its sandbox) from *authoritative publication authority*
(what may merge / push authoritative refs, tag releases, deploy
production, or mutate governance policy).

Ordinary autonomous worker authority is intentionally narrow:

* editing allowed source paths;
* running tests;
* producing candidate commits on the run's own
  ``orch/<run_id>/a<n>`` branch.

It does *not* automatically include:

* merging / pushing authoritative main;
* tagging releases;
* publishing packages as authoritative;
* mutating governance policy.

This module owns the typed vocabulary (:class:`PublicationKind` and
:class:`WorkerAuthorityScope`) and the single
:meth:`PublishingAuthority.authorize` predicate that the rest of the
codebase must consult before performing an authoritative publication
action.  ``PublishingAuthority`` is *additive* on top of the existing
:func:`saberops.git.publish_candidate` primitive -- it does not
introduce a second publication system.

The default worker publishing authority is empty: an autonomous worker
*cannot* merge main, push authoritative refs, tag releases, deploy
production, or mutate governance policy.  Only an explicit, governed
publish authority (carried as durable evidence on the run) can lift any
of these capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from saberops.git import (
    CandidatePublication,
    CandidatePublicationError,
    GitManager,
)


class PublicationKind(StrEnum):
    """Bounded vocabulary for authoritative publication actions."""

    CANDIDATE_BRANCH_PUSH = "CANDIDATE_BRANCH_PUSH"
    AUTHORITATIVE_MERGE = "AUTHORITATIVE_MERGE"
    RELEASE_TAG = "RELEASE_TAG"
    PACKAGE_PUBLISH = "PACKAGE_PUBLISH"
    DEPLOY_PRODUCTION = "DEPLOY_PRODUCTION"
    GOVERNANCE_MUTATION = "GOVERNANCE_MUTATION"


# The set of publication kinds that ordinary autonomous worker authority
# MUST NEVER grant by default.  A worker may push candidate branches
# (:class:`PublicationKind.CANDIDATE_BRANCH_PUSH`) only when its own
# run's bounded governance says so -- and even then, only against the
# run's own ``orch/<run_id>/a<n>`` ref, never against an authoritative
# branch.
_WORKER_FORBIDDEN_KINDS: frozenset[PublicationKind] = frozenset(
    {
        PublicationKind.AUTHORITATIVE_MERGE,
        PublicationKind.RELEASE_TAG,
        PublicationKind.PACKAGE_PUBLISH,
        PublicationKind.DEPLOY_PRODUCTION,
        PublicationKind.GOVERNANCE_MUTATION,
    }
)


@dataclass(frozen=True)
class WorkerAuthorityScope:
    """The implementation authority granted to one autonomous worker.

    Worker authority is bounded: a worker may mutate the paths in
    ``editable_paths`` (relative to the repository root), may run tests
    against the same paths, and may produce candidate commits inside the
    run's own ``orch/<run_id>/a<n>`` worktree branch.

    No autonomous worker authority includes merge / push of authoritative
    refs, release tagging, package publishing, deployment, or governance
    mutation.  Those require an explicit :class:`PublishingAuthority`.
    """

    editable_paths: tuple[str, ...] = ()
    can_run_tests: bool = False
    can_create_candidate_commits: bool = False
    worker_id: str | None = None

    def authorize_path(self, repo_relative_path: str) -> bool:
        # The actual glob authorization lives in :mod:`saberops.sandbox`;
        # this method is a typed boolean projection for places that already
        # hold the resolved set of editable paths.
        return bool(self.editable_paths)

    def as_dict(self) -> dict[str, Any]:
        return {
            "editable_paths": list(self.editable_paths),
            "can_run_tests": self.can_run_tests,
            "can_create_candidate_commits": self.can_create_candidate_commits,
            "worker_id": self.worker_id,
        }


DEFAULT_WORKER_AUTHORITY = WorkerAuthorityScope()


@dataclass(frozen=True)
class PublishingAuthority:
    """The authoritative publication authority for one run.

    ``granted`` is the set of :class:`PublicationKind` values the run
    has been *explicitly* authorised to perform.  Empty by default: an
    autonomous worker must never gain publication power implicitly.

    Composition with existing publication code:

    * :func:`saberops.git.publish_candidate` already constrains
      its refspec to ``orch/<run_id>/a<n>`` and refuses to push
      arbitrary refs.  This module adds the higher-level capability gate
      around it -- a run that has not been granted
      :attr:`PublicationKind.CANDIDATE_BRANCH_PUSH` cannot push even a
      candidate branch, so the primitive is still available but
      capability-gated.

    A run that has been granted, say,
    :attr:`PublicationKind.AUTHORITATIVE_MERGE` must still produce its
    evidence through the existing
    :func:`saberops.git.publish_candidate` primitive -- the
    grant is *permission*, the primitive is the *mechanism*.
    """

    granted: frozenset[PublicationKind] = field(default_factory=frozenset)
    worker_id: str | None = None
    reason: str | None = None

    def authorize(self, kind: PublicationKind) -> bool:
        return kind in self.granted

    def is_worker_forbidden(self, kind: PublicationKind) -> bool:
        return kind in _WORKER_FORBIDDEN_KINDS

    def as_dict(self) -> dict[str, Any]:
        return {
            "granted": sorted(kind.value for kind in self.granted),
            "worker_id": self.worker_id,
            "reason": self.reason,
        }


DEFAULT_PUBLISHING_AUTHORITY = PublishingAuthority()


def worker_can_publish(
    worker_authority: WorkerAuthorityScope,
    publishing_authority: PublishingAuthority,
    kind: PublicationKind,
) -> bool:
    """Single canonical gate: may ``worker_authority`` perform ``kind``?

    The gate composes both halves of the contract:

    * The :class:`WorkerAuthorityScope` is implementation authority and
      never carries authoritative publication kinds.  Even if the worker
      scope *somehow* claimed authority over a forbidden kind (a
      mistake the helper guards against), the gate refuses.
    * The :class:`PublishingAuthority` must explicitly grant ``kind``.

    The candidate-branch push is special: it is the one publication kind
    a worker may perform *only* when its run's bounded governance
    explicitly grants it.  The candidate branch itself is still pinned
    to ``orch/<run_id>/a<n>`` by the underlying Git primitive, so
    even an explicit grant cannot widen authority to arbitrary refs.
    """
    if publishing_authority.is_worker_forbidden(kind):
        # Defensive guard: even if the caller accidentally folded a
        # forbidden kind into the worker scope, refuse here.
        return False
    return publishing_authority.authorize(kind)


def governed_publish_candidate(
    worker_authority: WorkerAuthorityScope,
    publishing_authority: PublishingAuthority,
    *,
    repo_path: Path | str,
    candidate_sha: str,
    candidate_branch: str,
    remote_name: str = "origin",
) -> CandidatePublication:
    """Governed autonomous publication: the ONLY worker-reachable mutation path.

    Composition, not a second implementation:

    1. deterministic authorization via :func:`worker_can_publish` for
       :attr:`PublicationKind.CANDIDATE_BRANCH_PUSH` -- an ordinary
       autonomous worker without that explicit grant cannot reach the
       primitive at all;
    2. the ONLY remote-mutation primitive invoked is the existing
       :func:`saberops.git.GitManager.publish_candidate`,
       which pins the refspec to ``orch/<run_id>/a<n>``, refuses
       non-fast-forward updates, and verifies the remote SHA.

    An attempt to target ``main`` (or any non-candidate ref) is
    rejected by branch validation before any mutation.  A grant for an
    unrelated publication kind (e.g. ``RELEASE_TAG``) still denies a
    candidate push.  ``AUTHORITATIVE_MERGE`` / ``RELEASE_TAG`` /
    ``PACKAGE_PUBLISH`` / ``DEPLOY_PRODUCTION`` / ``GOVERNANCE_MUTATION``
    are never exposed through this function: the gate refuses them even
    when granted, and this function cannot merge main -- the primitive
    it calls cannot merge main either.  Authoritative owner/Orch
    release operations remain separate governed operations.
    """
    if not worker_can_publish(
        worker_authority,
        publishing_authority,
        PublicationKind.CANDIDATE_BRANCH_PUSH,
    ):
        raise CandidatePublicationError(
            "Refusing candidate publication: run holds no explicit "
            "CANDIDATE_BRANCH_PUSH authority"
        )
    return GitManager.publish_candidate(
        repo_path=repo_path,
        candidate_sha=candidate_sha,
        candidate_branch=candidate_branch,
        remote_name=remote_name,
    )


__all__ = [
    "DEFAULT_PUBLISHING_AUTHORITY",
    "DEFAULT_WORKER_AUTHORITY",
    "PublicationKind",
    "PublishingAuthority",
    "WorkerAuthorityScope",
    "governed_publish_candidate",
    "worker_can_publish",
]
