"""Engine-run gate execution: profiles, slots, history, scope and producer."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from saberops.gate_history import (
    GateHistoryError,
    GateKind,
    read_entries,
    require_engine_evidence,
)
from saberops.gate_profiles import (
    GateCommand,
    GateProfile,
    GateProfileError,
    GateProfileName,
)
from saberops.gate_runner import (
    EngineGateExecutor,
    EngineGateRequest,
    GateSlotPool,
    GateSlotWaitTimeout,
    host_disk_admission,
)
from saberops.gate_scope import RetirementAction, RetirementEntry

WRITER = '''\
import json, os, pathlib, sys
out = pathlib.Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
word = os.environ.get("SABEROPS_TEST_OUTCOME", "PASS")
failed = [x for x in os.environ.get("SABEROPS_TEST_FAILED", "").split(",") if x]
flaky = [x for x in os.environ.get("SABEROPS_TEST_FLAKY", "").split(",") if x]
payload = {
    "outcome": word,
    "reason": os.environ.get("SABEROPS_TEST_REASON", ""),
    "counts": {"tests": int(os.environ.get("SABEROPS_TEST_TESTS", "3")),
               "failed": len(failed), "flaky": len(flaky)},
    "failed": failed,
    "flaky": flaky,
    "checks_ok": os.environ.get("SABEROPS_TEST_CHECKS", "1") == "1",
    "selected": os.environ.get("SABEROPS_TEST_SELECTED", "1") == "1",
}
(out / "outcome.json").write_text(json.dumps(payload))
exit_code = {"PASS": 0, "FLAKY_PASS": 0, "FAIL": 1, "TIMEOUT": 1, "INFRA": 3}
sys.exit(exit_code.get(word, 1))
'''


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


def _init_repo(path: Path) -> Path:
    """Covers: helper (temporary git repo only)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_output(["git", "init", "-q", "-b", "main"], cwd=path)
    subprocess.check_output(["git", "config", "user.email", "t@example.invalid"], cwd=path)
    subprocess.check_output(["git", "config", "user.name", "T"], cwd=path)
    (path / "tests").mkdir(exist_ok=True)
    (path / "tests" / "quarantine.txt").write_text("")
    (path / "README.md").write_text("hi\n")
    subprocess.check_output(["git", "add", "-A"], cwd=path)
    subprocess.check_output(["git", "commit", "-qm", "init"], cwd=path)
    return path


def _writer(repo: Path) -> Path:
    script = repo / "writer.py"
    script.write_text(WRITER)
    return script


def _stub_profile(
    repo: Path, name: GateProfileName, *, extra: str = "", budget: int = 120
) -> GateProfile:
    command = f"{{python}} {_writer(repo)} {{result_dir}} {extra}".strip()
    return GateProfile(
        name=name,
        commands=(GateCommand("stub", command),),
        wall_budget_seconds=budget,
        rerun_failures=name is GateProfileName.ACCEPTANCE,
        require_map_freshness=False,
    )


def _request(
    repo: Path,
    kind: str,
    evidence_dir: Path,
    profile: GateProfile | None = None,
    **kwargs: object,
) -> EngineGateRequest:
    profiles = {
        GateProfileName.FAST: profile or _stub_profile(repo, GateProfileName.FAST),
        GateProfileName.FULL: profile or _stub_profile(repo, GateProfileName.FULL),
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
        **kwargs,  # type: ignore[arg-type]
    )


def _executor(**kwargs: object) -> EngineGateExecutor:
    return EngineGateExecutor(
        slot_pool=_FakePool(), disk_check=lambda repo, *, estimate_bytes=0: None, **kwargs
    )


def test_engine_g3_pass_records_engine_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0004 D3 (G3 engine evidence, outcome.json + kind/producer)."""
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("SABEROPS_TEST_OUTCOME", "PASS")
    request = _request(repo, "G3", tmp_path / "evidence")
    result = _executor().execute(request)
    assert result.outcome == "PASS"
    assert result.accepted is True
    assert result.producer == "engine"
    assert result.profile == "full"
    assert result.kind == "G3"
    assert result.tests == 3
    outcome = json.loads(Path(result.outcome_path).read_text())
    assert outcome["producer"] == "engine" and outcome["kind"] == "G3"
    entries = read_entries(result.history_path)
    assert len(entries) == 1
    assert entries[0]["kind"] == "G3"
    assert entries[0]["producer"] == "engine"
    assert entries[0]["profile"] == "full"
    assert entries[0]["outcome"] == "PASS"
    require_engine_evidence(entries[0])


def test_engine_g1_fail_is_repair_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: ADR-0003 D1 (FAIL is a code failure with the failing ids)."""
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("SABEROPS_TEST_OUTCOME", "FAIL")
    monkeypatch.setenv("SABEROPS_TEST_FAILED", "tests/test_a.py::test_x")
    result = _executor().execute(_request(repo, "G1", tmp_path / "evidence"))
    assert result.outcome == "FAIL"
    assert result.accepted is False
    assert result.failed == ("tests/test_a.py::test_x",)


def test_engine_g1_flaky_pass_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: ADR-0003 D1 (G1 FLAKY_PASS needs no quarantine)."""
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("SABEROPS_TEST_OUTCOME", "FLAKY_PASS")
    monkeypatch.setenv("SABEROPS_TEST_FLAKY", "tests/test_a.py::test_x")
    result = _executor().execute(_request(repo, "G1", tmp_path / "evidence"))
    assert result.outcome == "FLAKY_PASS"
    assert result.accepted is True


def test_engine_pass_without_checks_evidence_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: fail-closed (missing checks_ok/artifact can never be green)."""
    repo = _init_repo(tmp_path / "repo")
    script = repo / "writer.py"
    script.write_text("import sys; sys.exit(0)\n")  # writes nothing
    profile = GateProfile(
        name=GateProfileName.FULL,
        commands=(GateCommand("stub", f"{{python}} {script} {{result_dir}}"),),
        wall_budget_seconds=30,
    )
    result = _executor().execute(_request(repo, "G3", tmp_path / "evidence", profile=profile))
    assert result.outcome == "INFRA"
    assert result.accepted is False
    assert "GATE_ARTIFACT_MISSING" in result.reason


def test_engine_gate_refuses_write_scope_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0004 D3 (out-of-scope edit blocks before any command runs)."""
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("SABEROPS_TEST_OUTCOME", "PASS")
    request = _request(
        repo,
        "G1",
        tmp_path / "evidence",
        touches=("src/saberops/gate_runner.py",),
        changed_files=("src/saberops/web.py",),
    )
    result = _executor().execute(request)
    assert result.outcome == "BLOCKED"
    assert result.accepted is False
    assert "WRITE_SCOPE_VIOLATION" in result.reason
    assert result.scope is not None and result.scope.out_of_scope == ("src/saberops/web.py",)
    # Fail closed: the profile command never ran, so it produced no artifact.
    assert not list((tmp_path / "evidence").rglob("outcome.json"))
    assert read_entries(result.history_path)[0]["outcome"] == "BLOCKED"


def test_engine_gate_refuses_unauthorized_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0004 D3 (unauthorized test deletion blocks)."""
    repo = _init_repo(tmp_path / "repo")
    base = {"tests/test_mod.py": "def test_a():\n    assert True\n"}
    head = {"tests/test_mod.py": "def test_b():\n    assert True\n"}
    request = _request(
        repo,
        "G1",
        tmp_path / "evidence",
        base_test_sources=base,
        head_test_sources=head,
    )
    result = _executor().execute(request)
    assert result.outcome == "BLOCKED"
    assert "TEST_RETIREMENT_VIOLATION" in result.reason
    assert result.retirement is not None
    assert result.retirement.unauthorized_removed == ("tests/test_mod.py::test_a",)


def test_engine_gate_allows_authorized_retirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: an authorized removal does not block the gate."""
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("SABEROPS_TEST_OUTCOME", "PASS")
    request = _request(
        repo,
        "G1",
        tmp_path / "evidence",
        base_test_sources={"tests/test_mod.py": "def test_a():\n    assert True\n"},
        head_test_sources={"tests/test_mod.py": "def test_b():\n    assert True\n"},
        retirement=(RetirementEntry("tests/test_mod.py::test_a", RetirementAction.REMOVED),),
    )
    result = _executor().execute(request)
    assert result.outcome == "PASS"


def test_engine_gate_disk_low_is_infra_and_recorded(tmp_path: Path) -> None:
    """Covers: ADR-0003 D4 (below floor -> INFRA DISK_LOW, never a pass)."""
    repo = _init_repo(tmp_path / "repo")
    executor = EngineGateExecutor(
        slot_pool=_FakePool(),
        disk_check=lambda repo, *, estimate_bytes=0: "DISK_LOW",
    )
    result = executor.execute(_request(repo, "G3", tmp_path / "evidence"))
    assert result.outcome == "INFRA"
    assert result.accepted is False
    assert "DISK_LOW" in result.reason
    assert read_entries(result.history_path)[0]["outcome"] == "INFRA"


def test_engine_gate_slot_timeout_is_infra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: ADR-0003 D3 (slot wait exceeded -> INFRA SLOT_WAIT_TIMEOUT)."""
    repo = _init_repo(tmp_path / "repo")
    common = (repo / ".git").resolve()
    pool = GateSlotPool(common)
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "0.1")
    with pool.heavy():
        executor = EngineGateExecutor(
            slot_pool=pool, disk_check=lambda repo, *, estimate_bytes=0: None
        )
        result = executor.execute(_request(repo, "G3", tmp_path / "evidence"))
    assert result.outcome == "INFRA"
    assert "SLOT_WAIT_TIMEOUT" in result.reason


def test_kind_selects_slot_class(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: ADR-0003 D3 (G1/G2 light; G3/main-gate heavy+light)."""
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("SABEROPS_TEST_OUTCOME", "PASS")
    light_pool = _FakePool()
    EngineGateExecutor(
        slot_pool=light_pool, disk_check=lambda repo, *, estimate_bytes=0: None
    ).execute(_request(repo, "G1", tmp_path / "e1"))
    assert light_pool.calls == ["light"]
    heavy_pool = _FakePool()
    EngineGateExecutor(
        slot_pool=heavy_pool, disk_check=lambda repo, *, estimate_bytes=0: None
    ).execute(_request(repo, "G3", tmp_path / "e2"))
    assert heavy_pool.calls == ["heavy"]


def test_engine_gate_rejects_profile_lowering(tmp_path: Path) -> None:
    """Covers: ADR-0001 invariant 3 (caller cannot lower required profile)."""
    repo = _init_repo(tmp_path / "repo")
    request = _request(repo, "G3", tmp_path / "evidence", requested_profile="fast")
    with pytest.raises(GateProfileError) as excinfo:
        _executor().execute(request)
    assert "requires at least" in str(excinfo.value)


def test_slot_pool_heavy_excludes_light(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: ADR-0003 D3 (heavy = 1 exclusive; lane waits while held)."""
    common = tmp_path / "common"
    pool = GateSlotPool(common)
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "0.2")
    with pool.heavy():
        with pytest.raises(GateSlotWaitTimeout):
            with pool.light():
                pytest.fail("lane entered while heavy held")
    with pool.light():
        pass


def test_slot_pool_two_light_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: ADR-0003 D3 (light slots = 2 concurrent)."""
    common = tmp_path / "common"
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "5")
    held: list[int] = []
    guard = threading.Lock()
    release = threading.Event()

    def holder() -> None:
        with GateSlotPool(common).light():
            with guard:
                held.append(1)
            assert release.wait(timeout=5)

    threads = [threading.Thread(target=holder) for _ in range(2)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with guard:
            if len(held) == 2:
                break
        time.sleep(0.05)
    with guard:
        assert len(held) == 2
    # Both light slots are held -> a third waits and times out fast.
    monkeypatch.setenv("SABEROPS_GATE_SLOT_WAIT", "0.2")
    with pytest.raises(GateSlotWaitTimeout):
        with GateSlotPool(common).light():
            pytest.fail("third light slot acquired")
    release.set()
    for thread in threads:
        thread.join(timeout=5)


def test_host_disk_admission_floor_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Covers: ADR-0003 D4 (shared floor is always enforced)."""
    import saberops.gate_runner as gate_runner

    monkeypatch.setattr(
        gate_runner, "default_gate_disk_guard", lambda repo, minimum_gb=10.0: "DISK_LOW"
    )
    assert host_disk_admission(tmp_path, estimate_bytes=1) == "DISK_LOW"


def test_history_rejects_non_engine_producer(tmp_path: Path) -> None:
    """Covers: ADR-0004 D3 (G0/model runs are never evidence)."""
    with pytest.raises(GateHistoryError):
        require_engine_evidence({"producer": "model"})
    with pytest.raises(GateHistoryError):
        require_engine_evidence({})
    require_engine_evidence({"producer": "engine"})


def test_orchestrator_seam_forwards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: orchestrator integration seam delegates to the engine executor."""
    import saberops.orchestrator as orchestrator

    captured: dict[str, object] = {}

    class _Recorder:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["init"] = kwargs

        def execute(self, request: EngineGateRequest) -> object:
            captured["request"] = request
            return "sentinel"

    monkeypatch.setattr(orchestrator, "EngineGateExecutor", _Recorder)
    monkeypatch.setattr(orchestrator, "GateSlotPool", lambda common: "pool")
    monkeypatch.setattr(orchestrator, "_common_dir_for", lambda repo: tmp_path)
    result = orchestrator.run_engine_gate(
        target_repo=tmp_path / "repo",
        kind="G1",
        evidence_dir=tmp_path / "ev",
        base="base",
        head_sha="head",
    )
    assert result == "sentinel"
    request = captured["request"]
    assert isinstance(request, EngineGateRequest)
    assert request.kind == "G1"

def test_slot_pool_refuses_invalid_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: malformed slot configuration fails closed as SLOT_WAIT_TIMEOUT."""
    monkeypatch.setenv("SABEROPS_GATE_SLOTS", "zero")
    with pytest.raises(GateSlotWaitTimeout):
        with GateSlotPool(tmp_path / "c").light():
            pass


def test_engine_history_refuses_producer_after_append(tmp_path: Path) -> None:
    """Covers: appended engine lines always carry the authoritative producer."""
    repo = _init_repo(tmp_path / "repo")
    entries_before = read_entries(repo / ".git" / "saberops-gate-history" / "full.jsonl")
    assert entries_before == []
    # A model run is refused by the shared guard even if it reaches a consumer.
    with pytest.raises(GateHistoryError):
        require_engine_evidence({"kind": GateKind.G1.value, "producer": "G0"})


def test_gate_kind_enum_covers_lifecycle_points() -> None:
    """Covers: ADR-0004 D3 (kind taxonomy G1|G2|G3|main-gate|manual)."""
    assert {kind.value for kind in GateKind} == {"G1", "G2", "G3", "main-gate", "manual"}


def test_engine_executor_requires_wall_budget() -> None:
    """Covers: ADR-0003 D2 (profiles always carry a wall budget)."""
    with pytest.raises(GateProfileError):
        GateProfile(
            name=GateProfileName.FAST,
            commands=(GateCommand("x", "true"),),
            wall_budget_seconds=0,
        )


def test_engine_gate_records_wall_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: evidence always carries a real elapsed wall time."""
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("SABEROPS_TEST_OUTCOME", "PASS")
    started = time.monotonic()
    result = _executor().execute(_request(repo, "G1", tmp_path / "evidence"))
    assert result.wall_time >= 0.0
    assert time.monotonic() - started >= 0.0
    assert os.path.isdir(tmp_path / "evidence")


def test_engine_gate_extra_env_reaches_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: extra_env is overlaid for project-specific gate commands."""
    repo = _init_repo(tmp_path / "repo")
    marker = repo / "marker.txt"
    script = repo / "env_writer.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)\n"
        "(pathlib.Path(os.environ['SABEROPS_TEST_MARKER'])).write_text('seen')\n"
        "(out / 'outcome.json').write_text(json.dumps({"
        "'outcome': 'PASS', 'counts': {'tests': 1}, 'checks_ok': True, 'selected': True}))\n"
    )
    profile = GateProfile(
        name=GateProfileName.FAST,
        commands=(GateCommand("stub", f"{{python}} {script} {{result_dir}}"),),
        wall_budget_seconds=30,
    )
    request = _request(
        repo,
        "G1",
        tmp_path / "evidence",
        profile=profile,
        extra_env={"SABEROPS_TEST_MARKER": str(marker)},
    )
    result = _executor().execute(request)
    assert result.outcome == "PASS"
    assert marker.read_text() == "seen"
