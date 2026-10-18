"""Hermetic tests for the stable project registry (Step 015 / PL-1).

Every test is hermetic: all state lives under ``tmp_path`` via an explicit
registry file, with no provider call, no network and no clock. Each case
states the invariant it covers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saberops.cli import main
from saberops.project import (
    ProjectCapError,
    ProjectRegistrationError,
    ProjectRegistry,
    RegisteredProject,
)
from saberops.ready_frontier import DEFAULT_ACTIVE_PROJECT_LIMIT


def _registry(tmp_path: Path) -> ProjectRegistry:
    """Covers: registry isolation — every test owns its backing file."""
    return ProjectRegistry(tmp_path / "registry" / "project-registry.json")


def _repo(tmp_path: Path, name: str = "repo") -> str:
    """Covers: registration requires a real existing repository root."""
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def test_register_round_trip_defaults_inactive(tmp_path: Path) -> None:
    """Covers: explicit registration persists every field, inactive by default."""
    registry = _registry(tmp_path)
    project = registry.register("alpha-1", "Alpha", _repo(tmp_path), str(tmp_path / "state"))
    assert project.project_id == "alpha-1"
    assert project.display_name == "Alpha"
    assert project.active is False
    assert registry.get("alpha-1") == project


def test_register_active_flag(tmp_path: Path) -> None:
    """Covers: explicit activation at registration time."""
    registry = _registry(tmp_path)
    project = registry.register("alpha-1", "Alpha", _repo(tmp_path), str(tmp_path), active=True)
    assert project.active is True
    assert [item.project_id for item in registry.active_projects()] == ["alpha-1"]


@pytest.mark.parametrize("slug", ["a", "a1", "abc-123", "x" * 64, "0-9-z"])
def test_valid_slugs_accepted(tmp_path: Path, slug: str) -> None:
    """Covers: slug contract — lowercase alnum with interior dashes, 1-64 chars."""
    registry = _registry(tmp_path)
    project = registry.register(slug, "Name", _repo(tmp_path), str(tmp_path))
    assert project.project_id == slug


@pytest.mark.parametrize(
    "slug",
    [
        "",
        "ABC",
        "-lead",
        "trail-",
        "has space",
        "under_score",
        "dot.name",
        "UPPER",
        "x" * 65,
        "a/b",
        "shout!",
    ],
)
def test_malformed_slugs_refused(tmp_path: Path, slug: str) -> None:
    """Covers: malformed slugs are refused, never normalized or inferred."""
    registry = _registry(tmp_path)
    with pytest.raises(ProjectRegistrationError):
        registry.register(slug, "Name", _repo(tmp_path), str(tmp_path))


def test_empty_identity_refused_without_inference(tmp_path: Path) -> None:
    """Covers: unauthenticated directory-inferred registration is refused."""
    registry = _registry(tmp_path)
    with pytest.raises(ProjectRegistrationError):
        registry.register("", "Name", _repo(tmp_path), str(tmp_path))
    assert registry.list_projects() == ()


def test_duplicate_project_id_refused(tmp_path: Path) -> None:
    """Covers: duplicate project IDs are refused; the original is untouched."""
    registry = _registry(tmp_path)
    first = registry.register("alpha-1", "Alpha", _repo(tmp_path), str(tmp_path))
    with pytest.raises(ProjectRegistrationError):
        registry.register("alpha-1", "Other", _repo(tmp_path), str(tmp_path))
    assert registry.get("alpha-1") == first


def test_blank_display_name_refused(tmp_path: Path) -> None:
    """Covers: display name is required — no anonymous registrations."""
    registry = _registry(tmp_path)
    with pytest.raises(ProjectRegistrationError):
        registry.register("alpha-1", "   ", _repo(tmp_path), str(tmp_path))


def test_missing_paths_refused(tmp_path: Path) -> None:
    """Covers: repository_root and state_root are both required."""
    registry = _registry(tmp_path)
    with pytest.raises(ProjectRegistrationError):
        registry.register("alpha-1", "Alpha", "", str(tmp_path))
    with pytest.raises(ProjectRegistrationError):
        registry.register("alpha-1", "Alpha", _repo(tmp_path), "")


def test_nonexistent_repository_root_refused(tmp_path: Path) -> None:
    """Covers: fail-closed on a repository root that does not exist."""
    registry = _registry(tmp_path)
    with pytest.raises(ProjectRegistrationError):
        registry.register("alpha-1", "Alpha", str(tmp_path / "nope"), str(tmp_path))


def test_file_repository_root_refused(tmp_path: Path) -> None:
    """Covers: repository root must be a directory, not a file."""
    registry = _registry(tmp_path)
    marker = tmp_path / "file.txt"
    marker.write_text("x", encoding="utf-8")
    with pytest.raises(ProjectRegistrationError):
        registry.register("alpha-1", "Alpha", str(marker), str(tmp_path))


def test_active_cap_matches_owner_policy() -> None:
    """Covers: registry cap is the owner-approved 3-active-project limit."""
    assert DEFAULT_ACTIVE_PROJECT_LIMIT == 3


def test_register_active_beyond_cap_refused(tmp_path: Path) -> None:
    """Covers: the 3-active-project cap is enforced at registration."""
    registry = _registry(tmp_path)
    for index in range(3):
        registry.register(
            f"p-{index}",
            f"P {index}",
            _repo(tmp_path, f"repo-{index}"),
            str(tmp_path),
            active=True,
        )
    with pytest.raises(ProjectCapError):
        registry.register("p-3", "P 3", _repo(tmp_path, "repo-3"), str(tmp_path), active=True)
    assert len(registry.active_projects()) == 3
    fourth = registry.register("p-3", "P 3", _repo(tmp_path, "repo-3"), str(tmp_path))
    assert fourth.active is False


def test_activate_beyond_cap_refused(tmp_path: Path) -> None:
    """Covers: activation enforces the cap; deactivation frees a slot."""
    registry = _registry(tmp_path)
    for index in range(4):
        registry.register(
            f"p-{index}", f"P {index}", _repo(tmp_path, f"repo-{index}"), str(tmp_path)
        )
    registry.activate("p-0")
    registry.activate("p-1")
    registry.activate("p-2")
    with pytest.raises(ProjectCapError):
        registry.activate("p-3")
    registry.deactivate("p-2")
    assert registry.activate("p-3").active is True
    assert {item.project_id for item in registry.active_projects()} == {"p-0", "p-1", "p-3"}


def test_activate_unknown_refused(tmp_path: Path) -> None:
    """Covers: activating or deactivating an unknown project fails closed."""
    registry = _registry(tmp_path)
    with pytest.raises(ProjectRegistrationError):
        registry.activate("ghost")
    with pytest.raises(ProjectRegistrationError):
        registry.deactivate("ghost")


def test_cap_error_is_registration_error(tmp_path: Path) -> None:
    """Covers: cap violations are registration errors (single except surface)."""
    assert issubclass(ProjectCapError, ProjectRegistrationError)


def test_persistence_across_instances(tmp_path: Path) -> None:
    """Covers: the registry is durable — a new instance sees prior writes."""
    path = tmp_path / "project-registry.json"
    first = ProjectRegistry(path)
    first.register("alpha-1", "Alpha", _repo(tmp_path), str(tmp_path), active=True)
    second = ProjectRegistry(path)
    assert second.get("alpha-1") == first.get("alpha-1")
    assert [item.project_id for item in second.active_projects()] == ["alpha-1"]


def test_missing_file_reads_as_empty(tmp_path: Path) -> None:
    """Covers: a missing registry file is an empty registry, not an error."""
    registry = ProjectRegistry(tmp_path / "absent.json")
    assert registry.list_projects() == ()
    assert registry.active_projects() == ()
    assert registry.get("anything") is None


def test_corrupt_file_fails_closed(tmp_path: Path) -> None:
    """Covers: a corrupt registry file is refused, never silently ignored."""
    path = tmp_path / "project-registry.json"
    path.write_text("{not json", encoding="utf-8")
    registry = ProjectRegistry(path)
    with pytest.raises(ProjectRegistrationError):
        registry.list_projects()


def test_wrong_shape_fails_closed(tmp_path: Path) -> None:
    """Covers: an unrecognized registry shape is refused."""
    path = tmp_path / "project-registry.json"
    path.write_text(json.dumps({"projects": ["not-a-mapping"]}), encoding="utf-8")
    registry = ProjectRegistry(path)
    with pytest.raises(ProjectRegistrationError):
        registry.list_projects()


def test_key_mismatch_fails_closed(tmp_path: Path) -> None:
    """Covers: a registry key mismatching its record identity is refused."""
    path = tmp_path / "project-registry.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": {
                    "alpha-1": {
                        "project_id": "beta-2",
                        "display_name": "X",
                        "repository_root": "/tmp",
                        "state_root": "/tmp",
                        "active": False,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry = ProjectRegistry(path)
    with pytest.raises(ProjectRegistrationError):
        registry.list_projects()


def test_list_order_is_stable(tmp_path: Path) -> None:
    """Covers: listing order is deterministic (sorted by project ID)."""
    registry = _registry(tmp_path)
    for slug in ("c-project", "a-project", "b-project"):
        registry.register(slug, slug, _repo(tmp_path, slug), str(tmp_path))
    assert [item.project_id for item in registry.list_projects()] == [
        "a-project",
        "b-project",
        "c-project",
    ]


def test_registered_project_record_validation() -> None:
    """Covers: persisted records are re-validated on load (bad rows rejected)."""
    with pytest.raises(ProjectRegistrationError):
        RegisteredProject.from_dict(
            {"project_id": "BAD", "display_name": "X", "repository_root": "/r", "state_root": "/s"}
        )
    with pytest.raises(ProjectRegistrationError):
        RegisteredProject.from_dict("not-a-mapping")


def _cli(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Covers: CLI tests drive the real entry point with an explicit registry."""
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_register_list_active_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: orch project register|list|active end-to-end (explicit identity)."""
    registry = str(tmp_path / "registry.json")
    repo = _repo(tmp_path)
    code, out, _ = _cli(
        "project",
        "register",
        "--id",
        "alpha-1",
        "--name",
        "Alpha",
        "--repo",
        repo,
        "--state-dir",
        str(tmp_path),
        "--registry",
        registry,
        "--json",
        capsys=capsys,
    )
    assert code == 0
    assert json.loads(out)["project_id"] == "alpha-1"

    code, out, _ = _cli("project", "list", "--registry", registry, "--json", capsys=capsys)
    assert code == 0
    assert [item["project_id"] for item in json.loads(out)["projects"]] == ["alpha-1"]

    code, out, _ = _cli("project", "active", "--registry", registry, "--json", capsys=capsys)
    assert code == 0
    assert json.loads(out)["projects"] == []

    code, out, _ = _cli(
        "project", "activate", "--id", "alpha-1", "--registry", registry, capsys=capsys
    )
    assert code == 0
    code, out, _ = _cli("project", "active", "--registry", registry, "--json", capsys=capsys)
    assert code == 0
    assert [item["project_id"] for item in json.loads(out)["projects"]] == ["alpha-1"]


def test_cli_register_malformed_slug_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: CLI registration refuses malformed slugs with exit 1."""
    code, _, err = _cli(
        "project",
        "register",
        "--id",
        "BAD SLUG",
        "--name",
        "X",
        "--repo",
        _repo(tmp_path),
        "--state-dir",
        str(tmp_path),
        "--registry",
        str(tmp_path / "registry.json"),
        capsys=capsys,
    )
    assert code == 1
    assert "malformed" in err


def test_cli_cap_enforced(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Covers: CLI activation refuses the fourth active project."""
    registry = str(tmp_path / "registry.json")
    for index in range(3):
        code, _, _ = _cli(
            "project",
            "register",
            "--id",
            f"p-{index}",
            "--name",
            f"P {index}",
            "--repo",
            _repo(tmp_path, f"repo-{index}"),
            "--state-dir",
            str(tmp_path),
            "--registry",
            registry,
            "--active",
            capsys=capsys,
        )
        assert code == 0
    code, _, err = _cli(
        "project",
        "register",
        "--id",
        "p-3",
        "--name",
        "P 3",
        "--repo",
        _repo(tmp_path, "repo-3"),
        "--state-dir",
        str(tmp_path),
        "--registry",
        registry,
        "--active",
        capsys=capsys,
    )
    assert code == 1
    assert "cap" in err
