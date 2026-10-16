"""Vendor CLI fixtures: manifest, redaction, drift detection, capture (HV-1).

Every test runs against stub binaries on a temporary PATH.  No test resolves a
real vendor CLI, sends a prompt, or contacts a vendor.
"""

from __future__ import annotations

import getpass
import io
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from saberops import vendor_cli_fixtures as vcf
from saberops.vendor_cli_fixtures import (
    CHECK_INVALID_MANIFEST_EXIT,
    HELP_COMMANDS,
    MANIFEST_PATH,
    MISSING,
    UNKNOWN_VERSION,
    FakeOnly,
    Fixture,
    Manifest,
    RedactionError,
    assert_redacted,
    capture,
    detect_drift,
    load_manifest,
    main,
    read_installed_version,
    redact,
    run_check,
    validate_manifest,
)


def _stub_binary(directory: Path, name: str) -> Path:
    """Write an executable that logs its argv and prints a stub version."""
    path = directory / name
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['HV1_ARGV_LOG'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print(os.path.basename(sys.argv[0]) + ' 1.0')\n"
        "else:\n"
        "    print('stub help for ' + ' '.join(sys.argv[1:]))\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_committed_manifest_is_valid_and_every_file_exists() -> None:
    """Covers: HV-1 manifest validation (required fields + real files)."""
    manifest = load_manifest(MANIFEST_PATH)
    assert validate_manifest(manifest) == []
    assert manifest.fixtures, "at least one real fixture must be committed"
    for fixture in manifest.fixtures:
        assert (vcf.FIXTURES_ROOT / fixture.file).is_file()
        assert fixture.cli_version
        assert fixture.redacted is True


def test_manifest_records_visible_fake_only_gaps() -> None:
    """Covers: HV-1 fake-only gap listing."""
    manifest = load_manifest(MANIFEST_PATH)
    consumers = {gap.consumer for gap in manifest.fake_only}
    # The usage and exhaustion parsers for every installed CLI are recorded fake-only.
    assert "saberops.cli_usage.parse_limits" in consumers
    assert "saberops.native_usage_panel.parse_panel" in consumers
    assert "saberops.opencode_go_usage.parse_go_limits" in consumers
    for vendor, consumer in (
        ("codex", "saberops.workers.exhaustion.classify_codex"),
        ("claude", "saberops.workers.exhaustion.classify_claude"),
        ("opencode", "saberops.workers.exhaustion.classify_opencode"),
    ):
        matches = [
            gap
            for gap in manifest.fake_only
            if gap.vendor == vendor and gap.kind == "exhaustion" and gap.consumer == consumer
        ]
        assert len(matches) == 1
        assert ".orch/R-001/findings.md" in matches[0].reason
        assert "D-0016" in matches[0].reason
    # Antigravity has no installed CLI, so it stays fake-only.
    assert any(gap.vendor == "antigravity" for gap in manifest.fake_only)


def test_validate_reports_missing_file_and_bad_kind(tmp_path: Path) -> None:
    """Covers: HV-1 manifest validation errors."""
    manifest = Manifest(
        fixtures=(
            Fixture(
                vendor="codex",
                cli="codex",
                cli_version="codex-cli 0.0.1",
                captured_on="2026-10-16",
                kind="not-a-kind",
                source="real-capture",
                redacted=True,
                file="codex/nope/missing.txt",
                consumer="saberops.cli_usage.parse_limits",
                expect={},
            ),
        ),
        fake_only=(),
    )
    errors = validate_manifest(manifest, fixtures_root=tmp_path)
    assert any("unknown kind" in e for e in errors)
    assert any("missing fixture file" in e for e in errors)


def test_load_manifest_reads_fake_only(tmp_path: Path) -> None:
    """Covers: HV-1 manifest loader."""
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "fixtures": [],
                "fake_only": [
                    {
                        "vendor": "cline",
                        "kind": "exhaustion",
                        "consumer": "saberops.workers.exhaustion.classify_cline",
                        "reason": "no exact signal",
                    }
                ],
            }
        )
    )
    manifest = load_manifest(path)
    assert manifest.fake_only == (
        FakeOnly(
            vendor="cline",
            kind="exhaustion",
            consumer="saberops.workers.exhaustion.classify_cline",
            reason="no exact signal",
        ),
    )
    assert manifest.checked_clis() == ()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "reach me at owner@example.com",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "key sk-abcdefghijklmnopqrstuvwx",
        "org token ghp_abcdefghijklmnopqrstuvwxyz01",
        "hex 0123456789abcdef0123456789abcdef",
        "uuid 12345678-1234-1234-1234-123456789abc",
        "account org-abcdefghijklmnop",
        "session ses_abcdefghijklmnop",
        'api_key: "abcdefghijklmnop"',
    ],
)
def test_assert_redacted_rejects_identity_and_secrets(text: str) -> None:
    """Covers: HV-1 redaction rejection of emails, tokens, ids."""
    with pytest.raises(RedactionError):
        assert_redacted(text, user="")


def test_assert_redacted_accepts_help_text_and_preserves_words() -> None:
    """Covers: HV-1 redaction accepts ordinary CLI text."""
    sample = (
        "Resets Oct 9, 9am (Europe/Berlin)\n"
        "Created 22% used\n"
        "-c, --config <key=value>  Override ~/.codex/config.toml\n"
        "disk-full-read-access is a sandbox permission\n"
    )
    assert_redacted(sample)  # must not raise
    # A product word containing the username must not be flagged.
    assert_redacted("saberops console", user="saber")


def test_redact_replaces_but_keeps_numbers_and_labels() -> None:
    """Covers: HV-1 redaction placeholders keep parser-visible content."""
    assert redact("mail a@b.com", user="") == "mail <EMAIL>"
    assert redact("token sk-abcdefghijklmnopqrstuvwx", user="") == "token <TOKEN>"
    assert redact("/home/owner/projects", home="/home/owner", user="") == "<HOME>/projects"
    keep = "Resets Oct 9, 9am (Europe/Berlin)\nCreated 22% used"
    assert redact(keep, home="/home/owner", user="owner") == keep
    once = redact("a@b.com 0123456789abcdef0123456789abcdef", user="")
    assert redact(once, user="") == once


def test_redact_masks_owner_username_and_home() -> None:
    """Covers: HV-1 redaction of the owner's home path and username."""
    home = str(Path.home())
    user = getpass.getuser()
    masked = redact(f"cwd={home}/x user={user}", home=home, user=user)
    assert home not in masked and user not in masked
    assert "<HOME>" in masked and "<USER>" in masked
    assert_redacted(masked, home=home, user=user)


# ---------------------------------------------------------------------------
# Installed-version reader
# ---------------------------------------------------------------------------


def test_read_installed_version_missing_and_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: HV-1 installed-version reader (present and missing)."""
    assert read_installed_version("codex", which=lambda _name: None) == MISSING
    stub = _stub_binary(tmp_path, "codex")
    monkeypatch.setenv("HV1_ARGV_LOG", str(tmp_path / "argv.log"))
    assert read_installed_version("codex", which=lambda _name: str(stub)) == "codex 1.0"


# ---------------------------------------------------------------------------
# Drift detector and check command
# ---------------------------------------------------------------------------


def _fixture(cli: str, version: str, file: str = "x/y.txt") -> Fixture:
    return Fixture(
        vendor=cli,
        cli=cli,
        cli_version=version,
        captured_on="2026-10-16",
        kind="config_help",
        source="real-capture",
        redacted=True,
        file=file,
        consumer="saberops.workers.codex:CodexAdapter",
        expect={},
    )


def test_detect_drift_match_differ_and_missing() -> None:
    """Covers: HV-1 drift detector match/differ/missing."""
    manifest = Manifest(fixtures=(_fixture("codex", "codex 1.0"),), fake_only=())
    assert detect_drift(manifest, {"codex": "codex 1.0"}) == []
    drift = detect_drift(manifest, {"codex": "codex 2.0"})
    assert len(drift) == 1
    assert drift[0].vendor == "codex"
    assert drift[0].installed_version == "codex 2.0"
    assert drift[0].expected_versions == ("codex 1.0",)
    missing = detect_drift(manifest, {})
    assert missing[0].installed_version == MISSING


def test_detect_drift_skips_unknown_versions() -> None:
    """Covers: HV-1 drift detector ignores unversioned owner logs."""
    manifest = Manifest(
        fixtures=(_fixture("codex", UNKNOWN_VERSION),),
        fake_only=(),
    )
    assert detect_drift(manifest, {}) == []
    assert manifest.checked_clis() == ()


def _write_manifest(tmp_path: Path, version: str) -> Path:
    """Write a *valid* manifest and the fixture file it names.

    ``run_check`` validates the manifest first (HV-2), so the drift tests need
    a manifest whose fixture file exists and is redaction-clean.
    """
    fixture = tmp_path / "help" / "x" / "config_help.txt"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("$ codex --help (exit 0)\nstub help for codex\n", encoding="utf-8")
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
                        "captured_on": "2026-10-16",
                        "kind": "config_help",
                        "source": "real-capture",
                        "redacted": True,
                        "file": "help/x/config_help.txt",
                        "consumer": "saberops.workers.codex:CodexAdapter",
                        "expect": {"flags": []},
                    }
                ],
                "fake_only": [],
            }
        )
    )
    return path


def test_check_exit_codes_for_match_and_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: HV-1 `check` exit codes on match and drift."""
    monkeypatch.setenv("HV1_ARGV_LOG", str(tmp_path / "argv.log"))
    stub = _stub_binary(tmp_path, "codex")  # prints "codex 1.0"
    which = lambda _name: str(stub)  # noqa: E731
    match = run_check(_write_manifest(tmp_path, "codex 1.0"), which=which, stream=_Null())
    assert match == 0
    drift = run_check(_write_manifest(tmp_path, "codex 0.9"), which=which, stream=_Null())
    assert drift == 1


def test_check_refuses_an_invalid_manifest_without_running_drift(
    tmp_path: Path,
) -> None:
    """Covers: HV-2 `check` validates the manifest before the drift run."""
    probes: list[str] = []

    def which(name: str) -> str | None:
        probes.append(name)
        return None

    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "fixtures": [
                    {
                        "vendor": "codex",
                        "cli": "codex",
                        "cli_version": "codex 1.0",
                        "captured_on": "2026-10-16",
                        "kind": "not-a-kind",
                        "source": "real-capture",
                        "redacted": True,
                        "file": "codex/x/config_help.txt",
                        "consumer": "saberops.workers.codex:CodexAdapter",
                        "expect": {"flags": []},
                    }
                ],
                "fake_only": [],
            }
        )
    )
    stream = io.StringIO()
    exit_code = run_check(path, which=which, stream=stream)
    assert exit_code == CHECK_INVALID_MANIFEST_EXIT
    assert exit_code != 0
    assert "INVALID manifest" in stream.getvalue()
    assert probes == [], "drift must not probe versions for an invalid manifest"


class _Null:
    def write(self, _text: str) -> int:  # pragma: no cover - trivial
        return 0


# ---------------------------------------------------------------------------
# Owner-run capture (against stubs only)
# ---------------------------------------------------------------------------


def _run_capture_against_stubs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[vcf.CaptureRecord], list[list[str]], Path]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name in ("codex", "claude", "opencode"):
        _stub_binary(stub_dir, name)
    argv_log = tmp_path / "argv.log"
    monkeypatch.setenv("HV1_ARGV_LOG", str(argv_log))

    reader_calls: list[list[str]] = []

    def fake_reader(exe: str, provider: str) -> list[dict[str, Any]]:
        reader_calls.append(["reader", exe, provider])
        return [{"label": f"{provider} · primary", "remaining": 60.0}]

    out = tmp_path / "capture-out"
    records = capture(
        "all",
        out,
        which=lambda name: str(stub_dir / name) if (stub_dir / name).exists() else None,
        read_limits=fake_reader,
    )
    logged = [json.loads(line) for line in argv_log.read_text().splitlines()]
    return records, logged + reader_calls, out


def test_capture_runs_only_allowlisted_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: HV-1 capture allowlist (stub argv log)."""
    records, logged, out = _run_capture_against_stubs(tmp_path, monkeypatch)
    allow: set[tuple[str, ...]] = {("--version",)}
    for commands in HELP_COMMANDS.values():
        allow.update(commands)
    for argv in logged:
        if argv and argv[0] == "reader":
            continue
        assert tuple(argv) in allow, f"capture ran a non-allowlisted command: {argv}"
    assert all(record.status == "captured" for record in records)
    assert out.is_dir()


def test_capture_writes_raw_and_redacted_only_under_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: HV-1 capture writes only to --out (raw + redacted + log)."""
    before = sorted(p.name for p in vcf.FIXTURES_ROOT.iterdir())
    records, _logged, out = _run_capture_against_stubs(tmp_path, monkeypatch)
    after = sorted(p.name for p in vcf.FIXTURES_ROOT.iterdir())
    assert before == after  # nothing was written into the repository
    for record in records:
        assert Path(record.out_dir).resolve().is_relative_to(out.resolve())
    produced = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert any("/raw/version.txt" in name for name in produced)
    assert any("/redacted/config_help.txt" in name for name in produced)
    assert any("capture_log.txt" in name for name in produced)
    for path in out.rglob("redacted/*"):
        if path.is_file():
            assert_redacted(path.read_text())


def test_capture_refuses_a_repository_out_dir(tmp_path: Path) -> None:
    """Covers: HV-1 capture never writes into the repository."""
    with pytest.raises(ValueError, match="outside the repository"):
        capture("codex", vcf.REPO_ROOT / "capture-should-fail", read_limits=lambda *_: [])


def test_capture_missing_executable_is_reported(tmp_path: Path) -> None:
    """Covers: HV-1 capture records a missing CLI instead of failing."""
    records = capture(
        "codex",
        tmp_path / "out",
        which=lambda _name: None,
        read_limits=lambda *_: [],
    )
    assert records[0].status == MISSING
    assert not (tmp_path / "out" / "codex").exists()


def test_main_dispatches_capture_and_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: HV-1 module CLI wiring."""
    calls: list[tuple[str, str]] = []

    def fake_capture(vendor: str, out: str, **_kwargs: Any) -> list[vcf.CaptureRecord]:
        calls.append((vendor, out))
        return []

    monkeypatch.setattr(vcf, "capture", fake_capture)
    assert main(["capture", "--vendor", "codex", "--out", str(tmp_path)]) == 0
    assert calls == [("codex", str(tmp_path))]


def test_usage_providers_and_help_commands_are_declared() -> None:
    """Covers: HV-1 capture mapping matches the readers' provider names."""
    assert vcf.USAGE_PROVIDER == {"codex": "codex", "claude": "anthropic", "opencode": "opencode"}
    assert set(vcf.HELP_COMMANDS) == set(vcf.VENDOR_CLI)
