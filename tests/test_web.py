"""C16-C2: minimal status page for the SaberOps engine (L-A step 5).

The workspace console (chats, projects, sessions, settings, terminal) was
cut; the page keeps exactly: run list, run detail (status, attempts, gate
results, review verdict, diff), report JSON, accept/reject actions, health.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.db import Database
from saberops.models import Run, RunStatus, Tier
from saberops.web import create_app

_CREATED = "2026-09-23T10:00:00+00:00"


@pytest.fixture()
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("ORCH_ACCESS_STORE", raising=False)
    monkeypatch.delenv("ORCH_ACCESS_MODELS_STORE", raising=False)
    monkeypatch.delenv("ORCH_BINDING_STORE", raising=False)


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


def test_c16c2_dashboard_lists_runs_with_kept_tokens(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Minimal dashboard lists runs and reuses the approved CSS tokens."""
    _seed_run(tmp_path)
    page = _client(tmp_path).get("/")
    assert page.status_code == 200
    text = page.text
    assert "/static/css/console-v2.css" in text
    assert "/static/js/console.js" in text
    assert "console integration probe" in text
    assert 'href="/runs/run_c16c2"' in text
    # Removed console surfaces stay gone.
    for gone in (
        "/access",
        "/routing",
        "/quota",
        "/inbox",
        "/setup",
        "/chats",
        "chat-composer",
        "resolveDecision",
        "Preview completed run",
    ):
        assert gone not in text, gone


def test_c16c2_run_detail_renders_status_evidence_and_actions(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Run page shows status, attempts, gate, review, diff, and owner actions."""
    client = _client(tmp_path)
    _seed_run(tmp_path, "run_c16c2_live", RunStatus.RUNNING)
    live = client.get("/runs/run_c16c2_live")
    assert live.status_code == 200
    text = live.text
    for section in (
        "Attempts · gate results",
        "Review verdict",
        "Candidate diff",
        "Owner actions",
        "Events",
    ):
        assert section in text, section
    assert 'data-run-refresh="events-status"' in text
    assert 'href="/runs/run_c16c2_live/report"' in text
    # Removed run operations stay gone.
    for gone in (
        "/runs/run_c16c2_live/cancel",
        "/runs/run_c16c2_live/retry",
        "/runs/run_c16c2_live/review",
        "/runs/run_c16c2_live/cleanup",
        "/runs/run_c16c2_live/continue",
        "runComposerStopForm",
        "resolveDecision",
    ):
        assert gone not in text, gone


def test_c16c2_missing_run_keeps_minimal_page(
    isolated_xdg: None, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    page = client.get("/runs/does-not-exist")
    assert page.status_code == 404
    assert "Run not found" in page.text
    assert "SaberOps · minimal status" in page.text


def test_c16c2_report_and_health_stay_json(
    isolated_xdg: None, tmp_path: Path
) -> None:
    _seed_run(tmp_path, "run_c16c2_done", RunStatus.COMPLETED)
    client = _client(tmp_path)
    report = client.get("/runs/run_c16c2_done/report")
    assert report.status_code == 200
    assert report.json()["identity"]["run_id"] == "run_c16c2_done"
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["product"] == "SaberOps"


def test_c16c2_console_assets_are_served(
    isolated_xdg: None, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    for asset in (
        "/static/css/console-v2.css",
        "/static/css/saberops-console.css",
        "/static/js/console.js",
        "/static/js/app.js",
    ):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert len(response.text) > 100, asset
    # Removed feature scripts stay gone.
    assert client.get("/static/js/chat.js").status_code == 404
    assert client.get("/static/js/terminal.js").status_code == 404


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
    match = re.search(r'name="csrf" value="([^"]+)"', client.get("/first-run").text)
    assert match is not None, "onboarding page renders no CSRF token"
    return match.group(1)


def _git_repo(tmp_path: Path) -> Path:
    """Create a committed git repository for mode/storage projections."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# probe repo\n", encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("config", "user.email", "ui1@example.com"),
        ("config", "user.name", "UI-1 probe"),
        ("add", "-A"),
        ("commit", "-qm", "initial"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def _dead_scratch(tmp_path: Path) -> Path:
    """Create provably dead-owned gate scratch under the isolated state root."""
    scratch = tmp_path / "state" / "saberops" / "gate-tmp" / "orchestrator-gate-ui1"
    scratch.mkdir(parents=True)
    (scratch / ".saberops-scratch-owner.json").write_text(
        json.dumps({"pid": 2_147_483_000}), encoding="utf-8"
    )
    (scratch / "blob.bin").write_text("junk", encoding="utf-8")
    return scratch


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
            raised_by_role=AskRole.ORCHESTRATOR,
            raised_by="orch",
            escalation=escalation,
            options=options,
        )
    )


def test_ui1_empty_store_lands_on_onboarding(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 Section A — empty-state navigation to the first-run wizard."""
    client = _client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    assert "First-run setup" in page.text
    assert client.get("/first-run").status_code == 200
    _seed_run(tmp_path)
    assert "First-run setup" not in client.get("/").text


def test_ui1_onboarding_sections_render(onboarding_env: None, tmp_path: Path) -> None:
    """Covers: UI-1 Sections A/B/C render with honest empty states."""
    text = _client(tmp_path).get("/first-run").text
    for marker in (
        "A1 · Readiness",
        "A2 · Official vendor logins",
        "A3 · Providers",
        "A4 · Billing posture",
        "B1 · Per-role model assignment",
        "B3 · Concurrency",
        "B4 · Project entry modes",
        "B5 · Plans",
        "C1 · Run monitoring",
        "C2 · Owner question queue",
        "C3 · Unattended",
        "C4 · Storage",
    ):
        assert marker in text, marker
    assert "No runs yet" in text
    assert "password, token, or secret" in text
    assert "/static/css/onboarding.css" in text


def test_ui1_onboarding_assets_are_served(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 — the onboarding stylesheet is served with content."""
    response = _client(tmp_path).get("/static/css/onboarding.css")
    assert response.status_code == 200
    assert len(response.text) > 100


def test_ui1_concurrency_saves_owner_confirmed_limits(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 Section B — 3x5 concurrency persists as an owner decision."""
    client = _client(tmp_path)
    token = _csrf(client)
    response = client.post(
        "/first-run/concurrency",
        data={
            "csrf": token,
            "active_projects": "2",
            "child_slots_per_project": "4",
            "owner_confirmed": "yes",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("concurrency_saved=1")
    from saberops.config import (
        ConfigLedger,
        config_ledger_path,
        current_workflow_settings,
    )
    from saberops.paths import saberops_state_dir

    settings = current_workflow_settings(
        ConfigLedger(config_ledger_path(saberops_state_dir()))
    )
    assert settings.max_concurrent_tasks == 2
    assert settings.max_worker_sessions_per_project == 4


def test_ui1_concurrency_requires_csrf_confirmation_and_range(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 security — CSRF, owner confirmation and range enforcement."""
    client = _client(tmp_path)
    token = _csrf(client)
    assert (
        client.post(
            "/first-run/concurrency",
            data={
                "active_projects": "3",
                "child_slots_per_project": "5",
                "owner_confirmed": "yes",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/first-run/concurrency",
            data={
                "csrf": token,
                "active_projects": "3",
                "child_slots_per_project": "5",
            },
        ).status_code
        == 400
    )
    for projects, slots in (("9", "5"), ("0", "5"), ("3", "6")):
        assert (
            client.post(
                "/first-run/concurrency",
                data={
                    "csrf": token,
                    "active_projects": projects,
                    "child_slots_per_project": slots,
                    "owner_confirmed": "yes",
                },
            ).status_code
            == 400
        ), (projects, slots)


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
    text = client.get("/first-run").text
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
    page = client.get("/first-run").text
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


def test_ui1_storage_dry_run_requires_policy_and_confirmation(
    onboarding_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: UI-1 Section C — dry-run, fail-closed policy, typed confirmation."""
    repo = _git_repo(tmp_path)
    scratch = _dead_scratch(tmp_path)
    client = _client(tmp_path)
    token = _csrf(client)
    refused = client.post(
        "/first-run/storage/apply",
        data={"csrf": token, "repo": str(repo), "preview_id": "x", "confirm": "x"},
        follow_redirects=False,
    )
    assert refused.status_code == 403
    assert scratch.is_dir()

    policy = tmp_path / "gc-policy.json"
    policy.write_text(
        json.dumps(
            {"approved": True, "version": "1", "allow_scratch_deletion": True}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCH_GC_POLICY", str(policy))
    page = client.get("/first-run", params={"repo": str(repo)}).text
    assert "APPROVED" in page
    assert str(scratch) in page
    preview = re.search(r'name="preview_id" value="([^"]+)"', page)
    assert preview is not None
    preview_id = preview.group(1)
    wrong = client.post(
        "/first-run/storage/apply",
        data={
            "csrf": token,
            "repo": str(repo),
            "preview_id": preview_id,
            "confirm": "wrong",
        },
        follow_redirects=False,
    )
    assert wrong.status_code == 400
    assert scratch.is_dir()
    applied = client.post(
        "/first-run/storage/apply",
        data={
            "csrf": token,
            "repo": str(repo),
            "preview_id": preview_id,
            "confirm": preview_id,
        },
        follow_redirects=False,
    )
    assert applied.status_code == 303
    assert "applied=1" in applied.headers["location"]
    assert not scratch.exists()


def test_ui1_project_entry_modes_and_registration(
    onboarding_env: None, tmp_path: Path
) -> None:
    """Covers: UI-1 Section B — entry-mode recommendation and registration."""
    repo = _git_repo(tmp_path)
    client = _client(tmp_path)
    token = _csrf(client)
    page = client.get("/first-run", params={"repo": str(repo)}).text
    assert "Recommended:" in page
    assert "develop_existing" in page
    registered = client.post(
        "/first-run/project/register",
        data={
            "csrf": token,
            "project_id": "proj-ui1",
            "display_name": "UI-1 Project",
            "repository_root": str(repo),
            "state_dir": str(tmp_path / "state" / "proj-ui1"),
            "owner_confirmed": "yes",
        },
        follow_redirects=False,
    )
    assert registered.status_code == 303
    from saberops.project import ProjectRegistry

    assert [p.project_id for p in ProjectRegistry().list_projects()] == ["proj-ui1"]


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
        client.post("/first-run/concurrency", headers={"Host": "evil.test"}).status_code
        == 403
    )
    assert (
        client.post(
            "/first-run/concurrency",
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
    """Covers: UI-1 Section C — standing decisions with live/expired windows."""
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
    text = _client(tmp_path).get("/first-run").text
    assert "k-live" in text and "LIVE" in text
    assert "k-expired" in text and "EXPIRED" in text

