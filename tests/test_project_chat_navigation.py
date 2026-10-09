"""Names and project navigation preserve the execution objective and isolation."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from saberops.chat import ChatStore
from saberops.db import Database
from saberops.models import Run, RunStatus, Tier
from saberops.navigation import project_folders
from saberops.orchestrator import _InitialOrchestratorGuidanceCache
from saberops.orchestrator_consumer import (
    OrchestratorModelConsumer,
    OrchestratorModelInvocationError,
    OrchestratorModelRequest,
    OrchestratorModelResult,
)
from saberops.project_bootstrap import prepare_project
from saberops.run_titles import clean_title, fallback_title, parse_named_guidance
from saberops.runtime import ProjectRuntimeManager
from saberops.web import create_app


def run(repo: str, identity: str = "run_navigation") -> Run:
    return Run(
        id=identity,
        task="Build “Incident Desk,” an issue tracker in " + repo,
        target_repo=repo,
        base_commit="base",
        tier=Tier.T1,
        training_allowed=False,
        gate_command="true",
        status=RunStatus.COMPLETED,
        created_at="2026-10-09T10:00:00Z",
    )


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    for name in (
        "SABEROPS_PROFILE",
        "ORCH_BINDING_STORE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_READINESS_STORE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_legacy_name_migration_preserves_objective(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    original = run("/repos/project")
    Database(path).create_run(original)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE runs DROP COLUMN display_title")
    legacy = Database(path, read_only=True).get_run(original.id)
    assert legacy and legacy.display_title is None
    migrated = Database(path)
    migrated.set_run_title(original.id, "Issue tracker")
    saved = migrated.get_run(original.id)
    assert saved and saved.task == original.task and saved.status == original.status
    assert saved.display_title == "Issue tracker"
    assert migrated.list_runs()[0].display_title == "Issue tracker"


def test_run_title_survives_creation_and_lifecycle_updates(tmp_path: Path) -> None:
    db = Database(tmp_path / "runs.db")
    original = replace(run("/repos/project"), display_title="Issue tracker")
    db.create_run(original)
    db.update_run(original.id, RunStatus.FAILED, result_summary="Gate failed")
    saved = Database(db.db_path).get_run(original.id)
    assert saved and saved.display_title == "Issue tracker" and saved.task == original.task


def test_name_bounds_and_transcript_fallback() -> None:
    repo = "/repos/my-project"
    assert (
        fallback_title("Owner conversation: Build “Incident Desk,” in " + repo, repo)
        == "Build Incident Desk"
    )
    assert (
        fallback_title("Implement the following brief:\nFix login expiry", repo)
        == "Fix login expiry"
    )
    label = clean_title("my-project /repos/my-project <b>Fix login\n" + "problem " * 20, repo)
    assert len(label) <= 80 and len(label.split()) <= 10
    assert "my-project" not in label and "/repos" not in label and "<" not in label
    assert fallback_title("", repo) == "Engineering task"
    assert parse_named_guidance("Legacy advisory") == ("", "Legacy advisory")
    assert parse_named_guidance('{"title": [], "guidance": "Keep checks."}') == ("", "Keep checks.")


@pytest.mark.parametrize(
    "output, expected, save_error",
    [
        (
            json.dumps({"title": "project Fix login", "guidance": "Keep checks."}),
            "Fix login",
            False,
        ),
        (json.dumps({"title": "Fix login", "guidance": "Keep checks."}), None, True),
        ("Legacy advisory", None, False),
        ('{"title": "Fix login", "guidance": false}', None, False),
    ],
)
def test_startup_names_with_one_existing_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    expected: str | None,
    save_error: bool,
) -> None:
    db = Database(tmp_path / "runs.db")
    original = run("/repos/project")
    db.create_run(original)
    requests: list[OrchestratorModelRequest] = []
    db.create_run(
        replace(
            run(original.target_repo, "earlier_run"),
            display_title="Build previous account settings",
        )
    )

    def invoke(
        self: OrchestratorModelConsumer, request: OrchestratorModelRequest
    ) -> OrchestratorModelResult:
        requests.append(request)
        return OrchestratorModelResult(
            status="SUCCESS",
            binding_id="b",
            profile_id="p",
            quota_pool_id="q",
            provider="stub",
            model="stub",
            backend="stub",
            reasoning_effort=None,
            output_text=output,
            summary="",
            error=None,
            prompt_bytes=0,
            duration_seconds=0,
            correlation_id=None,
            evidence_run_id=original.id,
            evidence_event_id=0,
        )

    monkeypatch.setattr(OrchestratorModelConsumer, "invoke", invoke)
    if save_error:

        def fail_save(self: Database, run_id: str, title: str) -> None:
            raise sqlite3.OperationalError("Read-only store")

        monkeypatch.setattr(Database, "set_run_title", fail_save)
    cache = _InitialOrchestratorGuidanceCache()
    service = SimpleNamespace(db=db)
    guidance = cache.get_or_fetch(service=service, original_task=original.task, run_id=original.id)
    assert (
        cache.get_or_fetch(service=service, original_task=original.task, run_id=original.id)
        == guidance
    )
    assert len(requests) == 1 and "title and guidance" in requests[0].system_prompt
    assert "4–10 word" in requests[0].system_prompt
    assert "Build previous account settings" in requests[0].user_prompt
    saved = db.get_run(original.id)
    assert (
        saved
        and saved.display_title == (expected or fallback_title(original.task, original.target_repo))
        and saved.task == original.task
    )
    if expected or save_error:
        assert guidance == ("Keep checks.", None)
    assert (
        sum(e.event_type == "orchestrator_guidance_result" for e in db.get_events(original.id)) == 1
    )


def test_unavailable_orchestrator_keeps_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "runs.db")
    original = run("/repos/project")
    db.create_run(original)

    def refuse(
        self: OrchestratorModelConsumer, request: OrchestratorModelRequest
    ) -> OrchestratorModelResult:
        raise OrchestratorModelInvocationError("ORCH_MODEL_NO_POLICY", message="Unavailable")

    monkeypatch.setattr(OrchestratorModelConsumer, "invoke", refuse)
    cache = _InitialOrchestratorGuidanceCache()
    text, reason = cache.get_or_fetch(
        service=SimpleNamespace(db=db), original_task=original.task, run_id=original.id
    )
    assert not text and reason
    saved = db.get_run(original.id)
    assert (
        saved
        and saved.task == original.task
        and fallback_title(saved.task, saved.target_repo) == "Build Incident Desk issue tracker"
    )


def test_project_folders_do_not_mix_same_named_repositories(tmp_path: Path) -> None:
    a, b = str(tmp_path / "a" / "repo"), str(tmp_path / "b" / "repo")
    manager = ProjectRuntimeManager(pinned_db_path=tmp_path / "runs.db")
    manager.owner_db.create_run(run(a, "run_a"))
    manager.owner_db.create_run(run(b, "run_b"))
    store = ChatStore(tmp_path / "chats.db")
    chat_a, chat_b = store.create(a, "A discussion"), store.create(b, "B discussion")
    before = manager.owner_db.db_path.read_bytes()
    groups = project_folders(manager, store, [a, b], a)
    assert [g["path"] for g in groups] == [a, b]
    assert {i["id"] for i in groups[0]["entries"]} == {"run_a", chat_a}
    assert {i["id"] for i in groups[1]["entries"]} == {"run_b", chat_b}
    assert all(
        i["title"] == "Build Incident Desk issue tracker"
        for g in groups
        for i in g["entries"]
        if i["kind"] == "run"
    )
    assert manager.owner_db.db_path.read_bytes() == before


def test_project_local_history_does_not_create_runtimes(tmp_path: Path) -> None:
    a, b = tmp_path / "a" / "repo", tmp_path / "b" / "repo"
    assert prepare_project(a)[0] and prepare_project(b)[0]
    manager = ProjectRuntimeManager(
        state_root=tmp_path / "projects", owner_db_path=tmp_path / "owner.db"
    )
    db_a, db_b = manager.for_repo(a).db, manager.for_repo(b).db
    # One busy project must not hide an older run in another repository.
    for index in range(55):
        db_a.create_run(run(str(a), f"run_a_{index}"))
    db_b.create_run(replace(run(str(b), "run_b"), created_at="2025-01-01T00:00:00Z"))
    manager.shutdown()
    fresh = ProjectRuntimeManager(
        state_root=tmp_path / "projects", owner_db_path=tmp_path / "owner.db"
    )
    store = ChatStore(tmp_path / "chats.db")
    snapshot = db_b.db_path.read_bytes()
    groups = project_folders(fresh, store, [str(a), str(b)], str(a))
    assert len(groups[0]["entries"]) == 50
    assert [item["id"] for item in groups[1]["entries"]] == ["run_b"]
    assert groups[0]["label"] != groups[1]["label"]
    assert not fresh._runtimes
    assert db_b.db_path.read_bytes() == snapshot


def test_run_and_chat_share_shell_and_polling_skips_navigation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    assert prepare_project(repo)[0]
    app = create_app(db_path=tmp_path / "runs.db", repo_path=repo)
    app.state.db.create_run(run(str(repo)))
    store: ChatStore = app.state.chats.store
    chat_id = store.create(str(repo), "A discussion")
    store.link_run(chat_id, "run_navigation")
    client = TestClient(app)
    response = client.get("/runs/run_navigation")
    assert response.status_code == 200
    assert "chat-shell run-chat-shell" in response.text
    assert (
        'data-history-kind="run"' in response.text and 'data-history-kind="chat"' in response.text
    )
    assert 'data-tab="chat" aria-selected="true"' in response.text
    assert "chat-shell.js" in response.text and "vendor/chat/marked.js" in response.text
    assert 'data-run-refresh="display-title">Build Incident Desk issue tracker' in response.text
    assert response.text.count('id="chatMessage"') == 1
    active = app.state.project_registry.active()

    def unexpected(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise AssertionError("Polling must not read project history")

    monkeypatch.setattr("saberops.web.project_folders", unexpected)
    assert client.get(f"/chats/{chat_id}/messages").status_code == 200
    assert app.state.project_registry.active() == active


def test_duplicate_names_are_reserved_atomically_and_survive_restart(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    db = Database(tmp_path / "runs.db")
    originals = [run("/repos/project", f"run_{index}") for index in range(8)]
    for original in originals:
        db.create_run(original)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda item: db.set_run_title(item.id, "Build shared issue tracker"), originals
            )
        )
    titles = {item.id: item.display_title for item in db.list_runs()}
    assert len({title.casefold() for title in titles.values() if title}) == 8
    restarted = Database(db.db_path)
    assert {item.id: item.display_title for item in restarted.list_runs()} == titles
    assert all(restarted.get_run(item.id).task == item.task for item in originals)  # type: ignore[union-attr]
    db.create_run(
        replace(run("/repos/another", "run_other"), display_title="Build shared issue tracker")
    )
    assert db.get_run("run_other").display_title == "Build shared issue tracker"  # type: ignore[union-attr]


def test_chat_names_are_descriptive_unique_and_durable(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chats.db")
    task = "Build “Incident Desk,” a complete local issue-tracking application."
    first = store.create("/repos/project", task)
    second = store.create("/repos/project", task)
    assert store.get(first)["title"] == "Discuss Incident Desk issue tracker"  # type: ignore[index]
    assert store.get(second)["title"] == "Discuss Incident Desk issue tracker · 2"  # type: ignore[index]
    restarted = ChatStore(store.path)
    assert restarted.get(second)["title"] == store.get(second)["title"]  # type: ignore[index]


def test_legacy_labels_upgrade_without_rewriting_transcripts(tmp_path: Path) -> None:
    db = Database(tmp_path / "runs.db")
    db.create_run(run("/repos/project", "first"))
    db.create_run(run("/repos/project", "second"))
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE runs SET display_title='Incident Desk'")
    upgraded = Database(db.db_path)
    labels = [item.display_title for item in upgraded.list_runs()]
    assert set(labels) == {
        "Build Incident Desk issue tracker",
        "Build Incident Desk issue tracker · 2",
    }
    store = ChatStore(tmp_path / "chats.db")
    original = "Build “Workflow Lab,” a local project-management application with a task board."
    chat_id = store.create("/repos/project", original)
    with store.connect() as conn:
        conn.execute("UPDATE conversations SET title='Workflow Lab' WHERE id=?", (chat_id,))
        conn.execute("DELETE FROM chat_metadata WHERE key='balanced_titles_v1'")
        conn.execute(
            "INSERT INTO messages (chat_id,role,text,created) VALUES (?,'user',?,1)",
            (chat_id, original),
        )
    upgraded_chat = ChatStore(store.path).get(chat_id)
    assert upgraded_chat and upgraded_chat["title"] == "Discuss Workflow Lab project management"
    assert upgraded_chat["messages"][0]["text"] == original


def test_owner_pages_share_navigation_and_keep_work_forms(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    assert prepare_project(repo)[0]
    app = create_app(db_path=tmp_path / "runs.db", repo_path=repo)
    app.state.db.create_run(run(str(repo)))
    client = TestClient(app)
    for path in (
        "/",
        "/?pane=runs",
        "/access",
        "/routing",
        "/setup",
        "/quota",
        "/runs/run_navigation",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert 'class="app workspace-shell' in response.text
        assert response.text.count('data-od-id="pane-providers"') == 1
        assert response.text.count('aria-label="Main navigation"') == 1
        assert "Search chats and runs" in response.text
        assert "chat-shell.js" in response.text
    response = client.get("/runs/run_navigation")
    assert '<nav data-work-only class="run-tabs"' in response.text
    assert '<details data-work-only class="run-recorded-brief"' in response.text
    assert 'action="/runs/run_navigation/conversation"' in response.text


def test_run_composer_honors_mode_without_existing_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    assert prepare_project(repo)[0]
    app = create_app(db_path=tmp_path / "runs.db", repo_path=repo)
    app.state.db.create_run(run(str(repo)))
    store: ChatStore = app.state.chats.store
    observed: list[str] = []

    def submit(
        self: object, chat_id: str, message: str, request_id: str, *, mode: str = "auto"
    ) -> None:
        observed.append(mode)

    monkeypatch.setattr("saberops.chat.ChatCoordinator.submit", submit)
    client = TestClient(app, follow_redirects=False)
    invalid = client.post(
        "/runs/run_navigation/conversation", data={"message": "Fix login", "mode": "invalid"}
    )
    assert invalid.status_code == 400 and store.for_run("run_navigation") is None
    for data, expected in [
        ({"message": "Fix login", "mode": "chat"}, "chat"),
        ({"message": "Fix login", "mode": "work"}, "work"),
        ({"message": "Legacy request"}, "auto"),
    ]:
        assert client.post("/runs/run_navigation/conversation", data=data).status_code == 303
        assert observed[-1] == expected
    assert store.for_run("run_navigation") is not None


def test_work_links_use_one_run_view_and_console_inspector(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    assert prepare_project(repo)[0]
    app = create_app(db_path=tmp_path / "runs.db", repo_path=repo)
    store: ChatStore = app.state.chats.store
    source = store.create(str(repo), "Original discussion", kind="chat")
    work = store.promote(source, "Implement the approved task", "convert")
    client = TestClient(app)
    pending = client.get(f"/chats/{work}")
    assert pending.status_code == 200
    assert 'id="agentSessionSelect"' in pending.text
    assert 'id="agentOpenAllTmux"' in pending.text
    assert "chatInspectorContent" not in pending.text and "chat-inspector.js" not in pending.text
    assert client.get(f"/chats/{work}/messages").json()["work_url"] is None
    app.state.db.create_run(run(str(repo)))
    store.link_run(work, "run_navigation")
    expected = "/runs/run_navigation?from=runs#work"
    link = client.get(f"/chats/{work}", follow_redirects=False)
    assert link.status_code == 307 and link.headers["location"] == expected
    page = client.get(f"/chats/{work}")
    assert page.status_code == 200 and page.url.path == "/runs/run_navigation"
    assert 'id="agentSessionSelect"' in page.text and 'data-tab="checks"' in page.text
    assert "chatInspectorContent" not in page.text and "chat-inspector.js" not in page.text
    assert client.get(f"/chats/{work}/messages").json()["work_url"] == expected
    assert client.get(f"/chats/{source}", follow_redirects=False).status_code == 200
    assert client.get(f"/chats/{source}/messages").json()["work_url"] is None
    # A later attempt opens its exact run, while a missing link cannot send users
    # to a stale earlier run or an unrelated project.
    app.state.db.create_run(run(str(repo), "run_second"))
    store.link_run(work, "run_second")
    assert client.get(f"/chats/{work}", follow_redirects=False).headers["location"] == (
        "/runs/run_second?from=runs#work"
    )
    store.link_run(work, "run_missing")
    assert client.get(f"/chats/{work}", follow_redirects=False).status_code == 200
    assert client.get(f"/chats/{work}/messages").json()["work_url"] is None
    assert store.get(source)["messages"] == []  # type: ignore[index]
