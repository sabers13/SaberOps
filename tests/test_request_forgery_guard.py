"""The local UI refuses requests another website could forge or DNS-rebind."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.web import create_app

EVIL = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
# A state-changing route outside the old chat-only allow-list, and its normal form body.
GATE = ("/projects/settings/gate", {"command": "make gate"})
AUTHORITY = ("/settings/authority", {"mode": "full_autonomy", "host_access": "unrestricted"})


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("state", "config"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in ("ORCH_ACCESS_STORE", "ORCH_ACCESS_MODELS_STORE", "ORCH_BINDING_STORE",
                "SABEROPS_ALLOWED_HOSTS", "SABEROPS_PROFILE"):
        monkeypatch.delenv(key, raising=False)


def _client(tmp_path: Path, *, host: str = "127.0.0.1") -> TestClient:
    """A browser on this machine talking to the loopback listener."""
    return TestClient(
        create_app(db_path=tmp_path / "web.db"),
        base_url=f"http://{host}:8765",
        client=("127.0.0.1", 50000),
    )


@pytest.mark.parametrize(("path", "form"), [GATE, AUTHORITY])
def test_cross_site_post_is_refused_on_every_state_changing_route(
    isolated_xdg: None, tmp_path: Path, path: str, form: dict[str, str]
) -> None:
    client = _client(tmp_path)
    for headers in (EVIL, {"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        response = client.post(path, data=form, headers=headers, follow_redirects=False)
        assert response.status_code == 403, headers


def test_a_refused_cross_site_post_changes_nothing(isolated_xdg: None, tmp_path: Path) -> None:
    client = _client(tmp_path)
    before = client.get("/").text
    client.post(*AUTHORITY[:1], data=AUTHORITY[1], headers=EVIL, follow_redirects=False)
    client.post(GATE[0], data=GATE[1], headers=EVIL, follow_redirects=False)
    assert client.get("/").text == before
    assert not list((tmp_path / "config").rglob("authority.json"))


@pytest.mark.parametrize(("path", "form"), [GATE, AUTHORITY])
def test_same_origin_and_headerless_posts_still_work(
    isolated_xdg: None, tmp_path: Path, path: str, form: dict[str, str]
) -> None:
    client = _client(tmp_path)
    same_origin = {"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"}
    assert client.post(path, data=form, headers=same_origin).status_code != 403
    assert client.post(path, data=form).status_code != 403  # curl / CLI: no browser headers


def test_foreign_host_name_on_a_loopback_connection_is_refused(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """DNS rebinding: evil.example resolves to 127.0.0.1, so the page looks same-origin."""
    rebound = _client(tmp_path, host="evil.example")
    assert rebound.get("/").status_code == 403
    assert rebound.get("/health").status_code == 403
    assert rebound.post(*GATE[:1], data=GATE[1], headers={"Origin": "http://evil.example:8765"}
                        ).status_code == 403
    for host in ("127.0.0.1", "localhost"):
        assert _client(tmp_path, host=host).get("/health").status_code == 200


def test_owner_can_allow_an_alias_host_name(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SABEROPS_ALLOWED_HOSTS", "saberops.test, other.test")
    assert _client(tmp_path, host="saberops.test").get("/health").status_code == 200
    assert _client(tmp_path, host="evil.example").get("/health").status_code == 403


def test_lan_clients_are_not_subject_to_the_host_check(isolated_xdg: None, tmp_path: Path) -> None:
    """A listener deliberately bound to a LAN address sees non-loopback clients."""
    client = TestClient(
        create_app(db_path=tmp_path / "web.db"),
        base_url="http://workstation.lan:8765",
        client=("192.168.1.20", 50000),
    )
    assert client.get("/health").status_code == 200
