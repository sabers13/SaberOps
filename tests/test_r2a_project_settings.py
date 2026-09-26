"""R2-A characterization: project settings + shared run-creation authority.

Covers the required cases without any real provider inference (run
policy is resolved and frozen, but no supervisor/worker is ever
launched):

A. Fresh project, no gate: Web and CLI fail before dispatch with the
   same actionable gate-required semantics.
B. Fresh private/unknown project, no data policy: frozen policy Denied.
C. Explicit settings survive reload from the project state directory.
D. Web/CLI parity: same settings + equivalent request freeze identical
   policy portions.
E. Immutability: an existing frozen Run keeps its values after project
   settings change; the next Run receives the new values.
F. Review-policy and gate-scratch reads/writes use the canonical
   existing persistence (no divergent copies).
G. Target branch: explicit branch frozen; absent branch resolves and
   freezes the current symbolic branch without altering acceptance.
H. No silent ``make gate``: normal creation cannot obtain it merely
   because no gate was specified.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.cli import _resolve_cli_run_config, build_parser
from saberops.config import RunConfig, reconstruct_run_config
from saberops.gate_runner import load_project_gate_scratch_policy
from saberops.models import GateScratchMode, ReviewPolicyMode
from saberops.project import bind_project_state_dir, compute_project_identity
from saberops.project_settings import (
    DataPolicy,
    load_project_settings,
    project_settings_path,
    set_project_accept_target_branch,
    set_project_data_policy,
    set_project_gate_command,
)
from saberops.review_adaptive import (
    load_project_review_policy,
    project_review_policy_path,
    set_project_review_policy_for_state_dir,
)
from saberops.run_creation import (
    GATE_REQUIRED_MESSAGE,
    RunCreationError,
    RunRequest,
    resolve_effective_run_config,
)
from saberops.web import create_app


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "xdg-state"
    config = tmp_path / "xdg-config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    return tmp_path


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_repo(base: Path, name: str = "proj") -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "r2a@example.com")
    _git(repo, "config", "user.name", "R2-A Probe")
    (repo / "README.md").write_text("# probe\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def _state_dir(repo: Path) -> Path:
    return bind_project_state_dir(compute_project_identity(repo))


def _cli_args(repo: Path, *extra: str) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(["run", "--repo", str(repo), "--task", "probe task", *extra])


# ---------------------------------------------------------------------------
# A. Fresh project with no explicit gate fails before dispatch (Web + CLI).
# ---------------------------------------------------------------------------


def test_r2a_a_cli_fails_without_gate(isolated_xdg: Path) -> None:
    repo = _make_repo(isolated_xdg, "a-cli")
    args = _cli_args(repo)
    assert args.gate is None
    assert args.training_allowed is None
    with pytest.raises(RunCreationError, match="no gate configured"):
        _resolve_cli_run_config(args, str(repo))


def test_r2a_a_web_fails_without_gate(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _make_repo(isolated_xdg, "a-web")
    client = TestClient(
        create_app(db_path=tmp_path / "web.db", repo_path=repo),
        raise_server_exceptions=False,
    )
    response = client.post("/runs", data={"task": "probe task"})
    assert response.status_code == 400
    assert "no gate configured" in response.text
    assert "never runs an implicit default gate" in response.text


def test_r2a_a_same_gate_required_semantics(isolated_xdg: Path, tmp_path: Path) -> None:
    """Web and CLI surface the same actionable gate-required message."""
    repo = _make_repo(isolated_xdg, "a-both")
    try:
        _resolve_cli_run_config(_cli_args(repo), str(repo))
        cli_message = ""
    except RunCreationError as exc:
        cli_message = str(exc)
    assert cli_message == GATE_REQUIRED_MESSAGE
    client = TestClient(
        create_app(db_path=tmp_path / "web.db", repo_path=repo),
        raise_server_exceptions=False,
    )
    response = client.post("/runs", data={"task": "probe task"})
    assert response.status_code == 400
    assert GATE_REQUIRED_MESSAGE.split(".")[0] in response.text


# ---------------------------------------------------------------------------
# B. Fresh private/unknown project with no data-policy setting is Denied.
# ---------------------------------------------------------------------------


def test_r2a_b_unset_data_policy_denied(isolated_xdg: Path) -> None:
    repo = _make_repo(isolated_xdg, "b-fresh")
    (repo / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    settings = load_project_settings(repo)
    assert settings.data_policy is None
    config = resolve_effective_run_config(
        RunRequest(task="probe task", repo_path=repo, training_allowed=None)
    )
    assert config.training_allowed is False


def test_r2a_b_explicit_allowed_survives(isolated_xdg: Path) -> None:
    repo = _make_repo(isolated_xdg, "b-allowed")
    (repo / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    set_project_data_policy(repo, DataPolicy.ALLOWED)
    config = resolve_effective_run_config(
        RunRequest(task="probe task", repo_path=repo, training_allowed=None)
    )
    assert config.training_allowed is True


# ---------------------------------------------------------------------------
# C. Explicit settings survive reload from the project state directory.
# ---------------------------------------------------------------------------


def test_r2a_c_settings_survive_reload(isolated_xdg: Path) -> None:
    from saberops.gate_runner import set_project_gate_scratch_policy

    repo = _make_repo(isolated_xdg, "c-settings")
    set_project_gate_command(repo, "pytest -q")
    set_project_data_policy(repo, "allowed")
    set_project_accept_target_branch(repo, "main")
    set_project_review_policy_for_state_dir(_state_dir(repo), ReviewPolicyMode.ALWAYS_REQUIRED)
    set_project_gate_scratch_policy(repo, GateScratchMode.RAM_PREFERRED)

    reloaded = load_project_settings(repo)
    assert reloaded.gate_command == "pytest -q"
    assert reloaded.data_policy is DataPolicy.ALLOWED
    assert reloaded.accept_target_branch == "main"
    assert load_project_review_policy(repo) is ReviewPolicyMode.ALWAYS_REQUIRED
    assert load_project_gate_scratch_policy(repo) is GateScratchMode.RAM_PREFERRED


# ---------------------------------------------------------------------------
# D. Web/CLI parity on the frozen policy portion.
# ---------------------------------------------------------------------------


def _policy_portion(config: RunConfig) -> tuple[object, ...]:
    return (
        config.gate_command,
        config.gate_scratch_mode,
        config.review_policy_mode,
        config.training_allowed,
        config.accept_target_branch,
    )


def test_r2a_d_web_cli_parity(isolated_xdg: Path) -> None:
    from saberops.gate_runner import set_project_gate_scratch_policy

    repo = _make_repo(isolated_xdg, "d-parity")
    set_project_gate_command(repo, "pytest -q")
    set_project_data_policy(repo, DataPolicy.DENIED)
    set_project_accept_target_branch(repo, "main")
    set_project_review_policy_for_state_dir(_state_dir(repo), ReviewPolicyMode.ALWAYS_REQUIRED)
    set_project_gate_scratch_policy(repo, GateScratchMode.RAM_PREFERRED)

    # CLI equivalent request: explicit per-run gate override + explicit
    # training-denied, project-driven review policy.
    cli_config = _resolve_cli_run_config(
        _cli_args(repo, "--gate", "pytest -q -x", "--training-denied"), str(repo)
    )
    # Web equivalent request: the same explicit gate box + Denied select,
    # no per-run review-policy knob (None = follow project).
    web_config = resolve_effective_run_config(
        RunRequest(
            task="probe task",
            repo_path=repo,
            gate_command="pytest -q -x",
            training_allowed=False,
            review_policy=None,
        )
    )
    assert _policy_portion(cli_config) == _policy_portion(web_config)
    assert web_config.gate_command == "pytest -q -x"
    assert web_config.training_allowed is False
    assert web_config.review_policy_mode is ReviewPolicyMode.ALWAYS_REQUIRED
    assert web_config.gate_scratch_mode is GateScratchMode.RAM_PREFERRED
    assert web_config.accept_target_branch == "main"


def test_r2a_d_defaults_parity(isolated_xdg: Path) -> None:
    """Project-following requests (no per-run overrides) also agree."""
    repo = _make_repo(isolated_xdg, "d-defaults")
    set_project_gate_command(repo, "pytest -q")
    set_project_data_policy(repo, DataPolicy.ALLOWED)
    cli_config = _resolve_cli_run_config(_cli_args(repo), str(repo))
    web_config = resolve_effective_run_config(
        RunRequest(task="probe task", repo_path=repo)
    )
    # CLI training default is None (follow project); Web passes the
    # dashboard-overlaid explicit project value: both freeze Allowed.
    web_explicit = resolve_effective_run_config(
        RunRequest(task="probe task", repo_path=repo, training_allowed=True)
    )
    assert cli_config.training_allowed is True
    assert web_config.training_allowed is True
    assert _policy_portion(cli_config) == _policy_portion(web_explicit)


# ---------------------------------------------------------------------------
# E. Frozen runs are immutable; later settings affect only future runs.
# ---------------------------------------------------------------------------


def test_r2a_e_old_run_immutable(isolated_xdg: Path) -> None:
    repo = _make_repo(isolated_xdg, "e-immutable")
    set_project_gate_command(repo, "pytest -q")
    set_project_data_policy(repo, DataPolicy.DENIED)
    set_project_accept_target_branch(repo, "main")

    first = _resolve_cli_run_config(_cli_args(repo), str(repo))
    frozen_payload = first.as_dict()

    set_project_gate_command(repo, "make gate-ci")
    set_project_data_policy(repo, DataPolicy.ALLOWED)
    set_project_accept_target_branch(repo, "release")

    # The already-frozen run is reconstructed from durable evidence
    # WITHOUT rereading mutable project settings.
    revived = reconstruct_run_config(json.loads(json.dumps(frozen_payload)))
    assert revived.gate_command == "pytest -q"
    assert revived.training_allowed is False
    assert revived.accept_target_branch == "main"

    second = _resolve_cli_run_config(_cli_args(repo), str(repo))
    assert second.gate_command == "make gate-ci"
    assert second.training_allowed is True
    assert second.accept_target_branch == "release"


# ---------------------------------------------------------------------------
# F. Canonical review-policy and gate-scratch authorities are composed.
# ---------------------------------------------------------------------------


def test_r2a_f_canonical_authorities(isolated_xdg: Path) -> None:
    from saberops.project_settings import (
        set_project_gate_scratch_mode,
        set_project_review_policy,
    )

    repo = _make_repo(isolated_xdg, "f-canonical")
    state_dir = _state_dir(repo)
    set_project_review_policy(repo, ReviewPolicyMode.ALWAYS_REQUIRED)
    set_project_gate_scratch_mode(repo, GateScratchMode.RAM_REQUIRED)
    set_project_gate_command(repo, "pytest -q")

    assert load_project_review_policy(repo) is ReviewPolicyMode.ALWAYS_REQUIRED
    assert load_project_gate_scratch_policy(repo) is GateScratchMode.RAM_REQUIRED
    # Canonical files exist; the composed document holds only the new
    # settings (never a copied review/scratch value).
    assert project_review_policy_path(state_dir).is_file()
    assert (state_dir / "gate-runner-policy.json").is_file()
    raw = json.loads(project_settings_path(state_dir).read_text(encoding="utf-8"))
    assert set(raw) <= {"version", "gate_command", "data_policy", "accept_target_branch"}
    assert "mode" not in raw


# ---------------------------------------------------------------------------
# G. Target branch freeze semantics.
# ---------------------------------------------------------------------------


def test_r2a_g_explicit_branch_frozen(isolated_xdg: Path) -> None:
    repo = _make_repo(isolated_xdg, "g-explicit")
    set_project_gate_command(repo, "pytest -q")
    set_project_accept_target_branch(repo, "release")
    config = resolve_effective_run_config(RunRequest(task="t", repo_path=repo))
    assert config.accept_target_branch == "release"


def test_r2a_g_live_branch_frozen_when_unset(isolated_xdg: Path) -> None:
    from saberops.git import GitManager

    repo = _make_repo(isolated_xdg, "g-live")
    set_project_gate_command(repo, "pytest -q")
    live = GitManager.get_current_branch(repo)
    assert live
    config = resolve_effective_run_config(RunRequest(task="t", repo_path=repo))
    assert config.accept_target_branch == live


def test_r2a_g_detached_head_requires_explicit_branch(isolated_xdg: Path) -> None:
    repo = _make_repo(isolated_xdg, "g-detached")
    set_project_gate_command(repo, "pytest -q")
    _git(repo, "checkout", "--detach", "HEAD")
    with pytest.raises(RunCreationError, match="target branch"):
        resolve_effective_run_config(RunRequest(task="t", repo_path=repo))
    set_project_accept_target_branch(repo, "main")
    config = resolve_effective_run_config(RunRequest(task="t", repo_path=repo))
    assert config.accept_target_branch == "main"


# ---------------------------------------------------------------------------
# H. No silent ``make gate``.
# ---------------------------------------------------------------------------


def test_r2a_h_no_silent_make_gate(isolated_xdg: Path) -> None:
    from saberops.run_creation import resolve_effective_gate_command

    repo = _make_repo(isolated_xdg, "h-silent")
    assert (repo / "Makefile").exists() is False
    with pytest.raises(RunCreationError, match="no gate configured"):
        resolve_effective_gate_command(repo_path=repo, explicit_gate=None)
    with pytest.raises(RunCreationError, match="no gate configured"):
        resolve_effective_gate_command(repo_path=repo, explicit_gate="   ")


def test_r2a_h_makefile_without_gate_target_still_fails(isolated_xdg: Path) -> None:
    from saberops.run_creation import resolve_effective_gate_command

    repo = _make_repo(isolated_xdg, "h-nogate-target")
    (repo / "Makefile").write_text("test:\n\t@echo ok\n", encoding="utf-8")
    with pytest.raises(RunCreationError, match="no gate configured"):
        resolve_effective_gate_command(repo_path=repo, explicit_gate=None)


def test_r2a_h_positive_detection_only_with_gate_target(isolated_xdg: Path) -> None:
    from saberops.run_creation import resolve_effective_gate_command

    repo = _make_repo(isolated_xdg, "h-detected")
    (repo / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    assert resolve_effective_gate_command(repo_path=repo, explicit_gate=None) == "make gate"
    # Explicit project configuration still wins over detection.
    set_project_gate_command(repo, "pytest -q")
    assert (
        resolve_effective_gate_command(repo_path=repo, explicit_gate=None) == "pytest -q"
    )
    # An explicit per-run gate wins over everything.
    assert (
        resolve_effective_gate_command(repo_path=repo, explicit_gate="true") == "true"
    )


# ---------------------------------------------------------------------------
# I. R2-A.1: one Run is composed from one project-settings snapshot.
# ---------------------------------------------------------------------------


def test_r2a_i_single_project_settings_snapshot(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2-A.1 negative control: exactly one project-settings load per Run.

    Deterministic (no timing/threads): the ``load_project_settings`` seam
    returns snapshot A on the first call and snapshot B afterwards.  A
    correct implementation loads once and freezes A verbatim; the old
    multi-read resolver loads three times and freezes a hybrid that
    never existed as one owner decision.
    """
    import saberops.run_creation as run_creation
    from saberops.project_settings import ProjectSettings
    from saberops.run_creation import RunRequest, resolve_effective_run_config

    repo = _make_repo(isolated_xdg, "i-snapshot")
    snapshot_a = ProjectSettings(
        gate_command="gate-A",
        data_policy=DataPolicy.DENIED,
        accept_target_branch="main",
    )
    snapshot_b = ProjectSettings(
        gate_command="gate-B",
        data_policy=DataPolicy.ALLOWED,
        accept_target_branch="release",
    )
    calls = {"count": 0}

    def _flapping_settings(repo_path: Path | str) -> ProjectSettings:
        calls["count"] += 1
        if calls["count"] == 1:
            return snapshot_a
        return snapshot_b

    monkeypatch.setattr(run_creation, "load_project_settings", _flapping_settings)

    config = resolve_effective_run_config(
        RunRequest(
            task="probe",
            repo_path=repo,
            gate_command=None,
            training_allowed=None,
            accept_target_branch=None,
        )
    )

    assert calls["count"] == 1
    assert config.gate_command == "gate-A"
    assert config.training_allowed is False
    assert config.accept_target_branch == "main"
