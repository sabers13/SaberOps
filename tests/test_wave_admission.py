"""Fail-closed wave admission: disjoint scopes, ancestry and route posture.

Hermetic: no provider call, no network, no sleeping. Ancestry is checked with a
real ``git`` repository created under ``tmp_path`` (the only I/O), or with an
injected predicate.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from saberops.wave_admission import (
    ANCESTRY_UNVERIFIED,
    BASE_NOT_ANCESTOR,
    INVALID_REVIEWER_ROUTE,
    INVALID_TARGET_HEAD,
    INVALID_WAVE,
    INVALID_WORKER_ROUTE,
    LANE_SELF_OVERLAP,
    OVERLAPPING_TOUCHES,
    GitAncestryChecker,
    WaveAdmissionDecision,
    admit_wave,
    lane_touch_overlaps,
    scope_covers,
    scopes_overlap,
)
from saberops.wave_models import Lane, Wave

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40

PosturePredicate = Callable[[str, str], bool]
AncestryPredicate = Callable[[str, str], bool]

ALWAYS_VALID: PosturePredicate = lambda role, route: True  # noqa: E731
ALWAYS_INVALID: PosturePredicate = lambda role, route: False  # noqa: E731
NEVER_ANCESTOR: AncestryPredicate = lambda ancestor, descendant: False  # noqa: E731
ALWAYS_ANCESTOR: AncestryPredicate = lambda ancestor, descendant: True  # noqa: E731


def _lane(lane_id: str, touches: list[str], **overrides: object) -> Lane:
    base: dict[str, object] = {
        "lane_id": lane_id,
        "task_id": f"task-{lane_id}",
        "prompt": "work",
        "touches": touches,
        "must_not_touch": [],
        "contracts": [],
        "retirement_table": {},
        "worker_route": "worker-chain",
        "reviewer_route": "reviewer-chain",
        "effort": "medium",
    }
    base.update(overrides)
    return Lane(**base)  # type: ignore[arg-type]


def _wave(lanes: list[Lane] | None = None, **overrides: object) -> Wave:
    built = lanes if lanes is not None else [_lane("L1", ["src/saberops"]), _lane("L2", ["tests"])]
    base: dict[str, object] = {
        "wave_id": "wave-1",
        "base_sha": SHA_A,
        "lanes": built,
        "merge_order": [lane.lane_id for lane in built],
        "named_glue": [],
        "budgets": {},
        "target_branch": "main",
    }
    base.update(overrides)
    return Wave(**base)  # type: ignore[arg-type]


def _admit(
    wave: Wave | None = None, target: str = SHA_B, **kwargs: object
) -> WaveAdmissionDecision:
    """Admit with a valid posture and ancestry unless overridden."""
    options: dict[str, object] = {
        "posture_validator": ALWAYS_VALID,
        "ancestry_checker": ALWAYS_ANCESTOR,
    }
    options.update(kwargs)
    return admit_wave(wave if wave is not None else _wave(), target, **options)  # type: ignore[arg-type]


# -- scope overlap helpers -------------------------------------------------


def test_scope_covers_is_prefix_exact() -> None:
    """Covers: ADR-0004 D2 (path-prefix overlap semantics)."""
    assert scope_covers("src/saberops", "src/saberops/a.py")
    assert scope_covers("src/saberops", "src/saberops")
    assert not scope_covers("src/saberops", "src/saberops-other/a.py")
    assert not scope_covers("src", "srcs/a.py")


def test_scopes_overlap_detects_nesting_and_symmetry() -> None:
    """Covers: ADR-0004 D2 (disjoint file sets)."""
    assert scopes_overlap(["src"], ["src/saberops/a.py"])
    assert scopes_overlap(["src/saberops/a.py"], ["src"])
    assert scopes_overlap(["src/a.py"], ["src/a.py"])
    assert not scopes_overlap(["src/a.py"], ["src/b.py"])
    assert not scopes_overlap(["src"], ["tests"])


def test_lane_touch_overlaps_reports_the_offending_patterns() -> None:
    """Covers: ADR-0004 D2 (overlap is reported deterministically)."""
    pairs = lane_touch_overlaps(
        _lane("L1", ["src/saberops"]), _lane("L2", ["src/saberops/a.py"])
    )
    assert pairs == (("src/saberops", "src/saberops/a.py"),)


# -- admission -------------------------------------------------------------


def test_disjoint_wave_with_valid_posture_and_ancestry_is_admitted() -> None:
    """Covers: ADR-0004 D2 + §4 invariant 2 (admission happy path)."""
    decision = _admit()
    assert decision.admitted is True
    assert decision.reasons == []
    assert decision.reason == ""


def test_overlapping_touches_are_rejected() -> None:
    """Covers: ADR-0004 D2 + §4 invariant 2 (overlap rejected at submit)."""
    wave = _wave(
        [
            _lane("L1", ["src/saberops"]),
            _lane("L2", ["src/saberops/a.py"]),
        ]
    )
    decision = _admit(wave)
    assert decision.admitted is False
    assert decision.reasons == [
        f"{OVERLAPPING_TOUCHES}:L1:L2:src/saberops:src/saberops/a.py"
    ]


def test_a_lane_may_not_touch_what_it_must_not_touch() -> None:
    """Covers: ADR-0004 D2 (self-conflict fails closed)."""
    # The model refuses a self-overlap on construction, so force it past the
    # constructor to prove admission is independently fail-closed too.
    wave = _wave([_lane("L1", ["src/a.py"])])
    lane = wave.lanes[0]
    object.__setattr__(lane, "must_not_touch", ["src/a.py"])
    decision = _admit(wave)
    assert decision.admitted is False
    assert reasons_with_prefix(decision.reasons, LANE_SELF_OVERLAP) == [
        f"{LANE_SELF_OVERLAP}:L1"
    ]


def test_base_that_is_not_an_ancestor_is_rejected() -> None:
    """Covers: ADR-0004 D2 (base must be an ancestor of the target branch)."""
    decision = _admit(ancestry_checker=NEVER_ANCESTOR)
    assert decision.admitted is False
    assert decision.reasons == [f"{BASE_NOT_ANCESTOR}:{SHA_A}:{SHA_B}"]


def test_ancestry_is_unverified_without_a_checker_or_a_repo() -> None:
    """Covers: ADR-0004 D2 (no silent ancestry assumption)."""
    decision = admit_wave(_wave(), SHA_B, ALWAYS_VALID)
    assert decision.admitted is False
    assert decision.reasons == [f"{ANCESTRY_UNVERIFIED}:{SHA_A}:{SHA_B}"]


def test_target_head_shape_is_validated() -> None:
    """Covers: ADR-0004 D2 (target head must be a commit SHA)."""
    decision = _admit(target="HEAD")
    assert decision.admitted is False
    assert INVALID_TARGET_HEAD in decision.reasons


def test_invalid_worker_and_reviewer_posture_are_rejected() -> None:
    """Covers: ADR-0004 D2 (every route has valid posture)."""
    decision = _admit(posture_validator=ALWAYS_INVALID)
    assert decision.admitted is False
    assert decision.reasons == [
        f"{INVALID_WORKER_ROUTE}:L1:worker-chain",
        f"{INVALID_REVIEWER_ROUTE}:L1:reviewer-chain",
        f"{INVALID_WORKER_ROUTE}:L2:worker-chain",
        f"{INVALID_REVIEWER_ROUTE}:L2:reviewer-chain",
    ]


def test_posture_validator_receives_role_and_route() -> None:
    """Covers: ADR-0004 D2 (posture is asked per role and route)."""
    seen: list[tuple[str, str]] = []

    def validator(role: str, route: str) -> bool:
        seen.append((role, route))
        return True

    _admit(posture_validator=validator)
    assert seen == [
        ("worker", "worker-chain"),
        ("reviewer", "reviewer-chain"),
        ("worker", "worker-chain"),
        ("reviewer", "reviewer-chain"),
    ]


def test_an_empty_route_is_reported_without_calling_the_validator() -> None:
    """Covers: ADR-0004 D2 (empty route cannot be in valid posture)."""
    wave = _wave([_lane("L1", ["src/a.py"])])
    object.__setattr__(wave.lanes[0], "worker_route", "  ")

    def validator(role: str, route: str) -> bool:
        assert route.strip(), "admission must not ask posture for an empty route"
        return True

    decision = _admit(wave, posture_validator=validator)
    assert decision.admitted is False
    assert f"{INVALID_WORKER_ROUTE}:L1:empty" in decision.reasons


def test_all_independent_failures_are_reported_together() -> None:
    """Covers: ADR-0004 D2 (one attempt reports every problem)."""
    wave = _wave(
        [
            _lane("L1", ["src/saberops"], worker_route="bad"),
            _lane("L2", ["src/saberops/a.py"]),
        ]
    )
    decision = _admit(
        wave, target="nope", posture_validator=ALWAYS_INVALID, ancestry_checker=NEVER_ANCESTOR
    )
    assert decision.admitted is False
    assert INVALID_TARGET_HEAD in decision.reasons
    assert reasons_with_prefix(decision.reasons, OVERLAPPING_TOUCHES)
    assert reasons_with_prefix(decision.reasons, INVALID_WORKER_ROUTE)
    assert reasons_with_prefix(decision.reasons, INVALID_REVIEWER_ROUTE)
    assert reasons_with_prefix(decision.reasons, BASE_NOT_ANCESTOR)


def test_structural_wave_problems_are_reported() -> None:
    """Covers: ADR-0004 D2 (admission re-checks the wave description)."""
    wave = _wave()
    object.__setattr__(wave, "lanes", list(wave.lanes) + [wave.lanes[0]])
    decision = _admit(wave)
    assert decision.admitted is False
    assert reasons_with_prefix(decision.reasons, INVALID_WAVE)


def test_a_broken_ancestry_checker_fails_closed() -> None:
    """Covers: ADR-0004 D2 (a broken predicate never admits)."""

    def exploding(ancestor: str, descendant: str) -> bool:
        raise RuntimeError("git unavailable")

    decision = _admit(ancestry_checker=exploding)
    assert decision.admitted is False
    assert decision.reasons == [f"{ANCESTRY_UNVERIFIED}:{SHA_A}:{SHA_B}:RuntimeError"]


def test_decisions_are_deterministic() -> None:
    """Covers: ADR-0004 D2 (deterministic admission)."""
    wave = _wave(
        [
            _lane("L1", ["src"]),
            _lane("L2", ["src/a.py"]),
        ]
    )
    first = _admit(wave)
    second = _admit(wave)
    assert first == second
    assert first.reasons == second.reasons


# -- real git ancestry -----------------------------------------------------


def _init_repo(path: Path) -> str:
    """Covers: helper (temporary git repository only)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_output(["git", "init", "-q", "-b", "main"], cwd=path)
    subprocess.check_output(["git", "config", "user.email", "t@example.invalid"], cwd=path)
    subprocess.check_output(["git", "config", "user.name", "T"], cwd=path)
    (path / "README.md").write_text("hi\n")
    subprocess.check_output(["git", "add", "-A"], cwd=path)
    subprocess.check_output(["git", "commit", "-qm", "init"], cwd=path)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def _commit(path: Path, name: str) -> str:
    (path / name).write_text("x\n")
    subprocess.check_output(["git", "add", "-A"], cwd=path)
    subprocess.check_output(["git", "commit", "-qm", name], cwd=path)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def test_git_ancestry_checker_accepts_an_ancestor_and_rejects_unrelated(tmp_path: Path) -> None:
    """Covers: ADR-0004 D2 (ancestry verified via git merge-base)."""
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    head = _commit(repo, "second.txt")
    checker = GitAncestryChecker(repo)
    assert checker(base, head) is True
    assert checker(head, base) is False

    # A sibling branch is not an ancestor of the target either.
    subprocess.check_output(["git", "checkout", "-q", "-b", "side", base], cwd=repo)
    side = _commit(repo, "side.txt")
    decision = admit_wave(_wave(base_sha=side), head, ALWAYS_VALID, repo=repo)
    assert decision.admitted is False
    assert decision.reasons == [f"{BASE_NOT_ANCESTOR}:{side}:{head}"]


def test_admit_wave_with_a_real_repository_admits_an_ancestor_base(tmp_path: Path) -> None:
    """Covers: ADR-0004 D2 + §4 invariant 2 (real git admission path)."""
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    head = _commit(repo, "second.txt")
    decision = admit_wave(_wave(base_sha=base), head, ALWAYS_VALID, repo=repo)
    assert decision.admitted is True
    assert decision.reasons == []


def reasons_with_prefix(reasons: list[str], prefix: str) -> list[str]:
    """Return the reasons beginning with ``prefix`` in their given order."""
    return [reason for reason in reasons if reason == prefix or reason.startswith(prefix + ":")]
