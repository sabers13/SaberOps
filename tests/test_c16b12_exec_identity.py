"""C16-B1.2 self-hosting executable-identity correction.

The detached/supervised continuation must re-execute the same
Python/SaberOps environment that launched it
(``[sys.executable, "-m", "saberops.cli", ...]``), never an arbitrary
``orch`` discovered from ``$PATH``.  ``ORCH_EXECUTABLE`` remains the only
explicit override.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from saberops.config import RunConfig
from saberops.db import Database
from saberops.models import Run, RunStatus, Tier
from saberops.supervisor import RunSupervisor
from saberops.web import resolve_orch_executable, resolve_owner_argv_prefix


def _config_for(repo: Path) -> RunConfig:
    return RunConfig(task="docs-only probe", target_repo=str(repo))


def _seed_pending_run(db: Database, repo: Path, run_id: str) -> None:
    db.create_run(
        Run(
            id=run_id,
            task="docs-only probe",
            target_repo=str(repo),
            base_commit="abc123",
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.PENDING,
            created_at="2026-01-01T00:00:00",
        )
    )


class _FakePopen:
    instances: list[_FakePopen] = []

    def __init__(self, args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.pid = 424242
        _FakePopen.instances.append(self)

    def poll(self) -> None:
        return None


def test_default_prefix_uses_current_python_not_path_orch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ORCH_EXECUTABLE the continuation reuses the current interpreter."""
    monkeypatch.delenv("ORCH_EXECUTABLE", raising=False)
    # Even if PATH resolution would find an orch, the default must not use it.
    monkeypatch.setattr("shutil.which", lambda _name: "/tmp/stale-global/orch")
    prefix = resolve_owner_argv_prefix()
    assert prefix == [sys.executable, "-m", "saberops.cli"]
    assert prefix[0] != "/tmp/stale-global/orch"
    assert "orch" not in prefix[0] or prefix[0] == sys.executable

    db = Database(tmp_path / "owner.db")
    supervisor = RunSupervisor(db=db)
    cli_args = supervisor._build_cli_args("run_default", _config_for(tmp_path))
    recovery_args = supervisor._build_recovery_cli_args("run_default", _config_for(tmp_path))
    assert cli_args[:3] == [sys.executable, "-m", "saberops.cli"]
    assert cli_args[3] == "run"
    assert recovery_args[:3] == [sys.executable, "-m", "saberops.cli"]
    assert recovery_args[3] == "recover"
    assert "/tmp/stale-global/orch" not in cli_args
    assert "/tmp/stale-global/orch" not in recovery_args


def test_stale_orch_earlier_on_path_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken/stale `orch` earlier on PATH never becomes the owner argv."""
    stale_dir = tmp_path / "stale-bin"
    stale_dir.mkdir()
    stale_orch = stale_dir / "orch"
    stale_orch.write_text(
        "#!/bin/sh\nexec python3 -c 'import orchestrator_mvp.cli'\n", encoding="utf-8"
    )
    stale_orch.chmod(stale_orch.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(stale_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("ORCH_EXECUTABLE", raising=False)

    prefix = resolve_owner_argv_prefix()
    assert prefix == [sys.executable, "-m", "saberops.cli"]
    assert str(stale_orch) not in prefix

    db = Database(tmp_path / "stale.db")
    supervisor = RunSupervisor(db=db)
    cli_args = supervisor._build_cli_args("run_stale", _config_for(tmp_path))
    assert cli_args[0] == sys.executable
    assert str(stale_orch) not in cli_args
    assert "orchestrator_mvp" not in " ".join(cli_args)


def test_explicit_orch_executable_still_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ORCH_EXECUTABLE remains the explicit owner/developer override."""
    monkeypatch.setenv("ORCH_EXECUTABLE", "/tmp/custom-orch")
    assert resolve_owner_argv_prefix() == ["/tmp/custom-orch"]
    assert resolve_orch_executable() == "/tmp/custom-orch"
    db = Database(tmp_path / "override.db")
    supervisor = RunSupervisor(db=db)
    cli_args = supervisor._build_cli_args("run_override", _config_for(tmp_path))
    recovery_args = supervisor._build_recovery_cli_args("run_override", _config_for(tmp_path))
    assert cli_args[0] == "/tmp/custom-orch"
    assert cli_args[1] == "run"
    assert recovery_args[0] == "/tmp/custom-orch"
    assert recovery_args[1] == "recover"


def test_owner_argv_remains_shell_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Owner launch stays argv-based: list args, no shell=True."""
    monkeypatch.delenv("ORCH_EXECUTABLE", raising=False)
    _FakePopen.instances.clear()
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    repo = tmp_path / "repo"
    repo.mkdir()
    db = Database(tmp_path / "shell.db")
    _seed_pending_run(db, repo, "run_shellfree")
    supervisor = RunSupervisor(db=db)
    assert supervisor._spawn_owner("run_shellfree", _config_for(repo)) is True
    assert len(_FakePopen.instances) == 1
    spawned = _FakePopen.instances[0]
    assert isinstance(spawned.args, list)
    assert all(isinstance(part, str) for part in spawned.args)
    assert spawned.args[:3] == [sys.executable, "-m", "saberops.cli"]
    assert spawned.kwargs.get("shell", False) is False


def test_detached_launch_reaches_execution_with_stale_orch_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Detached launch claims the owner and never leaves PENDING-via-failure."""
    stale_dir = tmp_path / "stale-bin2"
    stale_dir.mkdir()
    stale_orch = stale_dir / "orch"
    stale_orch.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    stale_orch.chmod(stale_orch.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(stale_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("ORCH_EXECUTABLE", raising=False)
    _FakePopen.instances.clear()
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    repo = tmp_path / "repo2"
    repo.mkdir()
    db = Database(tmp_path / "detached.db")
    _seed_pending_run(db, repo, "run_detached")
    supervisor = RunSupervisor(db=db)
    assert supervisor._spawn_owner("run_detached", _config_for(repo)) is True

    spawned = _FakePopen.instances[0]
    assert spawned.args[0] == sys.executable
    assert str(stale_orch) not in spawned.args

    owner = db.get_execution_owner("run_detached")
    assert owner is not None
    assert owner.is_active
    run = db.get_run("run_detached")
    assert run is not None
    assert run.status == RunStatus.PENDING
    assert run.result_summary is None


def test_installed_package_module_entry_not_regressed() -> None:
    """The default identity names an importable module entry point."""
    import importlib.util

    assert importlib.util.find_spec("saberops.cli") is not None
    prefix = resolve_owner_argv_prefix()
    assert prefix == [sys.executable, "-m", "saberops.cli"]
    display = resolve_orch_executable()
    assert sys.executable in display
    assert "saberops.cli" in display
    completed = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
