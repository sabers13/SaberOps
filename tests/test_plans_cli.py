"""Plans/posture service functions and CLI (M2-2, step G).

Every command goes through exactly one service function in
``saberops.plans_ops``; the CLI only parses arguments and renders
output.  Tests drive the real CLI entry point with an isolated
``XDG_CONFIG_HOME``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from saberops.cli import main


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def _cli(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _owner_path(isolated: Path) -> Path:
    return isolated / "config" / "saberops" / "control_plane.json"


# ---------------------------------------------------------------------------
# plans show / validate
# ---------------------------------------------------------------------------


def test_plans_show_json_shape(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli("plans", "show", "--json", capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert set(payload) == {
        "accounts",
        "quota_pools",
        "postures",
        "reserves",
        "reserves_source",
    }
    assert "chatgpt-plus" in payload["accounts"]
    assert "claude-pro" in payload["quota_pools"]
    assert "codex" in payload["postures"]
    entry = payload["postures"]["codex"]
    assert entry["sources"]["class"] == "packaged"
    assert entry["class"] == "A"


def test_plans_show_human(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli("plans", "show", capsys=capsys)
    assert code == 0
    assert "codex.class = 'A' (packaged)" in out


def test_plans_validate_ok(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli("plans", "validate", capsys=capsys)
    assert code == 0
    assert "valid" in out
    code, out, _ = _cli("plans", "validate", "--json", capsys=capsys)
    assert code == 0
    assert json.loads(out)["ok"] is True


def test_plans_validate_file_failure_exit_2(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = isolated / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "version": 2,
                "providers": {},
                "quota_pools": {},
                "postures": {"codex": {"class": "E"}},
            }
        ),
        encoding="utf-8",
    )
    code, _, err = _cli("plans", "validate", "--file", str(bad), capsys=capsys)
    assert code == 2
    assert "postures.codex.class" in err
    code, out, _ = _cli(
        "plans", "validate", "--file", str(bad), "--json", capsys=capsys
    )
    assert code == 2
    payload = json.loads(out)
    assert payload["ok"] is False
    assert any("postures.codex.class" in error for error in payload["errors"])


def test_plans_validate_effective_failure_exit_2(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = _owner_path(isolated)
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.write_text(
        json.dumps({"version": 2, "providers": {}, "quota_pools": {"p": {"mode": "NOPE"}}}),
        encoding="utf-8",
    )
    code, _, err = _cli("plans", "validate", capsys=capsys)
    assert code == 2
    assert "quota_pools.p.mode" in err


# ---------------------------------------------------------------------------
# posture show
# ---------------------------------------------------------------------------


def test_posture_show_json_shape(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli("posture", "show", "--json", capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert "today" in payload
    rows = {row["selector"]: row for row in payload["postures"]}
    assert set(rows) >= {"codex", "copilot", "backend:DIRECT_API"}
    assert set(rows["codex"]) == {
        "selector",
        "class",
        "verified_on",
        "expires_on",
        "status",
    }
    assert rows["codex"]["status"] == "UNVERIFIED"
    assert rows["copilot"]["status"] == "FORBIDDEN"


def test_posture_show_human(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli("posture", "show", capsys=capsys)
    assert code == 0
    assert "SELECTOR" in out
    assert "UNVERIFIED" in out


# ---------------------------------------------------------------------------
# posture verify
# ---------------------------------------------------------------------------


def test_verify_writes_only_owner_fields(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    today = date.today().isoformat()
    code, out, _ = _cli("posture", "verify", "codex", "--json", capsys=capsys)
    assert code == 0
    result = json.loads(out)
    assert result["selector"] == "codex"
    assert result["class"] == "A"
    assert result["verified_on"] == today
    assert result["status"] == "OK"
    owner = json.loads(_owner_path(isolated).read_text(encoding="utf-8"))
    # Only the attested selector (plus its pinned packaged account) was
    # written; no unrelated section was created or rewritten.
    assert set(owner) == {"postures", "version", "accounts"}
    assert set(owner["postures"]) == {"codex"}
    assert owner["postures"]["codex"]["verified_on"] == today
    assert set(owner["accounts"]) == {"chatgpt-plus"}
    # The effective record still overlays the packaged class and urls.
    code, out, _ = _cli("plans", "show", "--json", capsys=capsys)
    assert code == 0
    entry = json.loads(out)["postures"]["codex"]
    assert entry["class"] == "A"
    assert entry["verified_on"] == today
    assert entry["sources"]["verified_on"] == "owner"


def test_verify_human_output(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli("posture", "verify", "codex", capsys=capsys)
    assert code == 0
    assert "Posture verified: codex" in out
    assert "status OK" in out


def test_verify_unknown_selector_without_class_refused(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _cli(
        "posture", "verify", "brand-new-selector", capsys=capsys
    )
    assert code == 1
    assert "UNKNOWN_SELECTOR" in err
    assert not _owner_path(isolated).exists()


def test_verify_new_selector_with_class(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli(
        "posture",
        "verify",
        "myprovider",
        "--class",
        "C",
        "--url",
        "https://example.com/policy",
        "--json",
        capsys=capsys,
    )
    assert code == 0
    result = json.loads(out)
    assert result["class"] == "C"
    assert result["status"] == "OK"
    owner = json.loads(_owner_path(isolated).read_text(encoding="utf-8"))
    assert owner["postures"]["myprovider"]["class"] == "C"


def test_verify_class_change_without_class_keeps_existing(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A re-verification without --class must not silently change the
    # class; an explicit --class is required for a change.
    code, _, _ = _cli("posture", "verify", "codex", capsys=capsys)
    assert code == 0
    owner = json.loads(_owner_path(isolated).read_text(encoding="utf-8"))
    assert owner["postures"]["codex"]["class"] == "A"
    code, out, _ = _cli(
        "posture", "verify", "codex", "--class", "B", "--json", capsys=capsys
    )
    assert code == 0
    assert json.loads(out)["status"] == "ATTENDED_ONLY"
    owner = json.loads(_owner_path(isolated).read_text(encoding="utf-8"))
    assert owner["postures"]["codex"]["class"] == "B"


def test_verify_new_a_without_url_refused(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _cli(
        "posture", "verify", "newprovider", "--class", "A", capsys=capsys
    )
    assert code == 1
    assert "URLS_REQUIRED" in err
    assert not _owner_path(isolated).exists()


def test_verify_http_url_refused(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _cli(
        "posture",
        "verify",
        "codex",
        "--url",
        "http://example.com/policy",
        capsys=capsys,
    )
    assert code == 1
    assert "INVALID_URL" in err


def test_verify_explicit_date(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli(
        "posture", "verify", "codex", "--date", "2026-10-01", "--json", capsys=capsys
    )
    assert code == 0
    result = json.loads(out)
    assert result["verified_on"] == "2026-10-01"
    assert result["expires_on"] == "2026-10-31"
