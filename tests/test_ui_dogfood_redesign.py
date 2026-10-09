"""UI regressions for the v0.3.7 dogfood navigation and setup cleanup."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.projects import ProjectRegistry
from saberops.web import create_app

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "src/saberops/static/css/saberops-console.css"
SHELL_JS = ROOT / "src/saberops/static/js/console.js"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    projects = ProjectRegistry(path=tmp_path / "projects.json")
    return TestClient(create_app(projects=projects, seed_cwd_if_empty=False))


def test_composer_inherits_project_review_settings(client: TestClient) -> None:
    html = client.get("/").text
    assert 'id="runSettingsLayer"' not in html
    assert 'id="runSettingsButton"' not in html
    assert "Run options" not in html
    composer = re.search(r'<form id="startRunForm".*?</form>', html, re.DOTALL)
    assert composer
    for field in ("task", "training_allowed"):
        assert f'name="{field}"' in composer.group()
    assert 'type="hidden" name="training_allowed"' in composer.group()
    assert 'name="review_mode"' not in composer.group()
    assert 'name="review_limit"' not in composer.group()
    assert 'aria-label="Send message"' in html
    assert '<path d="M12 19V5m-6 6 6-6 6 6"/>' in html
    history = client.get("/?pane=runs").text
    assert 'id="startRunForm"' not in history


@pytest.mark.parametrize("mode", ["bounded", "max"])
def test_draft_survives_launch_validation_error_without_old_review_controls(
    client: TestClient, mode: str,
) -> None:
    # No active project: the request re-renders without dispatching a worker.
    response = client.post("/runs", data={
        "task": "Keep this draft", "review_mode": mode, "review_limit": "5",
    })
    assert response.status_code == 400
    html = response.text
    assert "Keep this draft" in html
    composer = re.search(r'<form id="startRunForm".*?</form>', html, re.DOTALL)
    assert composer
    assert 'name="review_mode"' not in composer.group()
    assert 'name="review_limit"' not in composer.group()


def test_collapsed_workers_release_width_and_have_one_reopen_control(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert page.text.count('id="rightCollapseButton"') == 1
    assert 'data-od-id="right-collapse"' in page.text
    css = CSS.read_text()
    assert ".app.right-collapsed { --right: 0px; }" in css
    assert ".app.right-collapsed .workers { display: none; }" in css
    js = SHELL_JS.read_text()
    assert 'collapsed ? "Open workers" : "Collapse workers"' in js
    assert 'collapsed ? "false" : "true"' in js


def test_runs_and_inbox_offer_a_dashboard_return(client: TestClient) -> None:
    inbox = client.get("/inbox")
    assert inbox.status_code == 200
    assert 'aria-label="Page navigation"><a href="/">← Dashboard</a>' in inbox.text
    assert 'data-od-id="runs-back-dashboard"' in inbox.text
    assert 'href="/" data-od-id="runs-back-dashboard">← Dashboard</a>' in inbox.text
    assert 'window.location.assign(pane === "runs" ? "/?pane=runs" : "/")' in SHELL_JS.read_text()


@pytest.mark.parametrize(
    ("context", "target", "label"),
    [
        ("inbox", "/inbox", "Inbox"),
        ("runs", "/?pane=runs", "Runs"),
        ("dashboard", "/", "Dashboard"),
    ],
)
def test_run_detail_has_contextual_back_link(
    client: TestClient, context: str, target: str, label: str
) -> None:
    page = client.get(f"/runs/missing-run?from={context}")
    assert page.status_code == 404
    assert f'id="runReturnLink" data-od-id="run-detail-back" href="{target}"' in page.text
    assert f"← {label}</a>" in page.text
    assert ".page-return a:focus-visible" in CSS.read_text()


def test_setup_steps_are_ordered_and_show_progress(client: TestClient) -> None:
    page = client.get("/setup")
    assert page.status_code == 200
    ids = re.findall(r'data-od-id="setup-step-([a-z]+)"', page.text)
    assert ids == [
        "providers", "worker", "reviewer", "orchestrator",
        "project", "policy", "ready",
    ]
    progress = re.search(r'aria-valuenow="(\d+)"', page.text)
    assert progress and 0 <= int(progress.group(1)) <= 6
    current = re.findall(r'id="setup-step-([1-6])" class="setup-step current"', page.text)
    assert len(current) == 1
    assert page.text.count('aria-current="step"') == 1
    assert f"SETUP INCOMPLETE · {progress.group(1)}/6 configured" in page.text
    for route in (
        "/access", "/routing", "/routing?role=reviewer",
        "/routing?role=orchestrator", "/projects/select",
    ):
        assert route in page.text
    for route in ("/access", "/routing", "/reviewer", "/orchestrator"):
        linked = client.get(route)
        assert linked.status_code == 200
        assert 'aria-label="Setup navigation"' in linked.text
        assert 'href="/setup"' in linked.text


def test_selecting_project_from_setup_returns_to_setup(
    client: TestClient, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Project\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    selected = client.post(
        "/projects/select?from=setup", data={"path": str(repo)}, follow_redirects=False
    )
    assert selected.status_code == 303
    assert selected.headers["location"] == "/setup"


def test_model_access_renders_canonical_readiness_evidence(
    client: TestClient,
) -> None:
    page = client.get("/access")
    assert page.status_code == 200
    html = page.text
    assert 'data-od-id="account-providers"' in html
    assert "Check login" in html
    assert "Refresh models" in html
    assert "Installed" in html
    assert "Authenticated" in html
    assert "discovered" in html
    assert '<details class="access-models">' in html
    assert "Model evidence" in html
    assert 'data-od-id="access-next-steps"' in html
    assert "Choose Worker" in html
    assert "Choose Reviewer" in html
    assert "Choose Orchestrator" in html
    template = (ROOT / "src/saberops/templates/access.html").read_text()
    assert "Discovered does" in template
    assert "not" in template and "executable" in template
    assert "a.readiness.reason" in template
    assert "a.readiness.observed_at" in template
    assert "m.execution_support" in template
    assert "m.routing_eligible" in template
    assert "m.discovery_status" in template


def test_routing_reviewer_orchestrator_binding_authority_unchanged(
    client: TestClient,
) -> None:
    routing = client.get("/routing")
    assert routing.status_code == 200
    assert 'id="routing-T1"' in routing.text
    assert "Earlier models in each role are tried first" in routing.text
    reviewer = client.get("/reviewer")
    assert reviewer.status_code == 200
    assert "Reviewer" in reviewer.text
    orch = client.get("/orchestrator")
    assert orch.status_code == 200
    assert "Orchestrator" in orch.text
    root = ROOT / "src/saberops/templates"
    assert 'action="/routing/assign?view=routing"' in (root / "routing.html").read_text()
    assert 'data-routing-order' in (root / "routing.html").read_text()
    assert 'action="/reviewer/select"' in (root / "reviewer.html").read_text()
    assert 'action="/orchestrator/select"' in (
        root / "orchestrator.html"
    ).read_text()
