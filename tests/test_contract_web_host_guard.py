"""Contract: local web Host-header guard for private routes (Step 020 early).

The owner pages expose setup, account and question state on private GET
routes and owner mutations on their POSTs.  A foreign or malformed Host
header (DNS rebinding, curl without Origin, old browsers) must fail closed
with 403 before any owner data is returned or any mutation runs; a missing
Origin header alone never excuses a foreign Host.  Legitimate loopback traffic
(127.0.0.1 / localhost / [::1]) and the default Starlette TestClient Host
(``testserver`` inside a synthetic ASGI scope only) keep working.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.web import create_app


def _client(tmp_path: Path) -> TestClient:
    """Default client: synthetic TestClient scope, Host ``testserver``."""
    return TestClient(create_app(db_path=tmp_path / "web.db"))


def _loopback_client(tmp_path: Path) -> TestClient:
    """Client whose ASGI server scope is a real loopback bind."""
    return TestClient(
        create_app(db_path=tmp_path / "loop.db"),
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    )


def _private_paths() -> list[str]:
    return ["/", "/setup", "/accounts", "/questions"]


def test_valid_loopback_hosts_reach_private_routes(tmp_path: Path) -> None:
    """Covers: prompt section 'Preserve legitimate localhost operation'."""
    client = _loopback_client(tmp_path)
    for host in (
        "127.0.0.1:8765",
        "127.0.0.1",
        "localhost:8765",
        "localhost",
        "[::1]:8765",
        "[::1]",
        "LOCALHOST:8765",
    ):
        response = client.get("/", headers={"Host": host})
        assert response.status_code == 200, host


def test_default_testclient_host_keeps_working(tmp_path: Path) -> None:
    """Covers: prompt section 'compatibility' (TestClient Host=testserver)."""
    client = _client(tmp_path)
    assert client.get("/").status_code == 200
    # A valid synthetic-scope request reaches the app, not the Host guard:
    # a retired route is a 404 from the app, never a 403 from the guard.
    assert client.get("/routing").status_code == 404


def test_foreign_host_rejected_on_private_get(tmp_path: Path) -> None:
    """Covers: threat-model T2/V3 hostile GET (DNS rebinding data theft)."""
    client = _loopback_client(tmp_path)
    for host in (
        "evil.test",
        "evil.test:8765",
        "attacker.example.com",
        "192.168.1.5:8765",
        "0.0.0.0:8765",
    ):
        for path in _private_paths():
            response = client.get(path, headers={"Host": host})
            assert response.status_code == 403, (host, path)


def test_foreign_host_rejected_on_owner_post(tmp_path: Path) -> None:
    """Covers: threat-model V3 hostile POST (owner-action CSRF)."""
    client = _loopback_client(tmp_path)
    for path in ("/questions/pause", "/accounts/confirm-all"):
        response = client.post(
            path, headers={"Host": "evil.test"}, data={"confirm": "yes"}
        )
        assert response.status_code == 403, path
        assert "Host" in response.json()["error"]


def test_missing_origin_cannot_bypass_foreign_host(tmp_path: Path) -> None:
    """Covers: threat-model T2 (Origin-less POST with hostile Host)."""
    client = _loopback_client(tmp_path)
    # No Origin header at all: the pre-existing Origin check stays silent,
    # so the Host guard alone must refuse.
    response = client.post("/questions/pause", headers={"Host": "evil.test"})
    assert response.status_code == 403
    assert "Host" in response.json()["error"]


def test_missing_origin_valid_host_reaches_app(tmp_path: Path) -> None:
    """Covers: prompt section 'compatibility' (curl-like localhost POST)."""
    client = _loopback_client(tmp_path)
    response = client.post(
        "/questions/pause", headers={"Host": "127.0.0.1:8765"}
    )
    # Guard passes; the app's own CSRF refusal answers as HTML, not the
    # guard's JSON refusal.
    assert response.status_code == 403
    assert "text/html" in response.headers["content-type"]
    assert "confirmation token" in response.text


def test_foreign_origin_still_rejected_with_valid_host(tmp_path: Path) -> None:
    """Covers: prompt section 'Preserve existing Origin protections'."""
    client = _loopback_client(tmp_path)
    response = client.post(
        "/questions/pause",
        headers={"Host": "127.0.0.1:8765", "Origin": "https://evil.test"},
    )
    assert response.status_code == 403
    assert "local SaberOps page" in response.json()["error"]
    response = client.post(
        "/accounts/confirm-all",
        headers={"Host": "127.0.0.1:8765", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert "local SaberOps page" in response.json()["error"]


def test_duplicate_host_headers_rejected(tmp_path: Path) -> None:
    """Covers: prompt section 'malformed/multiple Host' (request smuggling)."""
    client = _loopback_client(tmp_path)
    response = client.get(
        "/",
        headers=[("Host", "127.0.0.1:8765"), ("Host", "evil.test")],
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "",
        " ",
        "bad host!",
        "127.0.0.1:",
        "127.0.0.1:notaport",
        "127.0.0.1:0",
        "127.0.0.1:99999",
        "user@127.0.0.1",
        "localhost.",
        "http://127.0.0.1:8765/",
        "[::1",
        "::1",
        "[::1]:1",
        "127.0.0.1:1",
        "a:b:c",
        ":8765",
        "127.0.0.1:8765, evil.test",
    ],
)
def test_malformed_hosts_rejected(tmp_path: Path, host: str) -> None:
    """Covers: prompt section 'Reject malicious and malformed Host headers'."""
    client = _loopback_client(tmp_path)
    assert client.get("/", headers={"Host": host}).status_code == 403


def test_testserver_host_rejected_outside_synthetic_scope(tmp_path: Path) -> None:
    """Covers: prompt section 'NEVER blanket-allow testserver in production'."""
    client = _loopback_client(tmp_path)
    # Same Host value the default TestClient sends, but the ASGI server
    # scope is a real loopback bind: it must fail closed here.
    assert client.get("/", headers={"Host": "testserver"}).status_code == 403
    assert (
        client.post("/questions/pause", headers={"Host": "testserver"}).status_code
        == 403
    )


def test_health_stays_open_by_design(tmp_path: Path) -> None:
    """Covers: health reports non-secret install identity."""
    client = _loopback_client(tmp_path)
    response = client.get("/health", headers={"Host": "evil.test"})
    assert response.status_code == 200
