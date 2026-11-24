"""C16-C2 / H23: the kept owner pages (setup, accounts, questions) and health.

Phase 1.1 lane H23 retired run monitoring, plan and wave scoring, the routing
editor and the storage cleanup page from the web app.  These tests cover the
surviving surface and prove the retired routes stay gone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.db import Database
from saberops.models import Run, RunStatus, Tier
from saberops.web import create_app

_CREATED = "2026-09-23T10:00:00+00:00"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(db_path=tmp_path / "web.db"))


def _seed_run(
    tmp_path: Path,
    run_id: str = "run_c16c2",
    status: RunStatus = RunStatus.RUNNING,
) -> None:
    Database(tmp_path / "web.db").create_run(
        Run(
            id=run_id,
            task="console integration probe",
            target_repo="/tmp/does-not-exist-repo",
            base_commit="base-commit-sha",
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=status,
            created_at=_CREATED,
            completed_at=None,
            final_attempt_id=None,
            project_id="proj_c16c2",
            config_json=json.dumps({}),
        )
    )


def test_h23_root_opens_setup(isolated_xdg: None, tmp_path: Path) -> None:
    """The app entry redirects to Setup, and Setup stays reachable directly."""
    client = _client(tmp_path)
    root = client.get("/", follow_redirects=False)
    assert root.status_code == 303
    assert root.headers["location"] == "/setup"
    page = client.get("/")
    assert page.status_code == 200
    assert "Welcome to SaberOps" in page.text
    assert "/static/css/app.css" in page.text
    for link in ('href="/questions"', 'href="/accounts"', 'href="/setup"'):
        assert link in page.text, link


def test_h23_retired_web_routes_are_gone(isolated_xdg: None, tmp_path: Path) -> None:
    """Run monitoring, plans/waves, routing and storage pages stay retired."""
    _seed_run(tmp_path)
    client = _client(tmp_path)
    gone = (
        "/routing",
        "/projects",
        "/runs/run_c16c2",
        "/runs/run_c16c2/report",
        "/runs/run_c16c2/actions",
        "/runs/run_c16c2/events/stream",
    )
    for path in gone:
        assert client.get(path).status_code == 404, path
    for path in (
        "/routing/add",
        "/routing/remove",
        "/routing/move",
        "/first-run/concurrency",
        "/first-run/project/register",
        "/first-run/storage/apply",
        "/runs/run_c16c2/accept",
        "/runs/run_c16c2/reject",
    ):
        assert client.post(path, data={}).status_code in (404, 405), path
    # The run-page controller moved out with the page it drove.
    assert client.get("/static/js/app.js").status_code == 404


def test_h23_console_assets_are_served(isolated_xdg: None, tmp_path: Path) -> None:
    """The shared shell stylesheet and theme controller are still served."""
    client = _client(tmp_path)
    for asset in ("/static/css/app.css", "/static/js/console.js"):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert len(response.text) > 100, asset
    # Removed feature scripts stay gone.
    assert client.get("/static/js/chat.js").status_code == 404
    assert client.get("/static/js/terminal.js").status_code == 404


def test_c16c2_health_stays_json(isolated_xdg: None, tmp_path: Path) -> None:
    health = _client(tmp_path).get("/health")
    assert health.status_code == 200
    assert health.json()["product"] == "SaberOps"







# ---------------------------------------------------------------------------
# Step 019 (UI-1) — complete first-run onboarding surface.
#
# Hermetic: isolated XDG roots, no network, no provider/model call.  Discovery
# is exercised only through its CSRF/validation refusals, never by running a
# real vendor CLI.
# ---------------------------------------------------------------------------

_ONBOARDING_ENV_VARS = (
    "ORCH_ACCESS_STORE",
    "ORCH_ACCESS_MODELS_STORE",
    "ORCH_BINDING_STORE",
    "ORCH_READINESS_STORE",
    "ORCH_QUESTIONS_STORE",
    "ORCH_CONFIG_LEDGER",
    "ORCH_GC_POLICY",
    "ORCH_UNATTENDED_STORE",
)


@pytest.fixture()
def onboarding_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate XDG state/config and clear every SaberOps store override."""
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    for name in _ONBOARDING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _csrf(client: TestClient) -> str:
    """Extract the process CSRF token rendered into the onboarding page."""
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/setup").text)
    assert match is not None, "onboarding page renders no CSRF token"
    return match.group(1)


def test_ui1_empty_store_lands_on_setup(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 Section A — the app entry always lands on Setup now."""
    client = _client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    assert "Welcome to SaberOps" in page.text
    legacy = client.get("/first-run", follow_redirects=False)
    assert legacy.status_code == 303
    assert legacy.headers["location"] == "/setup"
    assert client.get("/first-run").status_code == 200
    _seed_run(tmp_path)
    # A configured store keeps the ordinary Setup heading, never the greeting.
    configured = client.get("/").text
    assert "Welcome to SaberOps" not in configured
    assert "Setup" in configured


def test_ui1_onboarding_sections_render(onboarding_env: None, tmp_path: Path) -> None:
    """Covers: UI-1 Sections A/B/C render on the kept owner pages."""
    client = _client(tmp_path)
    expected = {
        "/setup": (
            "This computer",
            "Sign in to your AI accounts",
            "Find models",
            "Confirm how each account is billed",
            "never asks for a password or key",
            "Connect your agent",
        ),
        "/questions": (
            "No questions waiting",
            "What may run without you",
            "Always your decision",
        ),
        "/accounts": ("Accounts", "What is left", "How it is billed"),
    }
    for path, markers in expected.items():
        page = client.get(path)
        assert page.status_code == 200, path
        assert "/static/css/app.css" in page.text, path
        for marker in markers:
            assert marker in page.text, (path, marker)


def _seed_question(key: str, *, options: tuple[str, ...] = ()) -> None:
    """Ask one routine question through the real owner question queue."""
    from saberops.questions import (
        AskRole,
        DecisionClass,
        Escalation,
        OwnerQuestionQueue,
        QuestionRequest,
    )

    escalation = Escalation(
        requirement="r",
        proposed_expansion="p",
        smallest_compliant="s",
        consequences="c",
        recommendation="rec",
    )
    OwnerQuestionQueue().ask(
        QuestionRequest(
            key=key,
            subject=f"subject {key}",
            kind="scope",
            decision_class=DecisionClass.ROUTINE_QUESTION,
            raised_by_role=AskRole.PLAN,
            raised_by="orch",
            escalation=escalation,
            options=options,
        )
    )




def test_ui1_onboarding_assets_are_served(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 — the shared stylesheet is served with content."""
    response = _client(tmp_path).get("/static/css/app.css")
    assert response.status_code == 200
    assert len(response.text) > 100




def test_ui1_posture_fail_closed_excludes_forbidden_routes(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 Section A — fail-closed posture excludes denied routes."""
    client = _client(tmp_path)
    token = _csrf(client)
    response = client.post(
        "/first-run/posture/verify",
        data={
            "csrf": token,
            "selector": "example/model-x",
            "posture_class": "X",
            "policy_urls": "",
            "note": "vendor forbids automation",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    text = client.get("/setup").text
    assert "example/model-x" in text
    assert "FORBIDDEN" in text


def test_ui1_question_answer_and_defer(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 Section C — the owner answers and defers queued questions."""
    _seed_question("Q-UI1-A", options=("lane-a", "lane-b"))
    _seed_question("Q-UI1-B")
    client = _client(tmp_path)
    token = _csrf(client)
    page = client.get("/questions").text
    assert "subject Q-UI1-A" in page
    assert client.post(
        "/questions/Q-UI1-A/answer",
        data={"answer_text": "no csrf"},
    ).status_code == 403
    answered = client.post(
        "/questions/Q-UI1-A/answer",
        data={"csrf": token, "answer_text": "lane a", "chosen_option": "lane-a"},
        follow_redirects=False,
    )
    assert answered.status_code == 303
    deferred = client.post(
        "/questions/Q-UI1-B/defer",
        data={"csrf": token, "until": "2027-01-01", "note": "later"},
        follow_redirects=False,
    )
    assert deferred.status_code == 303
    from saberops.questions import OwnerQuestionQueue, QuestionStatus

    queue = OwnerQuestionQueue()
    answered_question = queue.get("Q-UI1-A")
    deferred_question = queue.get("Q-UI1-B")
    assert answered_question is not None
    assert deferred_question is not None
    assert answered_question.status is QuestionStatus.ANSWERED
    assert deferred_question.status is QuestionStatus.DEFERRED




def test_ui1_mutations_reject_untrusted_host_and_origin(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 security — localhost-only Host/Origin on new mutations."""
    client = TestClient(
        create_app(db_path=tmp_path / "web.db"),
        base_url="http://127.0.0.1:8765",
        raise_server_exceptions=False,
    )
    assert (
        client.post("/accounts/check", headers={"Host": "evil.test"}).status_code
        == 403
    )
    assert (
        client.post(
            "/accounts/check",
            headers={"Host": "127.0.0.1:8765", "Origin": "https://evil.test"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/questions/Q/answer",
            headers={"Host": "127.0.0.1:8765", "Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )


def test_ui1_discover_is_bounded_and_csrf_gated(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 Section A — discovery is CSRF-gated and never invents models."""
    client = _client(tmp_path)
    token = _csrf(client)
    assert (
        client.post(
            "/first-run/discover", data={"connection_id": "codex-chatgpt-account"}
        ).status_code
        == 403
    )
    unknown = client.post(
        "/first-run/discover",
        data={"csrf": token, "connection_id": "does-not-exist"},
        follow_redirects=False,
    )
    assert unknown.status_code == 400


def test_ui1_standing_decisions_show_expiration_windows(
    onboarding_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: UI-1 Section C + F1 — standing decisions are advisory/expired windows."""
    store = tmp_path / "unattended.json"
    store.write_text(
        json.dumps(
            {
                "standing_decisions": [
                    {
                        "standing_decision_id": "sd-live",
                        "scope": {"project_id": "p", "task_id": "t", "paths": []},
                        "decision_key": "k-live",
                        "owner_words": "yes to local commits",
                        "answered_by": "owner",
                        "granted_at_turn": 0,
                        "ttl_turns": 10,
                    },
                    {
                        "standing_decision_id": "sd-expired",
                        "scope": {"project_id": "p", "task_id": "t", "paths": []},
                        "decision_key": "k-expired",
                        "owner_words": "old permission",
                        "answered_by": "owner",
                        "expires_at": "2000-01-01T00:00:00Z",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCH_UNATTENDED_STORE", str(store))
    text = _client(tmp_path).get("/questions").text
    assert "k-live" in text and "ADVISORY" in text
    assert "k-expired" in text and "EXPIRED" in text
    # F1: an unenforced standing permission is never presented as LIVE.
    live_row = text.split("k-live", 1)[1].split("</tr>", 1)[0]
    assert 'title="LIVE"' not in live_row


def test_f1_questions_page_switches_owner_mode(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: F1 — the owner switches the one owner mode from the web page."""
    from saberops.owner_mode import load_owner_mode_settings

    client = _client(tmp_path)
    token = _csrf(client)
    page = client.get("/questions").text
    assert "Owner mode" in page
    assert "Ask me" in page
    assert "Only you can switch it" in page

    refused = client.post(
        "/questions/mode",
        data={"mode": "away", "until": "2999-01-01T00:00:00Z"},
    )
    assert refused.status_code == 403

    saved = client.post(
        "/questions/mode",
        data={
            "csrf": token,
            "mode": "away",
            "until": "2999-01-01T00:00:00Z",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert load_owner_mode_settings().mode.value == "away"
    assert "Away" in client.get("/questions").text

    cleared = client.post(
        "/questions/mode",
        data={"csrf": token, "clear": "yes"},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    assert load_owner_mode_settings().mode.value == "ask-me"



def test_f17_questions_page_pauses_and_resumes_work(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: F17 -- the owner stops and resumes all work from the web page.

    F24: presence is required from the start, so the owner holds an attended
    terminal for this test; the page's pause/resume still governs the gate.
    """
    from saberops.attendance import attend
    from saberops.runtime_gate import evaluate_gate

    with attend("watch", label="web questions test"):
        client = _client(tmp_path)
        token = _csrf(client)
        page = client.get("/questions").text
        assert "Work gate" in page
        assert "Pause all work" in page

        # A pause needs the explicit confirmation, and the CSRF token.
        refused = client.post("/questions/pause", data={"reason": "stepping away"})
        assert refused.status_code == 403
        unconfirmed = client.post(
            "/questions/pause", data={"csrf": token, "reason": "stepping away"}
        )
        assert unconfirmed.status_code == 400
        assert evaluate_gate(db=None).closed is False

        paused = client.post(
            "/questions/pause",
            data={"csrf": token, "confirm": "yes", "reason": "stepping away"},
            follow_redirects=False,
        )
        assert paused.status_code == 303
        state = evaluate_gate(db=None)
        assert state.closed is True
        assert state.reason == "owner_pause"

        resumed = client.post(
            "/questions/resume",
            data={"csrf": token, "confirm": "yes"},
            follow_redirects=False,
        )
        assert resumed.status_code == 303
        assert evaluate_gate(db=None).closed is False


def test_f17_away_mode_needs_a_bound_on_the_web_page(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: F17 -- the web toggle refuses an unbounded away selection."""
    from saberops.owner_mode import load_owner_mode_settings

    client = _client(tmp_path)
    token = _csrf(client)
    response = client.post(
        "/questions/mode",
        data={"csrf": token, "mode": "away"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert load_owner_mode_settings().mode.value == "ask-me"

    saved = client.post(
        "/questions/mode",
        data={"csrf": token, "mode": "away", "max_jobs": "3"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert load_owner_mode_settings().max_jobs == 3
