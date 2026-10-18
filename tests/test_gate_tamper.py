"""Adversarial gate-protection tests: a candidate cannot change its own gate (GT-1).

Every test builds a throwaway git repository that already contains a full set
of gate definitions at its base commit, then lets a candidate tamper with one
of them and asserts the engine refuses to run the gate at all.  No test starts
a provider/model call and none touches the real checkout.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

from saberops.gate_history import read_entries
from saberops.gate_profiles import GateCommand, GateProfile, GateProfileName
from saberops.gate_runner import (
    EngineGateExecutor,
    EngineGateRequest,
    EngineGateResult,
    load_base_gate_definitions,
    verify_gate_definitions,
)
from saberops.gate_scope import GATE_PROTECTED_PATH_MUTATED

#: Stub gate command: writes the profile's outcome.json, exactly like a real
#: gate, so a tampered candidate would otherwise be able to "pass" itself.
WRITER = '''\
import json, os, pathlib, sys
out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
payload = {
    "outcome": "PASS",
    "reason": "",
    "counts": {"tests": 3, "failed": 0, "flaky": 0},
    "failed": [],
    "flaky": [],
    "checks_ok": True,
    "selected": True,
}
(out / "outcome.json").write_text(json.dumps(payload))
sys.exit(0)
'''

#: The gate definitions the base commit ships, and their trusted bytes.
BASE_SCRIPTS = {
    "scripts/gate-full.sh": (
        "#!/usr/bin/env bash\nset -euo pipefail\nexec python scripts/gate_tools.py full\n"
    ),
    "scripts/gate_tools.py": "import sys\n\nprint('gate')\nsys.exit(0)\n",
}
BASE_PYPROJECT = (
    "[tool.mypy]\nstrict = true\n\n[tool.pytest.ini_options]\ntimeout = 180\n"
)
BASE_CI = (
    "name: ci\non: [push]\njobs:\n  gate:\n    steps:\n"
    "      - run: bash scripts/gate-full.sh\n"
)
BASE_CONFTEST = "import pytest\n"


class _FakePool:
    """Slot pool that records which slot class a gate asked for."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @contextlib.contextmanager
    def heavy(self, *, wait: float | None = None) -> Iterator[None]:
        self.calls.append("heavy")
        yield

    @contextlib.contextmanager
    def light(self, *, wait: float | None = None) -> Iterator[None]:
        self.calls.append("light")
        yield


def _git(repo: Path, *args: str) -> str:
    """Covers: helper (temporary git repo only)."""
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _init_repo(path: Path) -> Path:
    """Covers: helper (temporary git repo with every gate definition at base)."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "T")
    (path / "scripts").mkdir(exist_ok=True)
    (path / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (path / "tests").mkdir(exist_ok=True)
    for name, text in BASE_SCRIPTS.items():
        (path / name).write_text(text)
    (path / "pyproject.toml").write_text(BASE_PYPROJECT)
    (path / ".github" / "workflows" / "ci.yml").write_text(BASE_CI)
    (path / "conftest.py").write_text(BASE_CONFTEST)
    (path / "tests" / "conftest.py").write_text(BASE_CONFTEST)
    (path / "tests" / "quarantine.txt").write_text("")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    return path


def _head(repo: Path) -> str:
    """Covers: helper (base SHA of the throwaway repository)."""
    return _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, message: str) -> str:
    """Covers: helper (commit a candidate change in the throwaway repository)."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _head(repo)


def _stub_profile(repo: Path, name: GateProfileName) -> GateProfile:
    """Covers: helper (stub gate profile, no real gate script involved)."""
    script = repo / "writer.py"
    script.write_text(WRITER)
    return GateProfile(
        name=name,
        commands=(GateCommand("stub", f"{{python}} {script} {{result_dir}}"),),
        wall_budget_seconds=60,
    )


def _request(
    repo: Path, base: str, kind: str, evidence_dir: Path, **kwargs: object
) -> EngineGateRequest:
    """Covers: helper (engine request with the stub profile and a real base)."""
    profiles = {
        GateProfileName.FAST: _stub_profile(repo, GateProfileName.FAST),
        GateProfileName.FULL: _stub_profile(repo, GateProfileName.FULL),
    }
    state_dir = repo / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "gate-profiles.json").write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    name.value: {
                        "commands": [
                            {"name": c.name, "command": c.command} for c in prof.commands
                        ],
                        "wall_budget_seconds": prof.wall_budget_seconds,
                        "rerun_failures": prof.rerun_failures,
                        "require_map_freshness": prof.require_map_freshness,
                    }
                    for name, prof in profiles.items()
                },
            }
        )
    )
    return EngineGateRequest(
        target_repo=repo,
        kind=kind,
        evidence_dir=evidence_dir,
        state_dir=state_dir,
        base=base,
        **kwargs,  # type: ignore[arg-type]
    )


def _executor() -> EngineGateExecutor:
    """Covers: helper (executor with a fake slot pool and no disk guard)."""
    return EngineGateExecutor(
        slot_pool=_FakePool(), disk_check=lambda repo, *, estimate_bytes=0: None
    )


def _assert_blocked(result: object, evidence_dir: Path, path: str) -> None:
    """Covers: helper (the fail-closed block shape shared by the tamper tests)."""
    assert isinstance(result, EngineGateResult)
    assert result.outcome == "BLOCKED"
    assert result.accepted is False
    assert "GATE_PROTECTION_BLOCKED" in result.reason
    assert GATE_PROTECTED_PATH_MUTATED in result.reason
    assert result.protection is not None
    assert result.protection.is_protected is True
    assert result.protection.allowed is False
    assert path in result.protection.tampered_paths
    assert result.protection.owner_question
    assert read_entries(result.history_path)[0]["outcome"] == "BLOCKED"
    # Fail closed: the gate command never ran, so it produced no artifact.
    assert not list(evidence_dir.rglob("outcome.json"))


def test_gate_tamper_conftest_cannot_change_its_own_verdict(tmp_path: Path) -> None:
    """Covers: GT-1 exit (a candidate conftest edit cannot change its own G1/G3)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "tests" / "conftest.py").write_text(
        "import pytest\n\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def skip_everything():\n"
        "    pytest.skip('tampered')\n"
    )
    _commit(repo, "tamper conftest")
    evidence = tmp_path / "evidence"
    result = _executor().execute(_request(repo, base, "G1", evidence))
    _assert_blocked(result, evidence, "tests/conftest.py")
    assert result.protection is not None
    assert dict(result.protection.digests)["tests/conftest.py"]


def test_gate_tamper_gate_script_weakening_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (weakening gate-full.sh cannot buy a passing verdict)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "scripts" / "gate-full.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    _commit(repo, "weaken the full gate")
    evidence = tmp_path / "evidence"
    result = _executor().execute(_request(repo, base, "G3", evidence))
    _assert_blocked(result, evidence, "scripts/gate-full.sh")


def test_gate_tamper_gate_tools_bypass_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (a gate_tools bypass is caught even without a commit)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "scripts" / "gate_tools.py").write_text("import sys\n\nsys.exit(0)\n")
    evidence = tmp_path / "evidence"
    result = _executor().execute(_request(repo, base, "G1", evidence))
    _assert_blocked(result, evidence, "scripts/gate_tools.py")
    assert result.protection is not None
    assert "base-content" in result.protection.detected_by


def test_gate_tamper_pyproject_mypy_relaxation_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (disabling strict mypy in pyproject.toml is blocked)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "pyproject.toml").write_text(
        "[tool.mypy]\nstrict = false\n\n[tool.pytest.ini_options]\ntimeout = 180\n"
    )
    _commit(repo, "relax the type gate")
    evidence = tmp_path / "evidence"
    result = _executor().execute(_request(repo, base, "G1", evidence))
    _assert_blocked(result, evidence, "pyproject.toml")


def test_gate_tamper_ci_workflow_change_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (a candidate cannot rewrite the CI gate it is judged by)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "name: ci\non: [push]\njobs: {}\n"
    )
    _commit(repo, "drop the CI gate")
    evidence = tmp_path / "evidence"
    result = _executor().execute(_request(repo, base, "G1", evidence))
    _assert_blocked(result, evidence, ".github/workflows/ci.yml")


def test_gate_tamper_untracked_subdirectory_conftest_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (an added conftest in any subdirectory is a definition)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    nested = repo / "tests" / "nested"
    nested.mkdir()
    (nested / "conftest.py").write_text(
        "import pytest\n\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def skip_everything():\n"
        "    pytest.skip('tampered')\n"
    )
    evidence = tmp_path / "evidence"
    result = _executor().execute(_request(repo, base, "G1", evidence))
    _assert_blocked(result, evidence, "tests/nested/conftest.py")
    assert result.protection is not None
    assert "base-content" in result.protection.detected_by


def test_gate_tamper_declared_gate_edit_is_still_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (a declared Touches entry is evidence, not authorization)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "scripts" / "gate-full.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    _commit(repo, "declared but unauthorized gate change")
    evidence = tmp_path / "evidence"
    result = _executor().execute(
        _request(repo, base, "G1", evidence, touches=("scripts/gate-full.sh",))
    )
    _assert_blocked(result, evidence, "scripts/gate-full.sh")
    assert result.protection is not None
    assert result.protection.declared_paths == ("scripts/gate-full.sh",)
    assert any("not an authorization" in finding for finding in result.findings)


def test_gate_tamper_clean_candidate_still_passes(tmp_path: Path) -> None:
    """Covers: GT-1 (an untouched gate definition set verifies and the gate runs)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "src").mkdir()
    (repo / "src" / "thing.py").write_text("VALUE = 1\n")
    _commit(repo, "authorized product change")
    evidence = tmp_path / "evidence"
    result = _executor().execute(
        _request(repo, base, "G1", evidence, touches=("src/",))
    )
    assert result.outcome == "PASS"
    assert result.accepted is True
    assert result.protection is not None
    assert result.protection.is_protected is False
    assert result.protection.base_verified is True
    assert result.protection.base_sha == base
    assert list(evidence.rglob("outcome.json"))


def test_gate_tamper_verdict_is_recorded_in_engine_outcome(tmp_path: Path) -> None:
    """Covers: GT-1 (the protection verdict is durable engine evidence)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "conftest.py").write_text("import pytest\npytest.skip('tampered')\n")
    _commit(repo, "tamper the root conftest")
    evidence = tmp_path / "evidence"
    result = _executor().execute(_request(repo, base, "G1", evidence))
    _assert_blocked(result, evidence, "conftest.py")
    payload = json.loads(Path(result.outcome_path).read_text())
    assert payload["outcome"] == "BLOCKED"
    assert payload["gate_protection"]["is_protected"] is True
    assert payload["gate_protection"]["tampered_paths"] == ["conftest.py"]
    assert payload["gate_protection"]["base_sha"] == base


def test_gate_tamper_without_a_declared_diff_is_not_applicable(tmp_path: Path) -> None:
    """Covers: GT-1 (no base and no changed files means no candidate diff to guard)."""
    repo = _init_repo(tmp_path / "repo")
    evidence = tmp_path / "evidence"
    result = _executor().execute(_request(repo, "", "G3", evidence))
    assert result.outcome == "PASS"
    assert result.protection is None


def test_gate_tamper_unverifiable_base_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (a base that cannot be read blocks instead of passing)."""
    repo = _init_repo(tmp_path / "repo")
    evidence = tmp_path / "evidence"
    result = _executor().execute(
        _request(repo, "0" * 40, "G1", evidence, changed_files=("src/thing.py",))
    )
    assert result.outcome == "BLOCKED"
    assert result.accepted is False
    assert "GATE_PROTECTION_BLOCKED" in result.reason
    assert "GATE_BASE_UNVERIFIABLE" in result.reason
    assert not list(evidence.rglob("outcome.json"))


def test_load_base_gate_definitions_reads_the_base_sha(tmp_path: Path) -> None:
    """Covers: GT-1 (definitions are loaded from the base SHA, not the candidate)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    loaded = load_base_gate_definitions(repo, base)
    assert loaded["scripts/gate-full.sh"] == BASE_SCRIPTS["scripts/gate-full.sh"]
    assert loaded["scripts/gate_tools.py"] == BASE_SCRIPTS["scripts/gate_tools.py"]
    assert loaded["pyproject.toml"] == BASE_PYPROJECT
    assert loaded["conftest.py"] == BASE_CONFTEST
    assert loaded["tests/conftest.py"] == BASE_CONFTEST
    assert loaded[".github/workflows/ci.yml"] == BASE_CI
    # A candidate edit is invisible to the base load, which is the point.
    (repo / "scripts" / "gate-full.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    assert load_base_gate_definitions(repo, base)["scripts/gate-full.sh"] == (
        BASE_SCRIPTS["scripts/gate-full.sh"]
    )
    assert (
        verify_gate_definitions(repo, base, changed_paths=("scripts/gate-full.sh",))
        .tampered_paths
        == ("scripts/gate-full.sh",)
    )
