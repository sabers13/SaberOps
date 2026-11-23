from pathlib import Path

import pytest

from saberops.preflight import disk_findings


def test_disk_findings_identify_target_and_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from saberops import storage_cleanup

    target = tmp_path / "target"
    scratch = tmp_path / "scratch"
    target.mkdir()
    scratch.mkdir()
    monkeypatch.setattr(
        storage_cleanup,
        "_statvfs",
        lambda p: (1 if p == target else 20_000_000_000, 100_000_000_000),
    )
    findings = disk_findings(target, scratch)
    assert findings[0].severity == "FAIL"
    assert str(target) in findings[0].message
    assert findings[1].severity == "OK"
    monkeypatch.setattr(
        storage_cleanup,
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
        main(["project", "set-gate-state", "disabled", "--repo", str(rig.repo)]) == 0
    )
    findings = collect_preflight(rig.repo)
    assert any(item.severity == "FAIL" and "gate is disabled" in item.message for item in findings)
    # Review is mandatory: the project has no switch that could disable it.
    assert not any("review is disabled" in item.message for item in findings)


@pytest.mark.packaged_routing_default
def test_fresh_store_walks_printed_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os
    import shlex
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
        billing = remedy(f"{provider}/* billing:")
        # The recorded posture carries the class and the policy pages already.
        assert "<" not in billing, billing
        run(billing)
    run(remedy("build route:"))
    run(remedy("reviewer route:"))
    run(remedy("reviewer route:"))
    while True:
        quota = [
            f for f in collect_preflight(repo) if f.severity == "FAIL" and " quota:" in f.message
        ]
        if not quota:
            break
        run(quota[0].remedy.replace("<number from 0 to 100>", "80"))
    remaining = [f.message for f in collect_preflight(repo) if f.severity == "FAIL"]
    assert remaining == ["scheduler: not running"]
    # The walkthrough's scheduler step starts a detached scheduler and returns.
    scheduler_command = shlex.split(remedy("scheduler:"))
    assert scheduler_command.pop(0) == "orch"
    assert scheduler_command == ["scheduler", "start"]
    assert main(scheduler_command) == 0
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            failures = [f for f in collect_preflight(repo) if f.severity == "FAIL"]
            if not failures:
                break
            time.sleep(0.2)
        assert not failures
    finally:
        from saberops.capacity.loop import stop_scheduler
        from saberops.db import Database

        stop_scheduler(Database())


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


def test_opencode_login_remedy_does_not_require_a_first_run() -> None:
    """A7: the check must not claim only a run can prove OpenCode sign-in.

    The v2.3 ``auth list`` probe makes ``orch access test`` a zero-inference
    check, and an attended run proceeds even when the probe cannot read a
    verdict.  The remedy must say that instead of implying a broken login.
    """
    from saberops.preflight import sign_in_remedy

    remedy = sign_in_remedy("opencode", "INSUFFICIENT_EVIDENCE", "opencode-account")
    assert "orch access test opencode-account" in remedy
    assert "does not block an attended run" in remedy
    assert "only by a first successful run" not in remedy


def test_quota_status_does_not_call_fresh_evidence_stale() -> None:
    """A7: a fresh observation that the pool policy cannot use is not "stale".

    The caller appends the age, so a fresh row must never produce
    ``stale (age 0s)`` -- the condition is unusable evidence, not staleness.
    """
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from saberops.preflight import quota_status

    now = datetime.now(UTC)
    decision = SimpleNamespace(eligible=False, reason="QUOTA_EVIDENCE_REQUIRED")
    fresh = SimpleNamespace(updated_at=now.isoformat(), freshness="FRESH")
    stale = SimpleNamespace(updated_at=now.isoformat(), freshness="STALE")

    assert quota_status(decision, [fresh], now) == (
        False,
        "unusable evidence for this pool",
    )
    assert quota_status(decision, [stale], now) == (False, "stale")
    assert quota_status(decision, [], now) == (False, "missing")


def test_billing_remedy_names_the_recorded_class_and_policy_pages() -> None:
    """D11: no unexplained placeholders when the plan registry already knows them."""
    from saberops.preflight import _posture_verify_remedy

    remedy = _posture_verify_remedy("codex")
    assert remedy.startswith("orch posture verify codex --class A --url ")
    assert "learn.chatgpt.com" in remedy
    assert "<" not in remedy

    unknown = _posture_verify_remedy("nobody/never-heard-of-it")
    assert "--class '<A if this use is included in a plan you already pay for>'" in unknown
    assert "--url '<the page on your plan that says so>'" in unknown


def test_disk_finding_reads_in_gigabytes_and_the_step_quotes_only_the_failure() -> None:
    """D11: an OK filesystem must never read like part of a disk problem."""
    from saberops.preflight import Finding
    from saberops.walkthrough import _steps_from_findings

    target = "/p"
    findings = [
        Finding("OK", "target/gate: /p (filesystem mounted at /, probe /p): "
                       "18.6 GB available (18600000000 bytes); "
                       "12.4 GB required (12400000000 bytes)"),
        Finding("FAIL", "scratch: /s (filesystem mounted at /s, probe /s): "
                        "0.4 GB available (400000000 bytes); "
                        "12.4 GB required (12400000000 bytes)",
                "export SABEROPS_GATE_TMP=/path/on/a/filesystem/with/free/space"),
    ]
    steps = _steps_from_findings(findings)
    assert len(steps) == 1
    disk = steps[0]
    assert disk.key == "disk"
    assert "0.4 GB available" in disk.detail
    assert "18.6 GB" not in disk.detail
    assert target not in disk.detail
