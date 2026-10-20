"""The Accounts page: subscription usage left and billing confirmations.

Hermetic: isolated XDG roots, readings seeded into the store, no CLI or
network. Under pytest the usage check never launches a real executable.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.accounts_view import accounts_view, check_usage
from saberops.capacity.store import ensure_tables
from saberops.db import Database
from saberops.web import _csrf_token, create_app

_NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("state", "config", "cache"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("ORCH_ACCOUNT_USAGE_CACHE", str(tmp_path / "state" / "usage.json"))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)


def _seed(db_path: Path, harness: str, window: str, remaining: float, minutes_ago: int) -> None:
    db = Database(db_path)
    ensure_tables(db)
    observed = _NOW - timedelta(minutes=minutes_ago)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO capacity_observations (id, pool_id, window_id, remaining_pct, reset_at,"
            " observed_at, source, legacy_provider) VALUES (?, ?, ?, ?, ?, ?, 'PROBE', ?)",
            (
                f"obs_{harness}_{window}_{minutes_ago}",
                harness,
                window,
                remaining,
                (_NOW + timedelta(hours=2)).isoformat(),
                observed.isoformat(),
                harness,
            ),
        )


def test_latest_reading_per_window_and_the_lowest_leads(isolated_xdg: None, tmp_path: Path) -> None:
    db_path = tmp_path / "web.db"
    _seed(db_path, "claude", "5h", 40.0, 90)
    _seed(db_path, "claude", "5h", 12.0, 10)
    _seed(db_path, "claude", "weekly", 54.0, 10)
    view = accounts_view(Database(db_path), now=_NOW)
    claude = next(card for card in view["cards"] if card["slug"] == "claude")
    assert [(w["label"], w["remaining"]) for w in claude["windows"]] == [
        ("5-hour window", 12),
        ("This week", 54),
    ]
    assert claude["windows"][0]["tone"] == "warn"
    assert claude["windows"][0]["reset"] == "resets in 2 h"
    assert claude["checked"] == "10 min ago" and not claude["stale"]
    assert view["low"][0][0] == "Claude"


def test_no_reading_is_shown_as_not_checked(isolated_xdg: None, tmp_path: Path) -> None:
    view = accounts_view(Database(tmp_path / "web.db"), now=_NOW)
    assert all(card["windows"] == [] and card["stale"] for card in view["cards"])
    assert view["low"] == []


def test_check_never_launches_a_cli_under_test(isolated_xdg: None, tmp_path: Path) -> None:
    db = Database(tmp_path / "web.db")
    assert check_usage(db, "codex", now=_NOW) == "disabled-under-test"
    assert check_usage(db, "antigravity", now=_NOW) == "disabled-under-test"
    assert check_usage(db, "nope", now=_NOW) == "failed"


def test_accounts_page_and_routes(isolated_xdg: None, tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    page = client.get("/accounts")
    assert page.status_code == 200
    for marker in ("Accounts", "What is left", "How it is billed", 'action="/accounts/check"'):
        assert marker in page.text, marker
    assert 'href="/accounts"' in page.text

    refused = client.post("/accounts/check", data={"account": "codex"})
    assert refused.status_code == 403
    checked = client.post(
        "/accounts/check",
        data={"csrf": _csrf_token(), "account": "codex"},
        follow_redirects=False,
    )
    assert checked.status_code == 303
    assert checked.headers["location"] == "/accounts?checked=codex&outcome=disabled-under-test"
    assert (
        client.post("/accounts/check", data={"csrf": _csrf_token(), "account": "other"}).status_code
        == 400
    )


def test_billing_confirmation_returns_to_accounts(isolated_xdg: None, tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    done = client.post(
        "/first-run/posture/verify",
        data={
            "csrf": _csrf_token(),
            "selector": "codex",
            "posture_class": "A",
            "policy_urls": "https://openai.com/policies/terms-of-use",
            "return_to": "accounts",
        },
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == "/accounts?posture_saved=1"
    page = client.get("/accounts?posture_saved=1").text
    assert "Billing confirmed until you change it" in page
    assert "Confirm again" in page
    refused = client.post(
        "/first-run/posture/verify",
        data={
            "csrf": _csrf_token(),
            "selector": "codex",
            "posture_class": "A",
            "return_to": "accounts",
            "policy_urls": "http://insecure.example",
        },
    )
    assert refused.status_code == 400 and "Billing not confirmed" in refused.text


@pytest.mark.parametrize(
    ("selector", "posture_class", "urls"),
    [("antigravity", "B", "https://antigravity.google/terms"), ("backend:DIRECT_API", "D", "")],
)
def test_attended_only_and_metered_confirmations_count_as_confirmed(
    isolated_xdg: None, tmp_path: Path, selector: str, posture_class: str, urls: str
) -> None:
    """Regression: a confirmed "only while you watch" account is confirmed, not pending."""
    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    done = client.post(
        "/first-run/posture/verify",
        data={
            "csrf": _csrf_token(),
            "selector": selector,
            "posture_class": posture_class,
            "policy_urls": urls,
            "return_to": "accounts",
        },
        follow_redirects=False,
    )
    assert done.status_code == 303
    view = accounts_view(Database(tmp_path / "web.db"))
    rows = [row for card in view["cards"] for row in card["billing"]] + view["others"]
    row = next(row for row in rows if row["selector"] == selector)
    assert row["ok"] is True
    if selector == "antigravity":
        card = next(card for card in view["cards"] if card["slug"] == "antigravity")
        assert card["billing_ok"] is True
        assert "Antigravity" not in view["unconfirmed"]


def test_no_expiry_confirmation_still_enforces_the_billing_type() -> None:
    from datetime import date

    from saberops.control_plane.plans import PlanSet, PostureRecord
    from saberops.control_plane.posture import posture_decision

    def decide(cls: str) -> tuple[bool, str | None, str | None]:
        plans = PlanSet(
            postures={"codex": PostureRecord("codex", cls, ttl_days=None, verified_on="2020-01-01")}
        )
        d = posture_decision("codex", "gpt-x", plans=plans, today=date(2030, 1, 1))
        return d.admitted_unattended, d.reason, d.expires_on

    assert decide("A") == (True, None, None)  # years later, still confirmed
    assert decide("B") == (False, "POSTURE_ATTENDED_ONLY", None)
    assert decide("D") == (False, "POSTURE_METERED", None)
    assert decide("X")[0] is False


def test_confirm_all_confirms_every_account_with_links(isolated_xdg: None, tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=tmp_path / "web.db"))
    assert client.post("/accounts/confirm-all").status_code == 403
    done = client.post(
        "/accounts/confirm-all", data={"csrf": _csrf_token()}, follow_redirects=False
    )
    assert done.status_code == 303
    page = client.get(done.headers["location"]).text
    assert "until you change it" in page
    view = accounts_view(Database(tmp_path / "web.db"))
    rows = [r for card in view["cards"] for r in card["billing"]] + view["others"]
    assert all(r["ok"] for r in rows if r["class"] != "X" and (r["urls"] or r["class"] == "D"))
    assert all(r["expires_on"] is None for r in rows if r["ok"])
