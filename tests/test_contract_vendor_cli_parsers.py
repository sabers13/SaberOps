"""Replay committed HV-1 vendor fixtures through the existing parser functions.

Every ``real-capture`` / ``owner-supplied-log`` fixture in the manifest is fed
to its ``consumer`` and compared with the recorded expectation.  ``config_help``
fixtures are checked against the flags the adapter pins.  Nothing here runs a
real vendor CLI or a model: the parsers are pure and the fixtures are files.

Covers: HV-1 real-fixture parser replay + adapter config pinning.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from saberops.capacity.probes import duration_to_window, parse_claude_reset_text
from saberops.cli_usage import parse_limits
from saberops.native_usage_panel import parse_panel
from saberops.opencode_go_usage import parse_go_limits
from saberops.vendor_cli_fixtures import (
    FIXTURES_ROOT,
    MANIFEST_PATH,
    Fixture,
    assert_redacted,
    load_manifest,
)
from saberops.workers.exhaustion import (
    ExhaustionClassification,
    classify_antigravity,
    classify_claude,
    classify_cline,
    classify_codex,
    classify_opencode,
    parse_antigravity_usage,
    reset_to_iso,
)

#: Manifest consumer paths mapped to the concrete existing functions they name.
EXPECTED_CONSUMERS: dict[str, Any] = {
    "saberops.native_usage_panel.parse_panel": parse_panel,
    "saberops.cli_usage.parse_limits": parse_limits,
    "saberops.opencode_go_usage.parse_go_limits": parse_go_limits,
    "saberops.capacity.probes.parse_claude_reset_text": parse_claude_reset_text,
    "saberops.capacity.probes.duration_to_window": duration_to_window,
    "saberops.workers.exhaustion.classify_codex": classify_codex,
    "saberops.workers.exhaustion.classify_claude": classify_claude,
    "saberops.workers.exhaustion.classify_opencode": classify_opencode,
    "saberops.workers.exhaustion.classify_cline": classify_cline,
    "saberops.workers.exhaustion.classify_antigravity": classify_antigravity,
    "saberops.workers.exhaustion.parse_antigravity_usage": parse_antigravity_usage,
}

MANIFEST = load_manifest(MANIFEST_PATH)
REAL = MANIFEST.fixtures
CONFIG_HELP = [fx for fx in REAL if fx.kind == "config_help"]
FIXTURE_FILES = sorted(
    p.relative_to(FIXTURES_ROOT).as_posix()
    for p in FIXTURES_ROOT.rglob("*")
    if p.is_file() and p != MANIFEST_PATH
)


def _text(fixture: Fixture) -> str:
    return (FIXTURES_ROOT / fixture.file).read_text(encoding="utf-8")


def _resolve(dotted: str) -> Any:
    module_name, separator, attribute = dotted.partition(":")
    if not separator:
        module_name, _, attribute = dotted.rpartition(".")
    return getattr(importlib.import_module(module_name), attribute)


def _now(fixture: Fixture) -> datetime | None:
    return datetime.fromisoformat(fixture.now) if fixture.now else None


def _tz(fixture: Fixture) -> Any:
    return ZoneInfo(fixture.tz) if fixture.tz else None


def _serialize(classification: ExhaustionClassification) -> dict[str, Any]:
    return {
        "verdict": str(classification.verdict),
        "rule": classification.rule,
        "confidence": classification.confidence,
        "reset_at": reset_to_iso(classification.reset_at),
        "window": classification.window,
        "model_scope": classification.model_scope,
        "reset_confidence": classification.reset_confidence,
    }


def _invoke(fixture: Fixture, text: str) -> Any:
    """Feed one fixture file to the parser or adapter named by ``fixture.consumer``."""
    consumer = fixture.consumer
    target = _resolve(consumer)
    if fixture.kind == "config_help":
        flags = list(fixture.expect["flags"])
        missing = [flag for flag in flags if flag not in text]
        result: dict[str, Any] = {"flags": flags}
        if missing:
            result["known_missing"] = missing
        return result
    if consumer == "saberops.native_usage_panel.parse_panel":
        return target(fixture.provider, text)
    if consumer == "saberops.cli_usage.parse_limits":
        return target(fixture.provider, json.loads(text))
    if consumer == "saberops.opencode_go_usage.parse_go_limits":
        return target(json.loads(text))
    if consumer == "saberops.capacity.probes.parse_claude_reset_text":
        reset, confidence = parse_claude_reset_text(text, now=_now(fixture), tz=_tz(fixture))
        return {"reset_at": reset.isoformat() if reset else None, "confidence": confidence}
    if consumer == "saberops.capacity.probes.duration_to_window":
        return {"window": duration_to_window(text.strip())}
    if consumer == "saberops.workers.exhaustion.parse_antigravity_usage":
        reset = target(text)
        return {"reset_at": reset.isoformat() if reset else None}
    if consumer.endswith("classify_opencode"):
        return _serialize(classify_opencode(text, now=_now(fixture)))
    if consumer.endswith(("classify_codex", "classify_claude")):
        return _serialize(target(text, now=_now(fixture), tz=_tz(fixture)))
    if consumer.endswith(("classify_cline", "classify_antigravity")):
        return _serialize(target(text, now=_now(fixture)))
    raise AssertionError(f"no replay dispatch for consumer {consumer!r}")


def test_manifest_has_real_fixtures_for_every_installed_cli() -> None:
    """Covers: HV-1 real fixture coverage per installed CLI."""
    vendors = {fixture.vendor for fixture in REAL}
    assert {"codex", "claude", "opencode"} <= vendors


@pytest.mark.parametrize("dotted", sorted(EXPECTED_CONSUMERS), ids=lambda d: d.split(".")[-1])
def test_consumer_paths_resolve_to_the_existing_parsers(dotted: str) -> None:
    """Covers: HV-1 consumer dotted paths name the real parser functions."""
    assert _resolve(dotted) is EXPECTED_CONSUMERS[dotted]


def test_every_parser_is_either_fixtured_or_recorded_fake_only() -> None:
    """Covers: HV-1 every parser assumption has a real fixture or a fake-only gap."""
    covered = {fx.consumer for fx in REAL} | {gap.consumer for gap in MANIFEST.fake_only}
    for gap in MANIFEST.fake_only:
        assert _resolve(gap.consumer) is not None
    assert set(EXPECTED_CONSUMERS) <= covered


@pytest.mark.parametrize("relpath", FIXTURE_FILES)
def test_every_real_fixture_is_redacted(relpath: str) -> None:
    """Covers: HV-1 redaction of every committed fixture file."""
    assert_redacted((FIXTURES_ROOT / relpath).read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", REAL, ids=lambda fx: fx.file)
def test_real_fixture_replays_through_its_consumer(fixture: Fixture) -> None:
    """Covers: HV-1 contract replay of a real fixture through its consumer."""
    assert _invoke(fixture, _text(fixture)) == fixture.expect


@pytest.mark.parametrize("fixture", CONFIG_HELP, ids=lambda fx: fx.file)
def test_config_help_documents_adapter_flags(fixture: Fixture) -> None:
    """Covers: HV-1 config_help fixture matches pinned adapter flags."""
    _resolve(fixture.consumer)  # the adapter the help must satisfy must import
    text = _text(fixture)
    known_missing = set(fixture.expect.get("known_missing", []))
    absent = [
        flag
        for flag in fixture.expect["flags"]
        if flag not in known_missing and flag not in text
    ]
    assert absent == [], f"{fixture.file} is missing pinned flags {absent}"


@pytest.mark.parametrize("fixture", CONFIG_HELP, ids=lambda fx: fx.file)
def test_config_help_absent_flags_match_recorded_drift(fixture: Fixture) -> None:
    """Covers: HV-1 only the recorded flag drift is absent from help."""
    text = _text(fixture)
    absent = {flag for flag in fixture.expect["flags"] if flag not in text}
    assert absent == set(fixture.expect.get("known_missing", []))


def test_opencode_adapter_flags_are_all_documented() -> None:
    """Covers: HV-2 F1 (every flag SaberOps pins is in the v2.0.29 help).

    HV-1 recorded this as a strict ``xfail`` (F1 drift: the adapter pinned
    ``--pure``/``--dir``, which the installed help does not document).  Step
    024 (HV-2) removed both flags from ``workers/opencode.py``,
    ``session_runtime.py`` and ``model_access/model_verification.py``, so the
    fixture's pinned-flag expectation is documented and this contract test is
    a plain pass.  ``fixture.expect["flags"]`` is the union of the flags and
    subcommands the three sites pass: ``run``/``--model``/``--format``/
    ``--session`` (worker), ``--agent`` (first-use check), and
    ``serve``/``--hostname``/``--port`` (session server).
    """
    fixture = next(
        fx for fx in CONFIG_HELP if fx.consumer == "saberops.workers.opencode:OpenCodeAdapter"
    )
    text = _text(fixture)
    absent = [flag for flag in fixture.expect["flags"] if flag not in text]
    assert absent == [], f"opencode adapter pins flags absent from help: {absent}"
    assert not fixture.expect.get("known_missing"), (
        "F1 is fixed: no pinned flag may remain recorded as missing from help"
    )
