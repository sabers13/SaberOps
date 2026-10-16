"""Dated, redacted real vendor-CLI fixtures, drift detection and owner capture.

HV-1 (Step 023).  This is fixture tooling only.  It never imports a vendor SDK,
never sends a model prompt, never reads credentials, and never contacts a
vendor on its own: the only network-touching path is the owner-run ``capture``
subcommand, which goes through the EXISTING account readers.

Real captures live under ``tests/fixtures/vendor_cli/`` and are described by a
manifest (:data:`MANIFEST_PATH`) so that every parser assumption either traces
to a real capture or is recorded as a visible ``fake-only`` gap.  The three
behaviours are:

* :func:`read_installed_version` runs ``<cli> --version`` with a short timeout
  and returns the exact string or ``missing``.  It runs only when explicitly
  called (never at import time) and does not contact a vendor.
* :func:`detect_drift` compares the manifest's fixture versions against the
  installed versions and reports every CLI whose fixtures are stale.
* :func:`capture` (owner-run, token-free) records ``--version``, the
  ``--help`` text the adapters pin, and the native usage rows read through the
  existing readers, writing only outside the repository.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

__all__ = [
    "CaptureRecord",
    "Drift",
    "FakeOnly",
    "Fixture",
    "Manifest",
    "RedactionError",
    "assert_redacted",
    "capture",
    "detect_drift",
    "load_manifest",
    "main",
    "read_installed_version",
    "redact",
    "validate_manifest",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "vendor_cli"
MANIFEST_PATH = FIXTURES_ROOT / "manifest.json"

#: Fixture content kinds.  ``config_help`` feeds the adapter flag-pinning check;
#: the other three feed the pure parser functions named in ``consumer``.
KINDS: tuple[str, ...] = ("usage_panel", "usage_json", "exhaustion", "config_help")

#: Provenance of a committed fixture.  ``real-capture`` is a byte capture taken
#: by this project (e.g. the installed CLI's own ``--help``); ``owner-supplied-log``
#: is a real vendor-output log the owner supplied (e.g. collected in R-001).
SOURCES: tuple[str, ...] = ("real-capture", "owner-supplied-log")

#: Sentinel returned by :func:`read_installed_version` when no CLI is on PATH.
MISSING = "missing"

#: Sentinel used in a manifest entry when the capture's exact ``--version`` is
#: not recorded.  Drift cannot be assessed for such entries.
UNKNOWN_VERSION = "unknown"

VERSION_TIMEOUT = 5.0
HELP_TIMEOUT = 15.0

#: The installed CLI to invoke for each vendor this tooling captures.
VENDOR_CLI: Mapping[str, str] = {
    "codex": "codex",
    "claude": "claude",
    "opencode": "opencode",
}

#: Vendor -> provider name accepted by ``cli_usage.read_limits``.
USAGE_PROVIDER: Mapping[str, str] = {
    "codex": "codex",
    "claude": "anthropic",
    "opencode": "opencode",
}

#: Allowlisted help invocations per vendor.  Only these, plus ``--version`` and
#: the usage reader, are ever run by :func:`capture`.
HELP_COMMANDS: Mapping[str, tuple[tuple[str, ...], ...]] = {
    "codex": (("--help",), ("exec", "--help"), ("resume", "--help")),
    "claude": (("--help",), ("auth", "--help")),
    "opencode": (("--help",), ("run", "--help"), ("models", "--help")),
}


class RedactionError(ValueError):
    """Raised when text still carries account identity or a secret."""


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
TOKEN_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
)
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
    r"(\"?\s*[:=]\s*\"?)([A-Za-z0-9._\-]{12,})"
)
HEX_RE = re.compile(r"\b[A-Fa-f0-9]{32,}\b")
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
ID_RE = re.compile(r"\b(?:org|user|acct|account|session|proj)-[A-Za-z0-9]{12,}\b")
_SES_ID_RE = re.compile(r"\bses_[A-Za-z0-9]{10,}\b")
_B64_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")


def _base64_secret(text: str) -> str | None:
    """Return a base64-looking run that is plausibly a secret, else None.

    A 40+ character run of the base64 alphabet that is nothing but letters is
    almost always a word, so require a digit or a ``+``/``/``/``=`` character.
    """
    for match in _B64_RE.finditer(text):
        body = match.group(0)
        if any(not ch.isalpha() for ch in body):
            return body
    return None


def _default_user() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - defensive
        return ""


def _identity_patterns(home: str | None, user: str | None) -> tuple[str, str]:
    resolved_home = str(Path.home()) if home is None else home
    resolved_user = _default_user() if user is None else user
    return resolved_home, resolved_user


def _first_offence(text: str, home: str, user: str) -> str | None:
    if home and home in text:
        return f"home path {home!r}"
    if EMAIL_RE.search(text):
        return "email address"
    if any(pattern.search(text) for pattern in TOKEN_RES):
        return "API token"
    if _ASSIGNMENT_RE.search(text):
        return "assigned secret value"
    if HEX_RE.search(text):
        return "long hex secret"
    if UUID_RE.search(text):
        return "UUID account/session id"
    if ID_RE.search(text) or _SES_ID_RE.search(text):
        return "account/session id"
    if _base64_secret(text) is not None:
        return "long base64 secret"
    if user and re.search(
        rf"(?<![A-Za-z0-9_.\-]){re.escape(user)}(?![A-Za-z0-9_\-])", text
    ):
        return f"username {user!r}"
    return None


def assert_redacted(text: str, *, home: str | None = None, user: str | None = None) -> None:
    """Raise :class:`RedactionError` if ``text`` still carries identity or secrets.

    Rejects email addresses, ``sk-``/bearer/``ghp_``/``xox``-style tokens,
    assigned secret values, long hex/base64 secrets, UUID-like and ``org-``/
    ``ses_`` account/session ids, and the owner's home path or username.
    """
    resolved_home, resolved_user = _identity_patterns(home, user)
    offence = _first_offence(text, resolved_home, resolved_user)
    if offence is not None:
        raise RedactionError(f"text is not redacted: {offence}")


def redact(text: str, *, home: str | None = None, user: str | None = None) -> str:
    """Replace identity and secrets in ``text`` with stable placeholders.

    Numbers, reset labels and layout are preserved byte-for-byte: only the
    offending substrings change, so the parsers still read exactly what the
    CLI printed.
    """
    resolved_home, resolved_user = _identity_patterns(home, user)
    result = EMAIL_RE.sub("<EMAIL>", text)
    for pattern in TOKEN_RES:
        result = pattern.sub("<TOKEN>", result)
    result = _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<SECRET>", result)
    result = HEX_RE.sub("<HEX>", result)
    result = UUID_RE.sub("<UUID>", result)
    result = _SES_ID_RE.sub("<SESSION_ID>", result)
    result = ID_RE.sub("<ID>", result)
    while (secret := _base64_secret(result)) is not None:
        result = result.replace(secret, "<BASE64>")
    if resolved_home:
        result = result.replace(resolved_home, "<HOME>")
    if resolved_user:
        result = re.sub(
            rf"(?<![A-Za-z0-9_.\-]){re.escape(resolved_user)}(?![A-Za-z0-9_\-])",
            "<USER>",
            result,
        )
    return result


# ---------------------------------------------------------------------------
# Manifest model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    """One committed real fixture and the parser it feeds."""

    vendor: str
    cli: str
    cli_version: str
    captured_on: str
    kind: str
    source: str
    redacted: bool
    file: str
    consumer: str
    expect: Any
    provider: str | None = None
    now: str | None = None
    tz: str | None = None
    provenance: str | None = None


@dataclass(frozen=True)
class FakeOnly:
    """A parser assumption that still has no real fixture (visible gap)."""

    vendor: str
    kind: str
    consumer: str
    reason: str


@dataclass(frozen=True)
class Manifest:
    """Loaded fixture manifest: real fixtures plus explicit fake-only gaps."""

    fixtures: tuple[Fixture, ...]
    fake_only: tuple[FakeOnly, ...]
    version: int = 1

    def checked_clis(self) -> tuple[str, ...]:
        """CLIs whose versions can be compared with an installed CLI."""
        return tuple(
            sorted(
                {
                    fixture.cli
                    for fixture in self.fixtures
                    if fixture.cli_version != UNKNOWN_VERSION
                }
            )
        )


def load_manifest(path: str | Path = MANIFEST_PATH) -> Manifest:
    """Read a fixture manifest from ``path`` (default: the committed one)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    fixtures = tuple(
        Fixture(
            vendor=entry["vendor"],
            cli=entry["cli"],
            cli_version=entry["cli_version"],
            captured_on=entry["captured_on"],
            kind=entry["kind"],
            source=entry["source"],
            redacted=bool(entry["redacted"]),
            file=entry["file"],
            consumer=entry["consumer"],
            expect=entry.get("expect"),
            provider=entry.get("provider"),
            now=entry.get("now"),
            tz=entry.get("tz"),
            provenance=entry.get("provenance"),
        )
        for entry in data.get("fixtures", [])
    )
    fake_only = tuple(
        FakeOnly(
            vendor=entry["vendor"],
            kind=entry["kind"],
            consumer=entry["consumer"],
            reason=entry["reason"],
        )
        for entry in data.get("fake_only", [])
    )
    return Manifest(fixtures=fixtures, fake_only=fake_only, version=int(data.get("version", 1)))


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def validate_manifest(
    manifest: Manifest,
    *,
    fixtures_root: Path = FIXTURES_ROOT,
    home: str | None = None,
    user: str | None = None,
) -> list[str]:
    """Return a list of manifest errors (empty when the manifest is valid)."""
    errors: list[str] = []
    seen: set[str] = set()
    for fixture in manifest.fixtures:
        label = fixture.file or f"{fixture.vendor}/{fixture.kind}"
        if fixture.kind not in KINDS:
            errors.append(f"{label}: unknown kind {fixture.kind!r}")
        if fixture.source not in SOURCES:
            errors.append(f"{label}: unknown source {fixture.source!r}")
        if fixture.redacted is not True:
            errors.append(f"{label}: redacted must be true")
        if not fixture.cli_version:
            errors.append(f"{label}: cli_version is required")
        if not fixture.consumer:
            errors.append(f"{label}: consumer is required")
        try:
            date.fromisoformat(fixture.captured_on)
        except ValueError:
            errors.append(f"{label}: captured_on is not an ISO date: {fixture.captured_on!r}")
        if fixture.file in seen:
            errors.append(f"{label}: duplicate fixture file")
        seen.add(fixture.file)
        target = fixtures_root / fixture.file
        if not _is_inside(target, fixtures_root):
            errors.append(f"{label}: file escapes the fixtures root")
            continue
        if not target.is_file():
            errors.append(f"{label}: missing fixture file {fixture.file}")
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        try:
            assert_redacted(text, home=home, user=user)
        except RedactionError as exc:
            errors.append(f"{label}: {exc}")
    for gap in manifest.fake_only:
        if not gap.consumer or not gap.reason:
            errors.append(f"{gap.vendor}/{gap.kind}: fake-only needs consumer and reason")
    return errors


# ---------------------------------------------------------------------------
# Installed-version reader and drift detector
# ---------------------------------------------------------------------------


def read_installed_version(
    cli: str, *, which: Callable[[str], str | None] = shutil.which, timeout: float = VERSION_TIMEOUT
) -> str:
    """Return ``<cli> --version`` output, or ``missing`` when it cannot be read.

    Explicit call only; never runs at import time and never contacts a vendor.
    """
    executable = which(cli)
    if not executable:
        return MISSING
    try:
        process = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return MISSING
    text = (process.stdout or "").strip() or (process.stderr or "").strip()
    return text or MISSING


@dataclass(frozen=True)
class Drift:
    """One CLI whose committed fixtures do not match the installed version."""

    cli: str
    vendor: str
    installed_version: str
    expected_versions: tuple[str, ...]
    fixtures: tuple[str, ...]


def detect_drift(manifest: Manifest, installed: Mapping[str, str]) -> list[Drift]:
    """Compare fixture versions with ``installed`` (cli -> version or missing)."""
    by_cli: dict[str, list[Fixture]] = {}
    for fixture in manifest.fixtures:
        if fixture.cli_version == UNKNOWN_VERSION:
            continue
        by_cli.setdefault(fixture.cli, []).append(fixture)
    drifts: list[Drift] = []
    for cli, fixtures in sorted(by_cli.items()):
        installed_version = installed.get(cli, MISSING)
        stale = [fx for fx in fixtures if fx.cli_version != installed_version]
        if not stale:
            continue
        drifts.append(
            Drift(
                cli=cli,
                vendor=stale[0].vendor,
                installed_version=installed_version,
                expected_versions=tuple(sorted({fx.cli_version for fx in fixtures})),
                fixtures=tuple(fx.file for fx in stale),
            )
        )
    return drifts


# ---------------------------------------------------------------------------
# Owner-run capture (token-free)
# ---------------------------------------------------------------------------

RunFn = Callable[[Sequence[str], float], tuple[int, str, str]]
ReaderFn = Callable[[str, str], "list[dict[str, Any]]"]


def _default_run(cmd: Sequence[str], timeout: float) -> tuple[int, str, str]:
    try:
        process = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)
    return process.returncode, process.stdout or "", process.stderr or ""


def _slug(version: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9.]+", "-", version).strip("-").lower()
    return slug or "unknown"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@dataclass(frozen=True)
class CaptureRecord:
    """Outcome of capturing one vendor."""

    vendor: str
    cli: str
    status: str
    version: str
    out_dir: str
    usage_rows: int
    files: tuple[str, ...] = field(default_factory=tuple)


def _capture_vendor(
    vendor: str,
    exe: str,
    out: Path,
    run: RunFn,
    read_limits: ReaderFn,
    day: str,
) -> CaptureRecord:
    cli = VENDOR_CLI[vendor]
    code, stdout, stderr = run([exe, "--version"], VERSION_TIMEOUT)
    version = (stdout.strip() or stderr.strip()) or MISSING
    base = out / vendor / _slug(version)
    artifacts: dict[str, str] = {"version.txt": f"$ {cli} --version\n{version}\n"}

    help_parts: list[str] = []
    for command in HELP_COMMANDS[vendor]:
        help_code, help_out, help_err = run([exe, *command], HELP_TIMEOUT)
        help_parts.append(
            f"$ {cli} {' '.join(command)} (exit {help_code})\n{help_out or help_err}"
        )
    artifacts["config_help.txt"] = "\n".join(help_parts) + "\n"

    rows: list[dict[str, Any]] = []
    try:
        rows = list(read_limits(exe, USAGE_PROVIDER[vendor]))
    except Exception as exc:  # capture records the failure instead of hiding it
        artifacts["usage_error.txt"] = f"{type(exc).__name__}: {exc}\n"
    artifacts["usage_reader.json"] = json.dumps(rows, indent=2, sort_keys=True) + "\n"

    written: list[str] = []
    for name, text in artifacts.items():
        raw_path = base / "raw" / name
        redacted_path = base / "redacted" / name
        _write(raw_path, text)
        _write(redacted_path, redact(text))
        assert_redacted(redacted_path.read_text(encoding="utf-8"))
        written.extend((str(raw_path), str(redacted_path)))

    log_lines = [
        f"captured_on: {day}",
        f"vendor: {vendor}",
        f"cli: {cli}",
        f"version: {version}",
        "commands:",
        f"  $ {cli} --version (exit {code})",
    ]
    log_lines.extend(
        f"  $ {cli} {' '.join(cmd)} (allowlisted help)" for cmd in HELP_COMMANDS[vendor]
    )
    log_lines.append(f"  usage reader -> {len(rows)} row(s) (no inference prompt)")
    log_path = base / "capture_log.txt"
    _write(log_path, "\n".join(log_lines) + "\n")
    written.append(str(log_path))
    return CaptureRecord(
        vendor=vendor,
        cli=cli,
        status="captured",
        version=version,
        out_dir=str(base),
        usage_rows=len(rows),
        files=tuple(written),
    )


def capture(
    vendor: str,
    out_dir: str | Path,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: RunFn = _default_run,
    read_limits: ReaderFn | None = None,
    today: str | None = None,
    repo_root: Path = REPO_ROOT,
) -> list[CaptureRecord]:
    """Capture allowlisted, token-free vendor output into ``out_dir``.

    ``vendor`` is ``codex``, ``claude``, ``opencode`` or ``all``.  ``out_dir``
    must be outside the repository.  Only ``--version``, the allowlisted
    ``--help`` invocations and the existing usage readers are ever run.
    """
    if vendor not in (*VENDOR_CLI, "all"):
        raise ValueError(f"unknown vendor {vendor!r}")
    if read_limits is None:
        from saberops.cli_usage import read_limits as _read_limits

        read_limits = _read_limits
    if _is_inside(Path(out_dir), repo_root):
        raise ValueError("--out must be a directory outside the repository")
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    targets = tuple(VENDOR_CLI) if vendor == "all" else (vendor,)
    day = today or date.today().isoformat()
    records: list[CaptureRecord] = []
    for target in targets:
        cli = VENDOR_CLI[target]
        executable = which(cli)
        if not executable:
            records.append(
                CaptureRecord(
                    vendor=target,
                    cli=cli,
                    status=MISSING,
                    version=MISSING,
                    out_dir=str(out / target),
                    usage_rows=0,
                )
            )
            continue
        records.append(_capture_vendor(target, executable, out, run, read_limits, day))
    return records


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------


def run_check(
    manifest_path: str | Path = MANIFEST_PATH,
    *,
    which: Callable[[str], str | None] = shutil.which,
    stream: Any = None,
) -> int:
    """Print drift records; return non-zero when any fixture is stale."""
    out = stream if stream is not None else sys.stdout
    manifest = load_manifest(manifest_path)
    installed = {
        cli: read_installed_version(cli, which=which) for cli in manifest.checked_clis()
    }
    drifts = detect_drift(manifest, installed)
    for drift in drifts:
        print(
            f"DRIFT {drift.vendor} ({drift.cli}): fixture"
            f"{'s' if len(drift.expected_versions) > 1 else ''} "
            f"{', '.join(drift.expected_versions)} != installed "
            f"{drift.installed_version}; files: {', '.join(drift.fixtures)}",
            file=out,
        )
    if not drifts:
        checked = ", ".join(
            f"{cli}={installed[cli]}" for cli in sorted(installed)
        )
        print(f"no drift: {len(manifest.fixtures)} fixture(s) match ({checked})", file=out)
    return 1 if drifts else 0


def _print_capture(records: Sequence[CaptureRecord], stream: Any = None) -> None:
    out = stream if stream is not None else sys.stdout
    for record in records:
        print(
            f"{record.status}: {record.vendor} ({record.cli}) "
            f"version={record.version!r} usage_rows={record.usage_rows} -> {record.out_dir}",
            file=out,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m saberops.vendor_cli_fixtures`` entry point."""
    parser = argparse.ArgumentParser(prog="python -m saberops.vendor_cli_fixtures")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check", help="report installed-version drift")
    check_parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    capture_parser = subparsers.add_parser("capture", help="capture token-free vendor output")
    capture_parser.add_argument("--vendor", choices=(*VENDOR_CLI, "all"), required=True)
    capture_parser.add_argument("--out", required=True, help="output directory outside the repo")
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args.manifest)
    records = capture(args.vendor, args.out)
    _print_capture(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
