"""Adversarial gate-protection tests: a candidate cannot change its own gate (GT-1).

Every test builds a throwaway git repository that already contains a full set
of gate definitions at its base commit, then lets a candidate tamper with one
of them and asserts the engine refuses to run the gate at all.  No test starts
a provider/model call and none touches the real checkout.

H32 moved the check onto the real paths: ``_run_configured_gate`` verifies
before its subprocess, and the acceptance ``GateRunner`` verifies before its
own.  These tests exercise the same shared ``verify_gate_definitions`` seam.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from saberops.config import RunConfig
from saberops.gate_runner import (
    GateProtectionBlockedError,
    GateRunner,
    GateRunnerRequest,
    load_base_gate_definitions,
    verify_gate_definitions,
)
from saberops.gate_scope import GATE_PROTECTED_PATH_MUTATED
from saberops.models import GateScratchMode
from saberops.orchestrator import _run_configured_gate

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
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)
    return _head(repo)


def _assert_blocked(
    repo: Path, base: str, path: str, *, touches: tuple[str, ...] = ()
) -> None:
    """Covers: helper (the fail-closed block shape shared by the tamper tests)."""
    protection = verify_gate_definitions(repo, base, touches=touches)
    assert protection.is_protected is True
    assert protection.allowed is False
    assert path in protection.tampered_paths
    assert protection.reason_code == GATE_PROTECTED_PATH_MUTATED
    assert protection.owner_question


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
    _assert_blocked(repo, base, "tests/conftest.py")
    protection = verify_gate_definitions(repo, base)
    assert dict(protection.digests)["tests/conftest.py"]


def test_gate_tamper_gate_script_weakening_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (weakening gate-full.sh cannot buy a passing verdict)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "scripts" / "gate-full.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    _commit(repo, "weaken the full gate")
    _assert_blocked(repo, base, "scripts/gate-full.sh")


def test_gate_tamper_gate_tools_bypass_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (a gate_tools bypass is caught even without a commit)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "scripts" / "gate_tools.py").write_text("import sys\n\nsys.exit(0)\n")
    _assert_blocked(repo, base, "scripts/gate_tools.py")


def test_gate_tamper_pyproject_mypy_relaxation_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (disabling strict mypy in pyproject.toml is blocked)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "pyproject.toml").write_text(
        "[tool.mypy]\nstrict = false\n\n[tool.pytest.ini_options]\ntimeout = 180\n"
    )
    _commit(repo, "relax the type gate")
    _assert_blocked(repo, base, "pyproject.toml")


def test_gate_tamper_ci_workflow_change_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (a candidate cannot rewrite the CI gate it is judged by)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "name: ci\non: [push]\njobs: {}\n"
    )
    _commit(repo, "drop the CI gate")
    _assert_blocked(repo, base, ".github/workflows/ci.yml")


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
    _assert_blocked(repo, base, "tests/nested/conftest.py")


def test_gate_tamper_declared_gate_edit_is_still_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (a declared Touches entry is evidence, not authorization)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "scripts" / "gate-full.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    _commit(repo, "declared but unauthorized gate change")
    protection = verify_gate_definitions(
        repo, base, touches=("scripts/gate-full.sh",)
    )
    assert protection.is_protected is True
    assert protection.declared_paths == ("scripts/gate-full.sh",)
    assert any("not an authorization" in item for item in protection.owner_question.split("."))


def test_gate_tamper_clean_candidate_still_passes(tmp_path: Path) -> None:
    """Covers: GT-1 (an untouched gate definition set verifies and the gate runs)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    (repo / "src").mkdir()
    (repo / "src" / "thing.py").write_text("VALUE = 1\n")
    _commit(repo, "authorized product change")
    protection = verify_gate_definitions(repo, base, touches=("src/",))
    assert protection.is_protected is False
    assert protection.allowed is True
    assert protection.base_verified is True
    assert protection.base_sha == base


def test_gate_tamper_without_a_declared_diff_is_not_applicable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: GT-1 (no base means no trusted revision to guard against)."""
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    repo = _init_repo(tmp_path / "repo")
    evidence = tmp_path / "evidence"
    receipt = GateRunner().run(
        GateRunnerRequest(
            target_repo=repo,
            gate_command=f"{sys.executable} -c pass",
            timeout=30.0,
            frozen_gate_scratch_mode=GateScratchMode.DISK,
            candidate_sha=_head(repo),
            expected_post_gate_sha=_head(repo),
            evidence_dir=evidence,
        ),
        disk_guard=lambda _repo: None,
    )
    assert receipt.passed() is True
    assert receipt.gate_protection is None


def test_gate_runner_blocks_a_tampered_conftest_before_it_runs(tmp_path: Path) -> None:
    """Covers: H32 acceptance path (own conftest never runs; owner question)."""
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    marker = tmp_path / "conftest-ran"
    (repo / "conftest.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "import pytest\n"
    )
    candidate = _commit(repo, "tamper the root conftest")
    evidence = tmp_path / "evidence"
    with pytest.raises(GateProtectionBlockedError) as excinfo:
        GateRunner().run(
            GateRunnerRequest(
                target_repo=repo,
                gate_command=f"{sys.executable} -c 'import conftest'",
                timeout=30.0,
                frozen_gate_scratch_mode=GateScratchMode.DISK,
                candidate_sha=candidate,
                expected_post_gate_sha=candidate,
                evidence_dir=evidence,
                base_sha=base,
                changed_files=("conftest.py",),
            ),
            disk_guard=lambda _repo: None,
        )
    assert "GATE_PROTECTION_BLOCKED" in str(excinfo.value)
    assert GATE_PROTECTED_PATH_MUTATED in str(excinfo.value)
    assert excinfo.value.protection.owner_question
    # The candidate's own definition never ran, so it wrote no marker and the
    # gate produced no evidence artifact.
    assert marker.exists() is False
    assert list(evidence.rglob("outcome.json")) == []


def test_job_gate_never_runs_a_tampered_conftest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: H32 real job path (own conftest never runs; owner question)."""
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    repo = _init_repo(tmp_path / "repo")
    base = _head(repo)
    marker = tmp_path / "conftest-ran"
    (repo / "conftest.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "import pytest\n"
    )
    _commit(repo, "tamper the root conftest")
    config = RunConfig(task="candidate", target_repo=str(repo))
    result = _run_configured_gate(
        config,
        [sys.executable, "-c", "import conftest"],
        cwd=repo,
        timeout=30.0,
        base=base,
    )
    assert result.passed is False
    assert "GATE_PROTECTION_BLOCKED" in result.stderr
    assert GATE_PROTECTED_PATH_MUTATED in result.stderr
    assert "Owner decision required" in result.stderr
    assert marker.exists() is False


def test_job_gate_allows_an_ordinary_fix_that_edits_a_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: H32 (an ordinary test fix is not a protected-definition change)."""
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    repo = _init_repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "def test_two_plus_two():\n    assert 2 + 2 == 5\n", encoding="utf-8"
    )
    _commit(repo, "base with a failing test")
    base = _head(repo)
    (repo / "test_app.py").write_text(
        "def test_two_plus_two():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    _commit(repo, "fix the failing test")

    config = RunConfig(task="fix the test", target_repo=str(repo))
    result = _run_configured_gate(
        config,
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo,
        timeout=60.0,
        base=base,
    )
    assert result.passed is True, result.stderr


def test_gate_tamper_unverifiable_base_is_blocked(tmp_path: Path) -> None:
    """Covers: GT-1 (a base that cannot be read blocks instead of passing)."""
    repo = _init_repo(tmp_path / "repo")
    with pytest.raises(GateProtectionBlockedError) as excinfo:
        GateRunner().run(
            GateRunnerRequest(
                target_repo=repo,
                gate_command=f"{sys.executable} -c pass",
                timeout=30.0,
                frozen_gate_scratch_mode=GateScratchMode.DISK,
                candidate_sha=_head(repo),
                expected_post_gate_sha=_head(repo),
                evidence_dir=tmp_path / "evidence",
                base_sha="0" * 40,
                changed_files=("src/thing.py",),
            ),
            disk_guard=lambda _repo: None,
        )
    assert "GATE_BASE_UNVERIFIABLE" in str(excinfo.value)


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
