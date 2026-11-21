"""Vendor CLI update guard (phase 1 lane F5).

A version change on the installed CLI triggers the no-prompt allowlisted
``--help`` check.  Only a complete capture that omits an adapter-pinned flag is
reported as the clear ``UNSUPPORTED`` state; an unreadable probe is
``UNVERIFIED`` so no dispatch decision is ever made on a guess.

Every test uses fake manifests, fake runners or a stub binary.  No test resolves
a real vendor CLI or sends a prompt.
"""

from __future__ import annotations

import io
import json
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from saberops import preflight
from saberops import vendor_cli_fixtures as vcf
from saberops.vendor_cli_fixtures import (
    HELP_COMMANDS,
    MISSING,
    UNKNOWN_VERSION,
    Fixture,
    Manifest,
    VersionCheck,
    VersionSupport,
    check_version_support,
    detect_drift,
    load_manifest,
    run_check,
)

CODEX_FIXTURE_VERSION = "codex-cli 1.0.0"
CODEX_NEW_VERSION = "codex-cli 1.1.0"


def _fixture(cli: str = "codex", version: str = CODEX_FIXTURE_VERSION) -> Fixture:
    return Fixture(
        vendor=cli,
        cli=cli,
        cli_version=version,
        captured_on="2026-11-19",
        kind="config_help",
        source="real-capture",
        redacted=True,
        file=f"{cli}/x/config_help.txt",
        consumer="saberops.workers.codex:CodexAdapter",
        expect={"flags": ["--model", "--format", "exec"]},
    )


def _manifest(*fixtures: Fixture) -> Manifest:
    return Manifest(fixtures=fixtures or (_fixture(),), fake_only=())


def _recording_run(
    outputs: dict[tuple[str, ...], tuple[int, str, str]]
) -> tuple[vcf.RunFn, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []

    def run(command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        calls.append(tuple(command[1:]))
        return outputs.get(tuple(command[1:]), (0, "help text", ""))

    return run, calls


# ---------------------------------------------------------------------------
# check_version_support
# ---------------------------------------------------------------------------


def test_version_change_with_documented_flags_is_supported() -> None:
    """Covers: F5 a real version change that keeps pinned flags is SUPPORTED."""
    outputs = dict.fromkeys(
        HELP_COMMANDS["codex"], (0, "codex help --model --format exec\n", "")
    )
    run, calls = _recording_run(outputs)
    check = check_version_support(
        _manifest(), "codex", CODEX_NEW_VERSION, which=lambda _cli: "/usr/bin/codex", run=run
    )
    assert check.state is VersionSupport.SUPPORTED
    assert check.missing == ()
    assert check.commands == tuple(" ".join(item) for item in HELP_COMMANDS["codex"])
    assert set(calls) == set(HELP_COMMANDS["codex"]), "only allowlisted help may run"


def test_version_change_missing_pinned_flag_is_unsupported() -> None:
    """Covers: F5 a complete help capture without a pinned flag is UNSUPPORTED."""
    outputs = dict.fromkeys(HELP_COMMANDS["codex"], (0, "codex help --format exec\n", ""))
    run, _calls = _recording_run(outputs)
    check = check_version_support(
        _manifest(), "codex", CODEX_NEW_VERSION, which=lambda _cli: "/usr/bin/codex", run=run
    )
    assert check.state is VersionSupport.UNSUPPORTED
    assert check.missing == ("--model",)
    assert "absent" in check.detail


def test_incomplete_help_capture_is_unverified_never_unsupported() -> None:
    """Covers: F5 a failed help probe never becomes an unsupported guess."""
    failed = HELP_COMMANDS["codex"][1]

    def run(command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        if tuple(command[1:]) == failed:
            return 1, "", "boom"
        return 0, "codex help --model --format exec\n", ""

    check = check_version_support(
        _manifest(), "codex", CODEX_NEW_VERSION, which=lambda _cli: "/usr/bin/codex", run=run
    )
    assert check.state is VersionSupport.UNVERIFIED
    assert check.missing == ()
    assert "help capture failed" in check.detail


def test_probe_exception_is_unverified() -> None:
    """Covers: F5 a probe exception is UNVERIFIED, not UNSUPPORTED."""

    def run(_command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        raise OSError("cannot execute")

    check = check_version_support(
        _manifest(), "codex", CODEX_NEW_VERSION, which=lambda _cli: "/usr/bin/codex", run=run
    )
    assert check.state is VersionSupport.UNVERIFIED
    assert "OSError" in check.detail


def test_matching_version_probes_nothing() -> None:
    """Covers: F5 no help runs when the fixtures already match the CLI."""

    def run(_command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        raise AssertionError("no help probe for a matching version")

    check = check_version_support(
        _manifest(),
        "codex",
        CODEX_FIXTURE_VERSION,
        which=lambda _cli: "/usr/bin/codex",
        run=run,
    )
    assert check.state is VersionSupport.SUPPORTED
    assert check.commands == ()


def test_missing_cli_is_unverified() -> None:
    """Covers: F5 a missing CLI is UNVERIFIED."""
    check = check_version_support(
        _manifest(), "codex", MISSING, which=lambda _cli: None, run=None
    )
    assert check.state is VersionSupport.UNVERIFIED
    assert "not installed" in check.detail


@pytest.mark.parametrize("reported", ["codex fake", "codex 0.0.0-fake", "0.0.0"])
def test_foreign_version_shape_is_never_probed(reported: str) -> None:
    """Covers: F5 a shim answering --version is not mistaken for an update."""
    probed: list[str] = []

    def which(_cli: str) -> str:
        probed.append("which")
        return "/tmp/fake-codex"

    def run(_command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        raise AssertionError("a foreign version shape must not be probed")

    check = check_version_support(_manifest(), "codex", reported, which=which, run=run)
    assert check.state is VersionSupport.UNVERIFIED
    assert probed == []
    assert "help probe skipped" in check.detail


def test_pipe_truncating_help_still_reads_the_full_text(tmp_path: Path) -> None:
    """Covers: F5 a vendor that truncates piped --help is still read fully.

    Claude Code 2.1.328 emits only a pipe-buffer prefix of its ``--help`` text
    when stdout is a pipe.  The guard therefore captures into a regular file;
    a pipe would hide ``--model`` and wrongly report ``UNSUPPORTED``.
    """
    stub = tmp_path / "codex"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os, stat, sys\n"
        "text = 'x' * 9000 + ' --model --format exec\\n'\n"
        "if stat.S_ISFIFO(os.fstat(1).st_mode):\n"
        "    sys.stdout.write(text[:8192])\n"
        "else:\n"
        "    sys.stdout.write(text)\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    check = check_version_support(
        _manifest(), "codex", CODEX_NEW_VERSION, which=lambda _cli: str(stub)
    )
    assert check.state is VersionSupport.SUPPORTED
    assert check.missing == ()


# ---------------------------------------------------------------------------
# The `check` command reports the state for a fake CLI
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, version: str) -> Path:
    fixture = tmp_path / "codex" / "x" / "config_help.txt"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("$ codex --help (exit 0)\nstub help\n", encoding="utf-8")
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "fixtures": [
                    {
                        "vendor": "codex",
                        "cli": "codex",
                        "cli_version": version,
                        "captured_on": "2026-11-19",
                        "kind": "config_help",
                        "source": "real-capture",
                        "redacted": True,
                        "file": "codex/x/config_help.txt",
                        "consumer": "saberops.workers.codex:CodexAdapter",
                        "expect": {"flags": ["--model"]},
                    }
                ],
                "fake_only": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _stub_cli(directory: Path, version: str, help_text: str) -> Path:
    path = directory / "codex"
    path.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$F5_ARGV_LOG"\n'
        'if [ "$1" = "--version" ]; then\n'
        f'  echo "{version}"\n'
        "  exit 0\n"
        "fi\n"
        f'echo "{help_text}"\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_fake_cli_with_new_version_triggers_the_check_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: F5 completion — a fake CLI reporting a new version shows the state."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    log = tmp_path / "argv.log"
    monkeypatch.setenv("F5_ARGV_LOG", str(log))
    stub = _stub_cli(stub_dir, "codex-cli 9.9.9", "codex stub help without the pinned flag")
    stream = io.StringIO()
    exit_code = run_check(
        _write_manifest(tmp_path, CODEX_FIXTURE_VERSION),
        which=lambda _cli: str(stub),
        stream=stream,
    )
    output = stream.getvalue()
    assert exit_code == 1, "drift still reports exit 1"
    assert "DRIFT codex (codex)" in output
    assert "UNSUPPORTED codex (codex)" in output
    assert "--model" in output

    logged = [tuple(line.split()) for line in log.read_text().splitlines()]
    allow = {("--version",)} | set(HELP_COMMANDS["codex"])
    assert set(logged) <= allow, f"non-allowlisted command ran: {set(logged) - allow}"
    assert ("--version",) in logged
    assert ("--help",) in logged


def test_check_reports_unverified_when_the_version_shape_is_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: F5 the check never probes a shim reporting a foreign version."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    log = tmp_path / "argv.log"
    monkeypatch.setenv("F5_ARGV_LOG", str(log))
    stub = _stub_cli(stub_dir, "codex 0.0.0-fake", "stub help")
    stream = io.StringIO()
    exit_code = run_check(
        _write_manifest(tmp_path, "codex 1.0.0"),
        which=lambda _cli: str(stub),
        stream=stream,
    )
    output = stream.getvalue()
    assert exit_code == 1
    assert "UNVERIFIED codex (codex)" in output
    assert "UNSUPPORTED" not in output
    assert log.read_text().splitlines() == ["--version"]


# ---------------------------------------------------------------------------
# Preflight surfaces the state without blocking on a guess
# ---------------------------------------------------------------------------


def _manifest_with_version(version: str = CODEX_FIXTURE_VERSION) -> Manifest:
    return _manifest(_fixture(version=version))


def test_preflight_match_has_no_version_findings() -> None:
    """Covers: F5 preflight stays silent when fixtures match."""
    findings = preflight.vendor_cli_version_findings(
        "codex", "codex", CODEX_FIXTURE_VERSION, _manifest_with_version()
    )
    assert findings == []


def test_preflight_drift_with_unsupported_flags_is_a_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: F5 preflight reports the clear unsupported-version state."""
    check = VersionCheck(
        cli="codex",
        vendor="codex",
        installed_version=CODEX_NEW_VERSION,
        fixture_versions=(CODEX_FIXTURE_VERSION,),
        state=VersionSupport.UNSUPPORTED,
        missing=("--model",),
        detail="recorded adapter flags are absent from --help",
    )
    monkeypatch.setattr(vcf, "check_version_support", lambda *a, **k: check)
    findings = preflight.vendor_cli_version_findings(
        "codex", "codex", CODEX_NEW_VERSION, _manifest_with_version()
    )
    assert findings[0].severity == "WARN"
    assert "differs from recorded fixtures" in findings[0].message
    assert findings[1].severity == "FAIL"
    assert "unsupported" in findings[1].message
    assert "--model" in findings[1].message


def test_preflight_drift_unverified_stays_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: F5 preflight never escalates an unverifiable probe."""
    check = VersionCheck(
        cli="codex",
        vendor="codex",
        installed_version=CODEX_NEW_VERSION,
        fixture_versions=(CODEX_FIXTURE_VERSION,),
        state=VersionSupport.UNVERIFIED,
        detail="help capture failed: codex --help (exit 2)",
    )
    monkeypatch.setattr(vcf, "check_version_support", lambda *a, **k: check)
    findings = preflight.vendor_cli_version_findings(
        "codex", "codex", CODEX_NEW_VERSION, _manifest_with_version()
    )
    assert [finding.severity for finding in findings] == ["WARN", "WARN"]
    assert "not verified" in findings[1].message


def test_preflight_drift_with_supported_flags_is_a_guard_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers: F5 preflight reports that a drifted CLI still documents the flags."""
    check = VersionCheck(
        cli="codex",
        vendor="codex",
        installed_version=CODEX_NEW_VERSION,
        fixture_versions=(CODEX_FIXTURE_VERSION,),
        state=VersionSupport.SUPPORTED,
        detail="recorded adapter flags are still documented",
    )
    monkeypatch.setattr(vcf, "check_version_support", lambda *a, **k: check)
    findings = preflight.vendor_cli_version_findings(
        "codex", "codex", CODEX_NEW_VERSION, _manifest_with_version()
    )
    assert [finding.severity for finding in findings] == ["WARN", "OK"]
    assert "still documented" in findings[1].message


# ---------------------------------------------------------------------------
# The checked-in fixtures were refreshed from token-free captures
# ---------------------------------------------------------------------------


def test_committed_fixtures_are_refreshed_to_the_installed_versions() -> None:
    """Covers: F5 fixture refresh (2026-11-19 installed versions)."""
    manifest = load_manifest()
    recorded = {fixture.cli: fixture.cli_version for fixture in manifest.fixtures}
    assert recorded == {
        "codex": "codex-cli 0.170.0",
        "claude": "2.1.328 (Claude Code)",
        "opencode": "opencode v2.3.2",
    }
    for fixture in manifest.fixtures:
        # The v2.3 server-route fixture was added 2026-11-20 (ADR-0039).
        expected = "2026-11-20" if fixture.kind == "server_openapi" else "2026-11-19"
        assert fixture.captured_on == expected
        assert (vcf.FIXTURES_ROOT / fixture.file).is_file()
    assert detect_drift(manifest, recorded) == []
    assert UNKNOWN_VERSION not in recorded.values()


def test_no_stale_fixture_directory_survives_the_refresh() -> None:
    """Covers: F5 the pre-refresh fixture directories are gone."""
    stale_tokens = ("2.1.301", "0.162.1", "2.0.29")
    stale = sorted(
        path.relative_to(vcf.FIXTURES_ROOT).as_posix()
        for path in vcf.FIXTURES_ROOT.rglob("*")
        if path.is_file() and any(token in path.as_posix() for token in stale_tokens)
    )
    assert stale == []


def test_guard_helpers_are_part_of_the_module_surface() -> None:
    """Covers: F5 the version guard is available through the module surface."""
    assert "check_version_support" in vcf.__all__
    assert "VersionSupport" in vcf.__all__
    assert set(HELP_COMMANDS) == set(vcf.VENDOR_CLI)


# ---------------------------------------------------------------------------
# D6: an installed build has no ``tests/`` tree
# ---------------------------------------------------------------------------


def _fake_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_package_data: bool,
) -> Path:
    """Simulate a wheel layout: ``lib/python3.12/site-packages/saberops``.

    The module's own ``__file__`` decides where it looks, so pointing it at a
    directory with no repository ``tests/`` tree reproduces an installed build
    exactly.  The installed package never contains ``tests/``.
    """
    package = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "saberops"
    package.mkdir(parents=True)
    (package / "vendor_cli_fixtures.py").write_text("", encoding="utf-8")
    if with_package_data:
        data = package / "fixtures" / "vendor_cli"
        data.mkdir(parents=True)
        (data / "manifest.json").write_text(
            json.dumps({"version": 1, "fixtures": [], "fake_only": []}), encoding="utf-8"
        )
    monkeypatch.setattr(vcf, "__file__", str(package / "vendor_cli_fixtures.py"))
    monkeypatch.delenv(vcf.FIXTURES_ENV, raising=False)
    return package


def test_installed_build_without_a_manifest_is_a_typed_unavailable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6: a wheel has no ``tests/fixtures``; that is typed, never a traceback."""
    _fake_install(tmp_path, monkeypatch, with_package_data=False)
    assert vcf.resolve_fixtures_root() is None
    with pytest.raises(vcf.ManifestUnavailable) as caught:
        vcf.load_manifest()
    assert "vendor CLI fixture manifest unavailable" in str(caught.value)
    # The typed error names what it searched, so a bug report is actionable.
    assert any("site-packages" in str(path) for path in caught.value.searched)
    with pytest.raises(vcf.ManifestUnavailable):
        vcf.run_check(stream=io.StringIO())


def test_manifest_resolves_from_shipped_package_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6: a build that ships the captures finds them without a checkout."""
    package = _fake_install(tmp_path, monkeypatch, with_package_data=True)
    assert vcf.resolve_fixtures_root() == package / "fixtures" / "vendor_cli"
    assert vcf.load_manifest().fixtures == ()
    # The environment override wins over both defaults.
    override = tmp_path / "elsewhere"
    override.mkdir()
    (override / "manifest.json").write_text(
        json.dumps({"version": 1, "fixtures": [], "fake_only": [
            {"vendor": "codex", "kind": "usage_panel", "consumer": "c", "reason": "r"}
        ]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(vcf.FIXTURES_ENV, str(override))
    assert vcf.resolve_fixtures_root() == override
    assert len(vcf.load_manifest().fake_only) == 1


def test_preflight_reports_a_missing_manifest_as_one_plain_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D6: the owner sees one WARN with the remedy, never one traceback per tick."""
    _fake_install(tmp_path, monkeypatch, with_package_data=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    findings = preflight.collect_preflight(repo)
    warnings = [
        item for item in findings if item.message == "vendor CLI fixture manifest unavailable"
    ]
    assert len(warnings) == 1
    assert warnings[0].severity == "WARN"
    assert vcf.FIXTURES_ENV in warnings[0].remedy
