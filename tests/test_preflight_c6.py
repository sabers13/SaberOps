from pathlib import Path

import pytest

from saberops.preflight import disk_findings


def test_disk_findings_identify_target_and_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from saberops import storage_maintenance

    target = tmp_path / "target"
    scratch = tmp_path / "scratch"
    target.mkdir()
    scratch.mkdir()
    monkeypatch.setattr(
        storage_maintenance,
        "_statvfs",
        lambda p: (1 if p == target else 20_000_000_000, 100_000_000_000),
    )
    findings = disk_findings(target, scratch)
    assert findings[0].severity == "FAIL"
    assert str(target) in findings[0].message
    assert findings[1].severity == "OK"
    monkeypatch.setattr(
        storage_maintenance,
        "_statvfs",
        lambda p: (1 if p == scratch else 20_000_000_000, 100_000_000_000),
    )
    findings = disk_findings(target, scratch)
    assert findings[0].severity == "OK"
    assert findings[1].severity == "FAIL"
    assert str(scratch) in findings[1].message
    assert "SABEROPS_GATE_TMP" in findings[1].remedy


@pytest.mark.packaged_routing_default
def test_configured_rig_reports_readiness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from lifecycle_rig import build_lifecycle_rig

    from saberops import vendor_cli_fixtures
    from saberops.preflight import collect_preflight

    monkeypatch.setattr(vendor_cli_fixtures, "read_installed_version", lambda cli: f"{cli} fake")
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    findings = collect_preflight(rig.repo)
    failures = [item.message for item in findings if item.severity == "FAIL"]
    assert not any("quota:" in failure for failure in failures)
    assert failures[-1] == "scheduler: not running"
    from saberops.cli import main

    assert (
        main(
            [
                "project",
                "set-workflow",
                "custom",
                "--repo",
                str(rig.repo),
                "--gate-disabled",
                "--review",
                "disabled",
            ]
        )
        == 0
    )
    findings = collect_preflight(rig.repo)
    assert any(item.severity == "FAIL" and "gate is disabled" in item.message for item in findings)
    assert any(
        item.severity == "FAIL" and "review is disabled" in item.message for item in findings
    )


@pytest.mark.packaged_routing_default
def test_fresh_store_walks_printed_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os
    import shlex
    import subprocess
    import sys
    import time

    from lifecycle_rig import _install_shim, create_toy_repo

    from saberops.cli import main
    from saberops.preflight import collect_preflight

    for variable, name in (
        ("HOME", "home"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "state" / "saberops"
    state.mkdir()
    log = tmp_path / "vendor-commands"
    for cli in ("codex", "claude"):
        shim = _install_shim(bindir, cli, state)
        contents = shim.read_text()
        prefix = (
            f'printf "%s\\n" "$*" >> "{log}"\n'
            f'if [ "$1" = "--version" ]; then echo "{cli} fake"; exit 0; fi\n'
        )
        shim.write_text(contents.replace("#!/bin/sh\n", "#!/bin/sh\n" + prefix))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    repo, _ = create_toy_repo(tmp_path)
    collect_preflight(repo)
    assert log.read_text().splitlines() == ["--version", "--version"]

    def remedy(prefix: str) -> str:
        return next(f.remedy for f in collect_preflight(repo) if f.message.startswith(prefix))

    def run(command: str) -> None:
        for part in command.split(" && "):
            words = shlex.split(part)
            assert words.pop(0) == "orch"
            assert main(words) == 0

    run(remedy("check command:").replace("<check command>", "true"))
    run(remedy("codex sign-in:"))
    run(remedy("anthropic sign-in:"))
    for provider in ("codex", "anthropic"):
        run(
            remedy(f"{provider}/* billing:")
            .replace("<owner-confirmed-class>", "A")
            .replace("<policy-url>", "https://example.com/policy")
        )
    run(remedy("build route:"))
    run(remedy("reviewer route:"))
    run(remedy("reviewer route:"))
    while True:
        quota = [
            f for f in collect_preflight(repo) if f.severity == "FAIL" and " quota:" in f.message
        ]
        if not quota:
            break
        run(quota[0].remedy.replace("<percent>", "80"))
    remaining = [f.message for f in collect_preflight(repo) if f.severity == "FAIL"]
    assert remaining == ["scheduler: not running"]
    scheduler_command = shlex.split(remedy("scheduler:"))
    assert scheduler_command.pop(0) == "orch"
    process = subprocess.Popen(
        [sys.executable, "-m", "saberops.cli", *scheduler_command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            failures = [f for f in collect_preflight(repo) if f.severity == "FAIL"]
            if not failures:
                break
            assert process.poll() is None
            time.sleep(0.2)
        assert not failures
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_mcp_preflight_uses_same_findings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from saberops import preflight
    from saberops.mcp.tools import ServerContext, build_default_registry

    expected = [preflight.Finding("FAIL", "check missing", "orch project set-gate")]
    monkeypatch.setattr(preflight, "collect_preflight", lambda repo, db: expected)
    registry = build_default_registry(ServerContext(db_path=tmp_path / "owner.db"))
    payload = registry.call("preflight", {"repo": str(tmp_path)})
    assert payload["ready"] is False
    assert payload["findings"] == [
        {"severity": "FAIL", "message": "check missing", "remedy": "orch project set-gate"}
    ]


def test_preflight_cli_serializes_shared_results(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import json

    from saberops import preflight
    from saberops.cli import main

    expected = [preflight.Finding("FAIL", "check missing", "orch project set-gate")]
    monkeypatch.setattr(preflight, "collect_preflight", lambda repo, db: expected)
    assert main(["preflight", "--repo", str(tmp_path), "--json"]) == 1
    output = json.loads(capsys.readouterr().out)
    assert not output["ready"] and output["findings"][0]["remedy"] == "orch project set-gate"


def test_login_remedy_refreshes_evidence_after_signin() -> None:
    from saberops.preflight import sign_in_remedy

    assert sign_in_remedy("codex", "NOT_AUTHENTICATED", "codex-chatgpt-account") == (
        "codex login && orch access test codex-chatgpt-account"
    )
    assert sign_in_remedy("codex", "NO_EVIDENCE", "codex-chatgpt-account") == (
        "orch access test codex-chatgpt-account"
    )
