"""C16-C2: approved OpenDesign console integrated into the production UI.

Regression coverage proving the redesign did not break backend-facing
actions. Source design: OpenDesign project
``25e96df8-02ca-468a-9335-0ea38e57308f``, artifact
``orchestrator-console-v2.html`` (canonical product UI).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.db import Database
from saberops.models import Run, RunStatus, Tier
from saberops.routing_config import (
    DynamicCandidate,
    add_dynamic_candidate_to_chain,
    format_candidate_id,
    load_effective_routing_payload,
    save_user_routing,
)
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


def _seed_routing_chain() -> None:
    """Seed one chain candidate so remove/move controls render."""
    candidate_id = format_candidate_id("opencode", "seed-model-c16c2")
    approval = DynamicCandidate(
        candidate_id=candidate_id,
        provider="opencode",
        model="seed-model-c16c2",
        connection_id="opencode-account",
        backend="opencode",
        binding_id="discovered-binding:worker:opencode-account:seed-model-c16c2",
        discovery_status="TRUE",
        execution_support="SUPPORTED",
        training_required="UNKNOWN",
        capabilities=(),
        approved_at=_CREATED,
    )
    save_user_routing(
        add_dynamic_candidate_to_chain(
            load_effective_routing_payload(),
            top="T1",
            sub="default",
            candidate=approval,
        )
    )


def test_c16c2_dashboard_renders_approved_shell(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Dashboard uses the approved console shell, not the legacy layout."""
    client = _client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    text = page.text
    # Approved shell markers.
    assert "/static/css/console-v2.css" in text
    assert "/static/js/console.js" in text
    assert 'data-od-id="orchestrator-chat-workspace"' in text
    assert 'data-od-id="pane-projects"' in text
    assert 'data-od-id="chat-composer"' in text
    assert 'data-od-id="settings-modal"' in text
    assert 'data-od-id="command-palette"' in text
    assert "SaberOps workspace" in text
    # Prototype-only fakes are not carried over.
    assert "scenario-switcher" not in text
    assert "setScenario" not in text
    assert "resolveDecision" not in text
    assert "Preview completed run" not in text
    # Legacy visual structure is gone.
    assert 'class="sidebar"' not in text
    assert "nav-link" not in text
    assert "/static/css/style.css" not in text
    # Owner capabilities remain reachable.
    assert "/access" in text
    assert "/routing" in text
    assert "/routing?role=orchestrator" in text
    assert "/quota" in text
    # R0-A: the legacy Manager capability is gone from owner navigation.
    assert "/manager" not in text
    assert "Ox Manager" not in text
    assert "Ox Alpha" not in text
    # Pages without inspector tabs use the collapsed three-row grid.
    assert 'class="workspace"' in text
    assert 'class="workspace has-tabs"' not in text


def test_c16c2_start_run_form_posts_real_fields(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """The composer form carries the real backend field names."""
    client = _client(tmp_path)
    text = client.get("/").text
    assert 'action="/chats"' in text
    for field in ('name="task"', 'name="training_allowed"'):
        assert field in text, field
    # The project-settings card on the same page legitimately owns review_mode and
    # review_limit; only the composer form must not carry run-time knobs.
    start = text.index('<form id="startRunForm"')
    composer = text[start:text.index("</form>", start)]
    for removed in ("review_mode", "review_limit", "manual_tier", "max_auto_tier",
                    "provider_override", "model_override",
                    "worker_timeout", "gate_timeout", "review_timeout", "gate_command",
                    "review_enabled", "routing_mode"):
        assert f'name="{removed}"' not in composer
    # No prototype-only knobs with no backend capability.
    assert 'id="runPriority"' not in text
    assert 'id="maxAttempts"' not in text
    assert 'id="hardBudget"' not in text


def test_c16c2_project_select_persists(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Select/switch repository still works through the shell."""
    client = _client(tmp_path)
    target = tmp_path / "repo"
    target.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", str(target)], check=True)
    response = client.post(
        "/projects/select", data={"path": str(target)}, follow_redirects=False
    )
    assert response.status_code == 303
    dashboard = client.get("/")
    assert str(target) in dashboard.text


def test_c16c2_run_detail_renders_inspector_and_actions(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Run page wires the approved inspector tabs to real actions."""
    client = _client(tmp_path)
    _seed_run(tmp_path, "run_c16c2_live", RunStatus.RUNNING)
    _seed_run(tmp_path, "run_c16c2_done", RunStatus.COMPLETED)
    live = client.get("/runs/run_c16c2_live")
    assert live.status_code == 200
    assert 'data-od-id="run-detail-back" href="/inbox"' in live.text
    from_runs = client.get("/runs/run_c16c2_live?from=runs")
    assert 'data-od-id="run-detail-back" href="/?pane=runs"' in from_runs.text
    assert 'class="workspace has-tabs"' in live.text
    for tab in (
        "chat",
        "overview",
        "workers",
        "changes",
        "checks",
        "review",
        "events",
        "routing",
        "usage",
        "terminal",
    ):
        assert f'data-tab="{tab}"' in live.text, tab
        assert f'data-panel="{tab}"' in live.text, tab
    # A live run exposes cancel + the live surfaces.
    assert 'action="/runs/run_c16c2_live/cancel"' in live.text
    assert 'data-run-refresh="events-status"' in live.text  # stream URL is built by app.js
    assert "/runs/run_c16c2_live/report" in live.text
    assert 'id="terminal"' in live.text
    assert 'id="runComposerStopButton"' in live.text
    assert 'aria-label="Stop task"' in live.text
    assert 'id="runComposerStopForm" data-action="cancel">' in live.text
    # A completed run exposes retry/review/cleanup.
    done = client.get("/runs/run_c16c2_done")
    assert done.status_code == 200
    assert 'action="/runs/run_c16c2_done/retry"' in done.text
    assert 'action="/runs/run_c16c2_done/review"' in done.text
    assert 'action="/runs/run_c16c2_done/cleanup"' in done.text
    assert 'id="runComposerStopForm" data-action="cancel" hidden' in done.text
    assert 'id="runComposerNewTask" aria-label="New task" title="New task">' in done.text
    # No fake prototype controls on either.
    assert "resolveDecision" not in live.text
    assert "resolveDecision" not in done.text
    assert "Preview completed run" not in live.text


def test_c16c2_missing_run_keeps_shell(
    isolated_xdg: None, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    page = client.get("/runs/does-not-exist")
    assert page.status_code == 404
    assert 'data-od-id="orchestrator-chat-workspace"' in page.text


def test_c16c2_quota_set_round_trip(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Quota management still mutates real backend state."""
    client = _client(tmp_path)
    response = client.post(
        "/quota/set",
        data={
            "provider": "opencode",
            "pool": "default",
            "remaining_percent": "42",
            "reset_at": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/quota")
    assert page.status_code == 200
    assert "opencode" in page.text
    assert "42%" in page.text
    assert 'action="/quota/enable"' in page.text


def test_c16c2_owner_pages_keep_forms(
    isolated_xdg: None, tmp_path: Path
) -> None:
    _seed_routing_chain()
    client = _client(tmp_path)
    access = client.get("/access")
    assert access.status_code == 200
    assert 'action="/access/connections"' in access.text
    assert "Refresh models" in access.text

    routing = client.get("/routing")
    assert routing.status_code == 200
    assert 'id="routing-T1"' in routing.text
    assert 'action="/routing/remove"' in routing.text
    assert "data-routing-order" in routing.text
    assert "Move up" not in routing.text

    orch = client.get("/orchestrator")
    assert orch.status_code == 200
    assert 'action="/orchestrator/select"' in orch.text
    assert "Available models" in orch.text


def test_c16c2_settings_modal_marks_unavailable_sections(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """Sections with no backend capability are removed, not fake."""
    client = _client(tmp_path)
    text = client.get("/").text
    for section in (
        "general",
        "execution",
        "providers",
        "models",
        "routing",
        "repositories",
    ):
        assert f'data-settings-section="{section}"' in text, section
    # Color palette and light/dark mode can be selected independently.
    assert 'aria-label="Color theme"' in text
    assert 'data-set-palette="graphite-blue"' in text
    assert 'data-set-palette="neutral-ink"' in text
    assert text.count('data-set-palette=') == 2
    assert 'data-set-theme="light"' in text
    assert 'data-set-theme="dark"' in text
    assert 'data-set-palette="moss"' not in text
    assert 'data-set-palette="midnight-blue"' not in text
    # R0-A: placeholder Notifications/Retention surfaces are gone.
    assert 'data-settings-section="notifications"' not in text
    assert 'data-settings-section="retention"' not in text
    assert "Not available in this version" not in text


def test_c16c2_console_assets_are_served(
    isolated_xdg: None, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    for asset in (
        "/static/css/console-v2.css",
        "/static/css/saberops-console.css",
        "/static/css/workspace.css",
        "/static/js/console.js",
        "/static/js/app.js",
        "/static/js/terminal.js",
    ):
        response = client.get(asset)
        assert response.status_code == 200, asset
        assert len(response.text) > 100, asset


def test_workspace_runs_view_lists_recorded_tasks_without_composer(
    isolated_xdg: None, tmp_path: Path
) -> None:
    _seed_run(tmp_path)
    client = _client(tmp_path)
    history = client.get("/?pane=runs")
    assert history.status_code == 200
    assert "Run history" in history.text
    assert 'class="run-list-item" href="/runs/run_c16c2?from=runs"' in history.text
    assert "console integration probe" in history.text
    assert 'data-od-id="chat-composer"' not in history.text
    assert 'data-has-inspector="false"' in history.text
    workspace = client.get("/")
    assert 'data-od-id="chat-composer"' in workspace.text
    assert 'class="run-list-item" href="/runs/run_c16c2?from=dashboard"' in workspace.text


def test_contextual_inspector_and_missing_run_have_correct_layout(
    isolated_xdg: None, tmp_path: Path
) -> None:
    _seed_run(tmp_path)
    client = _client(tmp_path)
    run = client.get("/runs/run_c16c2")
    assert 'data-has-inspector="true"' in run.text
    assert 'role="tablist"' in run.text
    missing = client.get("/runs/no-such-run")
    assert missing.status_code == 404
    assert 'class="workspace has-tabs"' not in missing.text
    assert 'data-has-inspector="false"' in missing.text
    assert "Run not found" in missing.text
