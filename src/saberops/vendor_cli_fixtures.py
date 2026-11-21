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
* :func:`check_version_support` runs the allowlisted ``--help`` invocations
  only when a version drift was observed and reports whether the adapter's
  pinned flags are still documented.  It never sends a prompt; when a help
  capture is incomplete the result is ``unverified``, never ``unsupported``.
* :func:`capture` (owner-run, token-free) records ``--version``, the
  ``--help`` text the adapters pin, and the native usage rows read through the
  existing readers, writing only outside the repository.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "CHECK_INVALID_MANIFEST_EXIT",
    "FIXTURES_ENV",
    "CaptureRecord",
    "Drift",
    "FakeOnly",
    "Fixture",
    "Manifest",
    "ManifestUnavailable",
    "RedactionError",
    "VersionCheck",
    "VersionSupport",
    "assert_redacted",
    "capture",
    "check_version_support",
    "default_manifest_path",
    "detect_drift",
    "fixture_root_candidates",
    "load_manifest",
    "main",
    "read_installed_version",
    "redact",
    "resolve_fixtures_root",
    "validate_manifest",
]

#: Source-checkout root. Only meaningful in a checkout: an installed wheel has
#: no ``tests/`` tree, so the manifest is resolved through
#: :func:`resolve_fixtures_root` instead of this path.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Optional explicit fixtures directory (an escape hatch for a relocated
#: checkout or a package that ships the dated captures elsewhere).
FIXTURES_ENV = "SABEROPS_VENDOR_FIXTURES"


class ManifestUnavailable(LookupError):
    """The dated vendor-CLI fixture manifest is not reachable from this install.

    A source checkout carries it under ``tests/fixtures/vendor_cli``; an
    installed build only carries it when the fixtures are shipped as package
    data. The error is typed so every caller can degrade to an explicit
    "unavailable" state instead of failing deep inside a scheduler tick.
    """

    def __init__(self, message: str, *, searched: Sequence[Path] = ()) -> None:
        self.searched = tuple(searched)
        detail = ""
        if self.searched:
            detail = " (searched: " + ", ".join(str(item) for item in self.searched) + ")"
        super().__init__(message + detail)


def fixture_root_candidates() -> tuple[Path, ...]:
    """Directories that may hold ``manifest.json``, most specific first."""
    module_root = Path(__file__).resolve().parent
    candidates: list[Path] = []
    override = os.environ.get(FIXTURES_ENV, "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    # A package that ships the captures as package data resolves here.
    candidates.append(module_root / "fixtures" / "vendor_cli")
    # A source checkout: src/saberops -> repository root -> tests/fixtures.
    candidates.append(module_root.parents[1] / "tests" / "fixtures" / "vendor_cli")
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return tuple(unique)


def resolve_fixtures_root() -> Path | None:
    """Return the first candidate that actually holds a manifest, else ``None``."""
    for candidate in fixture_root_candidates():
        if (candidate / "manifest.json").is_file():
            return candidate
    return None


def default_manifest_path() -> Path:
    """Return the committed manifest path or raise :class:`ManifestUnavailable`."""
    root = resolve_fixtures_root()
    if root is None:
        raise ManifestUnavailable(
            "vendor CLI fixture manifest unavailable: this installation ships no "
            "dated fixture capture",
            searched=fixture_root_candidates(),
        )
    return root / "manifest.json"


FIXTURES_ROOT = resolve_fixtures_root() or fixture_root_candidates()[-1]
MANIFEST_PATH = FIXTURES_ROOT / "manifest.json"

#: Fixture content kinds.  ``config_help`` feeds the adapter flag-pinning check;
#: the other kinds feed the pure parser functions named in ``consumer``.
#: ``server_openapi`` pins the shipped local-server route surface
#: (``GET /openapi.json``) for CLIs whose adapter speaks a server API.
KINDS: tuple[str, ...] = (
    "usage_panel",
    "usage_json",
    "exhaustion",
    "config_help",
    "server_openapi",
)

#: Provenance of a committed fixture.  ``real-capture`` is a byte capture taken
#: by this project (e.g. the installed CLI's own ``--help``); ``owner-supplied-log``
#: is a redacted real vendor-output log supplied by the owner.
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
    "opencode": (("--help",), ("run", "--help"), ("models", "--help"), ("serve", "--help")),
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


def load_manifest(path: str | Path | None = None) -> Manifest:
    """Read a fixture manifest from ``path`` (default: the resolved committed one).

    Raises :class:`ManifestUnavailable` when the default manifest cannot be
    found, so callers can report a typed "unavailable" instead of a traceback.
    """
    target = default_manifest_path() if path is None else Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestUnavailable(
            f"vendor CLI fixture manifest unavailable: {target} does not exist",
            searched=(target.parent,),
        ) from exc
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
# Version-change guard: allowlisted help check
# ---------------------------------------------------------------------------

#: One allowlisted invocation: (exit code, stdout, stderr).
RunFn = Callable[[Sequence[str], float], tuple[int, str, str]]
ReaderFn = Callable[[str, str], "list[dict[str, Any]]"]


class VersionSupport(StrEnum):
    """Outcome of checking a drifted CLI against the adapter's pinned flags.

    ``SUPPORTED`` and ``UNSUPPORTED`` need positive evidence from a complete
    allowlisted ``--help`` capture.  ``UNVERIFIED`` is the fail-open state: the
    CLI could not be probed, so nothing is known and dispatch must not treat
    the CLI as unsupported on a guess.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class VersionCheck:
    """Result of the version-change guard for one CLI."""

    cli: str
    vendor: str
    installed_version: str
    fixture_versions: tuple[str, ...]
    state: VersionSupport
    missing: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    detail: str = ""


def _version_shape(version: str) -> re.Pattern[str]:
    """Regex matching ``version`` with every number run treated as a number.

    ``codex-cli 0.162.1`` becomes ``^codex-cli \\d+\\.\\d+\\.\\d+$``.  The
    recorded format is what makes a genuine vendor update recognisable; a
    wrapper or fake CLI that answers ``--version`` with a different shape is
    never probed for help.
    """
    parts = re.split(r"\d+", version)
    return re.compile("^" + r"\d+".join(re.escape(part) for part in parts) + "$")


def _expected_flags(fixtures: Sequence[Fixture]) -> tuple[str, ...]:
    """Union of the adapter-pinned flags recorded on ``config_help`` fixtures."""
    flags: list[str] = []
    for fixture in fixtures:
        if fixture.kind != "config_help" or not isinstance(fixture.expect, Mapping):
            continue
        for flag in fixture.expect.get("flags", []):
            if isinstance(flag, str) and flag not in flags:
                flags.append(flag)
    return tuple(flags)


def check_version_support(
    manifest: Manifest,
    cli: str,
    installed_version: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: RunFn | None = None,
    timeout: float = HELP_TIMEOUT,
) -> VersionCheck:
    """Check a drifted CLI against the allowlisted ``--help`` fixtures.

    Called only when :func:`detect_drift` reported a version change.  It runs
    exactly the ``HELP_COMMANDS`` allowlist for the vendor (no prompt, no
    vendor setting, no credential) and returns:

    * ``SUPPORTED`` when every recorded adapter flag still appears in a
      complete help capture;
    * ``UNSUPPORTED`` when a complete capture omits one or more recorded
      flags — the clear unsupported-version state;
    * ``UNVERIFIED`` when no complete capture could be read (CLI missing,
      probe failed, no recorded flags), so nothing is inferred from a guess.
    """
    fixtures = tuple(
        fixture
        for fixture in manifest.fixtures
        if fixture.cli == cli and fixture.cli_version != UNKNOWN_VERSION
    )
    versions = tuple(sorted({fixture.cli_version for fixture in fixtures}))
    if not fixtures:
        return VersionCheck(
            cli=cli,
            vendor=cli,
            installed_version=installed_version,
            fixture_versions=(),
            state=VersionSupport.UNVERIFIED,
            detail="no versioned fixture for this CLI",
        )
    vendor = fixtures[0].vendor
    if installed_version == MISSING:
        return VersionCheck(
            cli=cli,
            vendor=vendor,
            installed_version=installed_version,
            fixture_versions=versions,
            state=VersionSupport.UNVERIFIED,
            detail="CLI not installed or version unreadable",
        )
    if installed_version in versions:
        return VersionCheck(
            cli=cli,
            vendor=vendor,
            installed_version=installed_version,
            fixture_versions=versions,
            state=VersionSupport.SUPPORTED,
            detail="fixtures match the installed version",
        )
    if not any(_version_shape(recorded).match(installed_version) for recorded in versions):
        return VersionCheck(
            cli=cli,
            vendor=vendor,
            installed_version=installed_version,
            fixture_versions=versions,
            state=VersionSupport.UNVERIFIED,
            detail=(
                "installed version does not match the recorded vendor version "
                "format; help probe skipped"
            ),
        )
    commands = HELP_COMMANDS.get(vendor)
    if not commands:
        return VersionCheck(
            cli=cli,
            vendor=vendor,
            installed_version=installed_version,
            fixture_versions=versions,
            state=VersionSupport.UNVERIFIED,
            detail=f"no allowlisted help commands for {vendor!r}",
        )
    expected = _expected_flags(
        tuple(fixture for fixture in fixtures if fixture.cli_version != installed_version)
    )
    if not expected:
        return VersionCheck(
            cli=cli,
            vendor=vendor,
            installed_version=installed_version,
            fixture_versions=versions,
            state=VersionSupport.UNVERIFIED,
            commands=tuple(" ".join(command) for command in commands),
            detail="no recorded adapter flags to check",
        )
    executable = which(cli)
    if not executable:
        return VersionCheck(
            cli=cli,
            vendor=vendor,
            installed_version=installed_version,
            fixture_versions=versions,
            state=VersionSupport.UNVERIFIED,
            commands=tuple(" ".join(command) for command in commands),
            detail="CLI executable not found",
        )
    runner = run or _default_run
    outputs: list[str] = []
    for command in commands:
        try:
            code, stdout, stderr = runner([executable, *command], timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return VersionCheck(
                cli=cli,
                vendor=vendor,
                installed_version=installed_version,
                fixture_versions=versions,
                state=VersionSupport.UNVERIFIED,
                commands=tuple(" ".join(item) for item in commands),
                detail=f"help capture failed: {type(exc).__name__}",
            )
        text = (stdout or stderr).strip()
        if code != 0 or not text:
            return VersionCheck(
                cli=cli,
                vendor=vendor,
                installed_version=installed_version,
                fixture_versions=versions,
                state=VersionSupport.UNVERIFIED,
                commands=tuple(" ".join(item) for item in commands),
                detail=f"help capture failed: {cli} {' '.join(command)} (exit {code})",
            )
        outputs.append(stdout or stderr)
    combined = "\n".join(outputs)
    missing = tuple(flag for flag in expected if flag not in combined)
    state = VersionSupport.UNSUPPORTED if missing else VersionSupport.SUPPORTED
    detail = (
        "recorded adapter flags are absent from --help"
        if missing
        else "recorded adapter flags are still documented"
    )
    return VersionCheck(
        cli=cli,
        vendor=vendor,
        installed_version=installed_version,
        fixture_versions=versions,
        state=state,
        missing=missing,
        commands=tuple(" ".join(command) for command in commands),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Server API shape check (opencode local server)
# ---------------------------------------------------------------------------

#: How long the token-free ``opencode serve`` probe may take, in seconds.
SERVER_PROBE_TIMEOUT = 30.0


def server_api_drift(
    fixture_document: Mapping[str, Any],
    live_document: Mapping[str, Any],
    *,
    required_paths: Sequence[str],
    forbidden_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return human-readable API-shape drift records (empty when aligned).

    Compares the route keys the adapter pins against the live server's
    ``/openapi.json`` and against the committed fixture.  Pure: both documents
    are passed in, so the check is unit-tested without launching a server.
    """
    fixture_paths = fixture_document.get("paths")
    live_paths = live_document.get("paths")
    fixture_paths = fixture_paths if isinstance(fixture_paths, Mapping) else {}
    live_paths = live_paths if isinstance(live_paths, Mapping) else {}
    problems: list[str] = []
    for path in required_paths:
        if path not in live_paths:
            problems.append(f"required route missing from installed server: {path}")
        if path not in fixture_paths:
            problems.append(f"required route missing from committed fixture: {path}")
    for path in forbidden_paths:
        if path in live_paths:
            problems.append(f"forbidden route present on installed server: {path}")
    if not fixture_paths:
        problems.append("committed fixture has no paths")
    return tuple(problems)


def opencode_server_openapi(
    executable: str, *, timeout: float = SERVER_PROBE_TIMEOUT
) -> dict[str, Any]:
    """Start ``opencode serve`` privately and return its ``/openapi.json``.

    Token-free: no prompt is sent and no credential file is read.  The child
    gets a disposable private XDG root (so project/global config cannot leak
    in), a random basic-auth password, and is always terminated afterwards.
    """
    import select
    import tempfile
    import time
    import urllib.request

    root = Path(tempfile.mkdtemp(prefix="saberops-openapi-probe-"))
    password = secrets.token_urlsafe(32)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root / "home"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_STATE_HOME": str(root / "state"),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_SERVER_PASSWORD": password,
        "OPENCODE_SERVER_USERNAME": "opencode",
    }
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [executable, "serve", "--hostname", "127.0.0.1", "--port", "0"],
            cwd=str(root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        endpoint = ""
        buffer = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not endpoint:
            ready, _, _ = select.select([process.stdout], [], [], 0.2)
            if not ready:
                if process.poll() is not None:
                    raise RuntimeError("opencode serve exited before printing its URL")
                continue
            line = process.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            buffer += line
            match = re.search(r"http://127\.0\.0\.1:\d+", buffer)
            if match:
                endpoint = match.group(0)
        if not endpoint:
            raise RuntimeError("opencode serve did not report a local endpoint")
        token = base64.b64encode(("opencode:" + password).encode()).decode()
        request = urllib.request.Request(
            endpoint + "/openapi.json", headers={"Authorization": "Basic " + token}
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            document = json.loads(response.read())
        if not isinstance(document, dict):
            raise RuntimeError("openapi.json is not a JSON object")
        return document
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(5)
        shutil.rmtree(root, ignore_errors=True)


@dataclass(frozen=True)
class ServerCheck:
    """Outcome of pinning one CLI's local server route surface."""

    vendor: str
    cli: str
    installed_version: str
    state: str
    problems: tuple[str, ...] = ()
    paths: int = 0


def check_server_api(
    manifest: Manifest,
    installed: Mapping[str, str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    fetch_openapi: Callable[[str], Mapping[str, Any]] = opencode_server_openapi,
    fixtures_root: Path = FIXTURES_ROOT,
) -> list[ServerCheck]:
    """Probe every CLI with a ``server_openapi`` fixture and compare routes."""
    checks: list[ServerCheck] = []
    for fixture in manifest.fixtures:
        if fixture.kind != "server_openapi":
            continue
        executable = which(fixture.cli)
        version = installed.get(fixture.cli, MISSING)
        if not executable:
            checks.append(
                ServerCheck(
                    vendor=fixture.vendor,
                    cli=fixture.cli,
                    installed_version=version,
                    state="missing",
                    problems=("CLI not installed",),
                )
            )
            continue
        expect = fixture.expect if isinstance(fixture.expect, Mapping) else {}
        required = tuple(
            str(item) for item in expect.get("required_paths", []) if isinstance(item, str)
        )
        forbidden = tuple(
            str(item) for item in expect.get("forbidden_paths", []) if isinstance(item, str)
        )
        try:
            committed = json.loads((fixtures_root / fixture.file).read_text(encoding="utf-8"))
            live = fetch_openapi(executable)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            checks.append(
                ServerCheck(
                    vendor=fixture.vendor,
                    cli=fixture.cli,
                    installed_version=version,
                    state="unverified",
                    problems=(f"server probe failed: {type(exc).__name__}: {exc}",),
                )
            )
            continue
        problems = server_api_drift(
            committed, live, required_paths=required, forbidden_paths=forbidden
        )
        raw_paths = live.get("paths")
        live_paths = raw_paths if isinstance(raw_paths, Mapping) else {}
        checks.append(
            ServerCheck(
                vendor=fixture.vendor,
                cli=fixture.cli,
                installed_version=version,
                state="drift" if problems else "ok",
                problems=problems,
                paths=len(live_paths),
            )
        )
    return checks


# ---------------------------------------------------------------------------
# Owner-run capture (token-free)
# ---------------------------------------------------------------------------


def _default_run(cmd: Sequence[str], timeout: float) -> tuple[int, str, str]:
    """Run one allowlisted command with file-backed capture.

    Output goes to temporary files rather than pipes: at least one vendor CLI
    (Claude Code 2.1.328) emits only a pipe-buffer prefix of its ``--help``
    text when stdout is a pipe, which would make a help capture look like a
    removed flag.  A regular file gets the complete text.
    """
    import tempfile

    try:
        with (
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file,
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file,
        ):
            process = subprocess.run(
                list(cmd),
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout_file.seek(0)
            stderr_file.seek(0)
            return process.returncode, stdout_file.read(), stderr_file.read()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)


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


#: Exit code of the ``check`` subcommand when the manifest itself is invalid.
#: Distinct from the drift exit code ``1`` so the owner can tell "a fixture is
#: stale" from "the manifest is broken".
CHECK_INVALID_MANIFEST_EXIT = 2


def run_check(
    manifest_path: str | Path | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: RunFn | None = None,
    stream: Any = None,
    probe_server_api: bool = True,
    fetch_openapi: Callable[[str], Mapping[str, Any]] = opencode_server_openapi,
) -> int:
    """Validate the manifest, print drift records, then guard each drift.

    Returns ``0`` when the validated manifest matches every installed version,
    ``1`` when a fixture is stale, and :data:`CHECK_INVALID_MANIFEST_EXIT` when
    the manifest itself is invalid (unknown kind/source, a missing or
    unredacted fixture file, a duplicate path, a bad date).  An invalid
    manifest stops the check before any ``--version`` probe: a drift
    comparison over an unverified manifest could otherwise print a misleading
    ``no drift``.

    A drifted CLI also gets the no-prompt :func:`check_version_support` guard:
    the allowlisted ``--help`` capture runs and its ``SUPPORTED``/
    ``UNSUPPORTED``/``UNVERIFIED`` state is printed.  The exit code still
    reports drift only; the guard never turns an unverified probe into a
    failure.

    ``probe_server_api`` additionally compares each ``server_openapi``
    fixture's pinned routes with the installed server (token-free).  A missing
    or forbidden route is reported as ``SERVER-API DRIFT`` and makes the exit
    code non-zero, because a help-text match cannot prove the shipped server
    API still exists.
    """
    out = stream if stream is not None else sys.stdout
    path = default_manifest_path() if manifest_path is None else Path(manifest_path)
    manifest = load_manifest(path)
    # Fixture ``file`` entries are relative to the manifest's own directory
    # (``tests/fixtures/vendor_cli/`` for the committed manifest), so an
    # explicit ``--manifest`` is validated against its own fixtures root.
    errors = validate_manifest(manifest, fixtures_root=path.parent)
    if errors:
        print(
            f"INVALID manifest {path}: {len(errors)} error(s); drift not run",
            file=out,
        )
        for error in errors:
            print(f"  - {error}", file=out)
        return CHECK_INVALID_MANIFEST_EXIT
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
        check = check_version_support(
            manifest, drift.cli, drift.installed_version, which=which, run=run
        )
        detail = f"; {check.detail}" if check.detail else ""
        if check.missing:
            detail += f"; missing: {', '.join(check.missing)}"
        print(
            f"{check.state.value.upper()} {drift.vendor} ({drift.cli}): "
            f"installed {drift.installed_version}{detail}",
            file=out,
        )
    if not drifts:
        checked = ", ".join(
            f"{cli}={installed[cli]}" for cli in sorted(installed)
        )
        print(f"no drift: {len(manifest.fixtures)} fixture(s) match ({checked})", file=out)
    api_drift = False
    if probe_server_api:
        for server_check in check_server_api(
            manifest,
            installed,
            which=which,
            fetch_openapi=fetch_openapi,
            fixtures_root=path.parent,
        ):
            if server_check.state == "ok":
                print(
                    f"SERVER-API OK {server_check.vendor} ({server_check.cli}): "
                    f"{server_check.paths} live path(s) include every pinned route",
                    file=out,
                )
                continue
            api_drift = True
            print(
                f"SERVER-API {server_check.state.upper()} "
                f"{server_check.vendor} ({server_check.cli}): "
                f"installed {server_check.installed_version}",
                file=out,
            )
            for problem in server_check.problems:
                print(f"  - {problem}", file=out)
    return 1 if drifts or api_drift else 0


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
    check_parser.add_argument(
        "--manifest",
        default=None,
        help="manifest path (default: the manifest this installation ships)",
    )
    check_parser.add_argument(
        "--no-server-api",
        action="store_true",
        help="skip the token-free local-server route pin",
    )
    capture_parser = subparsers.add_parser("capture", help="capture token-free vendor output")
    capture_parser.add_argument("--vendor", choices=(*VENDOR_CLI, "all"), required=True)
    capture_parser.add_argument("--out", required=True, help="output directory outside the repo")
    args = parser.parse_args(argv)
    if args.command == "check":
        try:
            return run_check(args.manifest, probe_server_api=not args.no_server_api)
        except ManifestUnavailable as exc:
            print(f"[!] {exc}", file=sys.stderr)
            return CHECK_INVALID_MANIFEST_EXIT
    records = capture(args.vendor, args.out)
    _print_capture(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
