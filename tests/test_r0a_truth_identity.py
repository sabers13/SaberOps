"""R0-A: truth and identity cleanup.

Proves the legacy Manager/Ox capability is gone from owner surfaces,
misleading UI is removed, SaberOps identity is truthful, and surviving
backend functionality still renders.
"""

from __future__ import annotations

import json
import re
import subprocess as _subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from saberops.db import Database
from saberops.models import Run, RunStatus, Tier
from saberops.observability.run_report import saberops_version
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
    return TestClient(create_app(db_path=tmp_path / "web.db"), raise_server_exceptions=False)


def _seed_run(tmp_path: Path, run_id: str = "run_r0a_probe") -> None:
    Database(tmp_path / "web.db").create_run(
        Run(
            id=run_id,
            task="r0a identity probe",
            target_repo="/tmp/does-not-exist-repo",
            base_commit="base-commit-sha",
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
            final_attempt_id=None,
            project_id="proj_r0a",
            config_json=json.dumps({}),
        )
    )


def test_r0a_manager_routes_gone(isolated_xdg: None, tmp_path: Path) -> None:
    """Legacy Manager HTTP surfaces no longer exist."""
    client = _client(tmp_path)
    assert client.get("/manager").status_code == 404
    response = client.post("/manager/message", json={"text": "hello"})
    assert response.status_code in (404, 405)


def test_r0a_manager_owner_navigation_gone(isolated_xdg: None, tmp_path: Path) -> None:
    """No owner navigation points at the removed Manager/Ox capability."""
    client = _client(tmp_path)
    _seed_run(tmp_path)
    for path in ("/", "/access", "/routing", "/orchestrator", "/quota", "/runs/run_r0a_probe"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "/manager" not in page.text, path
        assert "Ox Manager" not in page.text, path
        assert "Ox Alpha" not in page.text, path
        assert "manager-form" not in page.text, path
        assert "manager/message" not in page.text, path


def test_r0a_no_unsafe_quick_actions(isolated_xdg: None, tmp_path: Path) -> None:
    """Dashboard no longer fills the task composer to launch runs by accident."""
    client = _client(tmp_path)
    text = client.get("/").text
    assert "data-fill-composer" not in text
    assert "Show provider status" not in text
    assert "List routing chains" not in text
    assert "Summarize recent runs" not in text
    console = client.get("/static/js/console.js")
    assert console.status_code == 200
    assert "data-fill-composer" not in console.text
    app_js = client.get("/static/js/app.js")
    assert app_js.status_code == 200
    assert "/manager/message" not in app_js.text
    assert "Ox Alpha" not in app_js.text


def test_r0a_placeholder_surfaces_absent(isolated_xdg: None, tmp_path: Path) -> None:
    """Removed placeholder/misleading surfaces stay absent."""
    client = _client(tmp_path)
    _seed_run(tmp_path)
    dashboard = client.get("/").text
    assert 'data-settings-section="notifications"' not in dashboard
    assert 'data-settings-section="retention"' not in dashboard
    assert "Not available in this version" not in dashboard
    assert "Push remains CLI-only" not in dashboard
    assert "Antigravity permission posture" not in dashboard
    assert "Execution tiers" not in dashboard
    assert "ox-alpha-free" not in dashboard
    run_page = client.get("/runs/run_r0a_probe").text
    assert "Push remains CLI-only" not in run_page
    assert "3B plan" not in run_page


def test_r0a_saberops_branding_present(isolated_xdg: None, tmp_path: Path) -> None:
    """Owner shell presents the executing SaberOps identity."""
    client = _client(tmp_path)
    text = client.get("/").text
    assert f"SaberOps {saberops_version()}" in text
    assert "Orchestrator Dashboard" not in text


def test_r0a_orchestrator_kept_as_technical_role(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """The Orchestrator technical role keeps its name with truthful copy."""
    client = _client(tmp_path)
    page = client.get("/orchestrator")
    assert page.status_code == 200
    assert "Orchestrator role" in page.text
    assert "initial task guidance" in page.text
    assert "decision role" not in page.text
    assert "selection authority" not in page.text
    assert "binding identity" not in page.text
    assert "exact executable binding" not in page.text
    access = client.get("/access").text
    assert "decision role" not in access


def test_r0a_api_connections_are_experimental(isolated_xdg: None, tmp_path: Path) -> None:
    """API/BYOK surface is clearly marked Experimental, never a normal path."""
    client = _client(tmp_path)
    text = client.get("/access").text
    assert "Experimental" in text
    assert "cannot execute models yet" in text


def test_r0a_health_reports_product_version_truthfully(
    isolated_xdg: None, tmp_path: Path
) -> None:
    """GET /health names SaberOps and never fabricates a Git SHA."""
    client = _client(tmp_path)
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["product"] == "SaberOps"
    assert payload["version"] == saberops_version()
    assert payload["version"]
    sha = payload["source_git_sha"]
    assert isinstance(sha, str) and sha
    assert sha == "UNKNOWN" or re.fullmatch(r"[0-9a-f]{40}", sha) is not None


def test_r0a_health_unknown_outside_source_tree(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed package far from any checkout must report UNKNOWN."""
    import saberops.web as web_module

    fake_pkg = tmp_path / "site-packages" / "saberops"
    fake_pkg.mkdir(parents=True)
    monkeypatch.setattr(web_module, "_PACKAGE_DIR", fake_pkg)
    client = TestClient(create_app(db_path=tmp_path / "web.db"), raise_server_exceptions=False)
    payload = client.get("/health").json()
    assert payload["source_git_sha"] == "UNKNOWN"


def test_r0a_real_routes_still_render(isolated_xdg: None, tmp_path: Path) -> None:
    """Surviving run/provider/routing routes still render after removals."""
    client = _client(tmp_path)
    _seed_run(tmp_path)
    assert client.get("/").status_code == 200
    assert client.get("/access").status_code == 200
    assert client.get("/routing").status_code == 200
    assert client.get("/orchestrator").status_code == 200
    assert client.get("/quota").status_code == 200
    assert client.get("/runs/run_r0a_probe").status_code == 200
    assert client.get("/health").status_code == 200


def test_r0a_manager_state_not_deleted(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup/doctor never delete persisted manager-session state."""
    from saberops.cli import _check_legacy_manager_state
    from saberops.paths import saberops_state_dir

    legacy_dir = saberops_state_dir() / "manager-session"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    sentinel = legacy_dir / "session.json"
    sentinel.write_text('{"transcript": "owner-data"}', encoding="utf-8")

    TestClient(create_app(db_path=tmp_path / "web.db"), raise_server_exceptions=False).get("/")

    finding = _check_legacy_manager_state()
    assert finding is not None
    assert finding.severity == "WARN"
    assert "legacy" in finding.message.lower()
    assert sentinel.is_file()
    assert sentinel.read_text(encoding="utf-8") == '{"transcript": "owner-data"}'


def test_r0a_cli_help_uses_product_naming() -> None:
    """CLI help presents SaberOps, never legacy product naming."""
    from saberops.cli import build_parser

    parser = build_parser()
    actions = {action.dest: action for action in parser._subparsers._group_actions}  # type: ignore[union-attr]
    _ = actions
    help_text = parser.format_help()
    assert "Orchestrator Dashboard" not in help_text
    assert "OpenCode ORX agent" not in help_text
    assert "Exact Orch executable ORX may run" not in help_text


# ---------------------------------------------------------------------------
# R0-A.1 — fail-closed executing source identity (temporary Git repos only).
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    completed = _subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_package_repo(base: Path) -> tuple[Path, Path]:
    """Create a clean committed SaberOps-like tree: <repo>/src/saberops."""
    repo = base / "repo"
    pkg = repo / "src" / "saberops"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "README.md").write_text("# probe\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "r0a1@example.com")
    _git(repo, "config", "user.name", "R0-A.1 Probe")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo, pkg


def test_r0a1_a_clean_tree_reports_head(tmp_path: Path) -> None:
    """A. Clean committed package tree reports exact HEAD."""
    from saberops.web import _resolve_source_identity

    repo, pkg = _make_package_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    sha, state = _resolve_source_identity(pkg)
    assert sha == head
    assert re.fullmatch(r"[0-9a-f]{40}", sha) is not None
    assert state == "CLEAN"


def test_r0a1_b_modified_tracked_source_reports_unknown(tmp_path: Path) -> None:
    """B. Modified tracked package source must not report HEAD."""
    from saberops.web import _resolve_source_identity

    repo, pkg = _make_package_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    assert head
    (pkg / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    sha, state = _resolve_source_identity(pkg)
    assert sha == "UNKNOWN"
    assert state == "DIRTY"


def test_r0a1_c_staged_source_change_reports_unknown(tmp_path: Path) -> None:
    """C. Staged package-source change must not report HEAD."""
    from saberops.web import _resolve_source_identity

    repo, pkg = _make_package_repo(tmp_path)
    (pkg / "__init__.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git(repo, "add", "src/saberops/__init__.py")
    sha, state = _resolve_source_identity(pkg)
    assert sha == "UNKNOWN"
    assert state == "DIRTY"


def test_r0a1_d_untracked_file_in_package_reports_unknown(tmp_path: Path) -> None:
    """D. Untracked file inside the executing package tree is dirty."""
    from saberops.web import _resolve_source_identity

    repo, pkg = _make_package_repo(tmp_path)
    _ = repo
    (pkg / "extra_probe.py").write_text("X = 1\n", encoding="utf-8")
    sha, state = _resolve_source_identity(pkg)
    assert sha == "UNKNOWN"
    assert state == "DIRTY"


def test_r0a1_e_unrelated_dirty_file_keeps_head(tmp_path: Path) -> None:
    """E. Dirty file outside the package tree must not invalidate the SHA."""
    from saberops.web import _resolve_source_identity

    repo, pkg = _make_package_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("# probe dirty\n", encoding="utf-8")
    sha, state = _resolve_source_identity(pkg)
    assert sha == head
    assert state == "CLEAN"


def test_r0a1_f_outside_git_reports_unknown(tmp_path: Path) -> None:
    """F. Installed package directory outside Git reports UNKNOWN."""
    from saberops.web import _resolve_source_identity

    pkg = tmp_path / "site-packages" / "saberops"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    sha, state = _resolve_source_identity(pkg)
    assert sha == "UNKNOWN"
    assert state == "NOT_GIT"


def test_r0a1_health_wires_fail_closed_identity(
    isolated_xdg: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health reflects the fail-closed rule when the package tree moves."""
    import saberops.web as web_module

    repo, pkg = _make_package_repo(tmp_path / "health")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(web_module, "_PACKAGE_DIR", pkg)
    client = TestClient(create_app(db_path=tmp_path / "web.db"), raise_server_exceptions=False)
    payload = client.get("/health").json()
    assert payload["source_git_sha"] == head
    assert payload["source_tree_state"] == "CLEAN"
    (pkg / "__init__.py").write_text("VALUE = 99\n", encoding="utf-8")
    dirty = client.get("/health").json()
    assert dirty["source_git_sha"] == "UNKNOWN"
    assert dirty["source_tree_state"] == "DIRTY"
