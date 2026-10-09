"""Conversations preserve objectives without bypassing governed worker dispatch."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saberops.chat import ChatBusy, ChatCoordinator, ChatStore, parse_reply
from saberops.ownership import ProcessIdentity
from saberops.project_bootstrap import prepare_project
from saberops.web import create_app


@pytest.fixture()
def chat_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
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
    repo = tmp_path / "project"
    assert prepare_project(repo)[0]
    return create_app(db_path=tmp_path / "runs.db", repo_path=repo)


def coordinator(app: FastAPI) -> ChatCoordinator:
    return cast(ChatCoordinator, app.state.chats)


def wait_reply(store: ChatStore, chat_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        chat = store.get(chat_id)
        if chat and not chat["busy"]:
            return chat
        time.sleep(0.01)
    raise AssertionError("conversation reply did not finish")


def test_store_atomic_claim_idempotency_and_restart(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chat.db")
    chat_id = store.create("/project", "Original objective")
    tokens: list[str | None] = []

    def claim() -> None:
        tokens.append(store.claim(chat_id, "hello", "same-request"))

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(token is not None for token in tokens) == 1
    with pytest.raises(ChatBusy):
        store.claim(chat_id, "next", "new-request")
    token = next(token for token in tokens if token)
    store.finish(chat_id, "wrong-token", "Must not appear")
    store.recover()
    assert store.get(chat_id)["busy"]  # type: ignore[index]
    dead = ProcessIdentity.for_self().to_dict()
    dead["pid"] = 2147483647
    with store.connect() as conn:
        conn.execute("UPDATE conversations SET owner=? WHERE id=?", (json.dumps(dead), chat_id))
    store.recover()
    store.finish(chat_id, token, "Late completion must not replace interruption")
    restored = ChatStore(tmp_path / "chat.db").get(chat_id)
    assert restored and not restored["busy"]
    assert len(restored["messages"]) == 2
    assert "interrupted" in restored["messages"][-1]["text"]


def test_question_is_remembered_without_run_and_get_never_invokes(
    chat_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chats = coordinator(chat_app)
    calls: list[dict[str, Any]] = []

    def ask(
        self: ChatCoordinator, chat: dict[str, Any], *, outcome: str = ""
    ) -> tuple[str, str, str]:
        calls.append(self._context(chat))
        return "Hello! Let's talk about your project.", "respond", "fake / exact-model"

    monkeypatch.setattr(ChatCoordinator, "_ask", ask)
    with TestClient(chat_app) as client:
        response = client.post(
            "/chats", data={"task": "Hello, what can you do?"}, follow_redirects=False
        )
        assert response.status_code == 303
        chat_id = response.headers["location"].split("/")[-1]
        initial = wait_reply(chats.store, chat_id)
        assert initial["messages"][-1]["model"] == "fake / exact-model"
        for _ in range(3):
            assert client.get(f"/chats/{chat_id}/messages").status_code == 200
        assert len(calls) == 1
        response = client.post(
            f"/chats/{chat_id}/messages",
            data={"message": "Explain that", "request_id": "follow-up"},
        )
        assert response.status_code == 202
        wait_reply(chats.store, chat_id)
        client.post(
            f"/chats/{chat_id}/messages",
            data={"message": "Explain that", "request_id": "follow-up"},
        )
        assert len(calls) == 2
        assert calls[-1]["messages"][0]["text"] == "Hello, what can you do?"
        assert calls[-1]["messages"][-1]["text"] == "Explain that"
        assert chat_app.state.db.list_runs() == []
        page = client.get(f"/chats/{chat_id}")
        assert "data-chat-form" in page.text and "chatModelButton" in page.text
        assert "fake / exact-model" in page.text


@pytest.mark.parametrize("quick", [False, True])
def test_work_uses_original_objective_project_and_shared_policy(
    chat_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    quick: bool,
) -> None:
    from saberops.project_settings import set_project_gate_command, set_project_workflow
    from saberops.supervisor import RunSupervisor

    chats = coordinator(chat_app)
    repo = chat_app.state.project_registry.active()
    if quick:
        set_project_workflow(repo, preset="quick")
    else:
        set_project_gate_command(repo, "make gate")
    launched: list[Any] = []

    def start(self: RunSupervisor, config: Any) -> Any:
        from types import SimpleNamespace

        launched.append(config)
        return SimpleNamespace(run_id="fake-run")

    def ask(
        self: ChatCoordinator, chat: dict[str, Any], *, outcome: str = ""
    ) -> tuple[str, str, str]:
        return "I'll implement that.", "work", "fake / exact-model"

    monkeypatch.setattr(ChatCoordinator, "_ask", ask)
    monkeypatch.setattr(RunSupervisor, "start_supervised_run", start)
    chat_id = chats.store.create(str(repo), "Build a board")
    token = chats.store.claim(chat_id, "Build a task board with SQLite", "original")
    assert token
    chats.store.finish(chat_id, token, "What else?")
    chats.submit(chat_id, "Also add CSV export", "follow-up")
    chat = wait_reply(chats.store, chat_id)
    assert len(launched) == 1
    config = launched[0]
    assert config.target_repo == str(repo)
    assert "Build a task board with SQLite" in config.task
    assert "Also add CSV export" in config.task
    assert config.provider_override is None and config.model_override is None
    if quick:
        assert not config.gate_enabled and config.review_disabled
        assert "project policy disables the gate" in chat["messages"][-1]["text"]
    else:
        assert config.gate_enabled and config.review_enabled and not config.review_disabled
        assert f"`{config.gate_command}` from the project root" in chat["messages"][-1]["text"]
        assert "Independent SaberOps review is requested" in chat["messages"][-1]["text"]
    assert chat["messages"][-1]["run_id"] == "fake-run"


def test_provider_failure_keeps_message_and_does_not_dispatch(
    chat_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chats = coordinator(chat_app)

    def ask(
        self: ChatCoordinator, chat: dict[str, Any], *, outcome: str = ""
    ) -> tuple[str, str, str]:
        raise ValueError("Private transport details must not be shown")

    monkeypatch.setattr(ChatCoordinator, "_ask", ask)
    chat_id = chats.store.create(str(chat_app.state.project_registry.active()), "hello")
    chats.submit(chat_id, "hello", "first")
    chat = wait_reply(chats.store, chat_id)
    assert chat["messages"][0]["text"] == "hello"
    assert "Private transport" not in chat["messages"][-1]["text"]
    assert chat_app.state.db.list_runs() == []


def test_invalid_actions_never_authorize_work() -> None:
    assert parse_reply("Plain conversational response")[1] == "respond"
    for output in ('{"reply":"ok","action":"accept"}', '{"action":"work"}', "[]"):
        with pytest.raises(ValueError):
            parse_reply(output)


def test_chat_html_escapes_model_output(chat_app: FastAPI) -> None:
    chats = coordinator(chat_app)
    chat_id = chats.store.create("/repo", "Escaping")
    token = chats.store.claim(chat_id, "Hi", "first")
    assert token
    chats.store.finish(chat_id, token, '<script>alert("bad")</script>')
    page = TestClient(chat_app).get(f"/chats/{chat_id}")
    assert '<script>alert("bad")</script>' not in page.text
    assert "&lt;script&gt;" in page.text
    assert page.status_code == 200


def test_cross_site_posts_cannot_create_conversation_or_launch(chat_app: FastAPI) -> None:
    client = TestClient(chat_app)
    for endpoint, fields in (
        ("/chats", {"task": "implement something"}),
        ("/chats/missing/messages", {"message": "implement"}),
        ("/runs/missing/try-worker", {"binding_id": "worker"}),
    ):
        response = client.post(
            endpoint, data=fields, headers={"Origin": "https://unrelated.example"}
        )
        assert response.status_code == 403
    assert coordinator(chat_app).store.recent(str(chat_app.state.project_registry.active())) == []
    assert chat_app.state.db.list_runs() == []


def test_terminal_result_is_reported_once_even_after_restart(
    chat_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saberops.db import current_iso_timestamp
    from saberops.models import Run, RunStatus, Tier

    chats = coordinator(chat_app)
    repo = chat_app.state.project_registry.active()
    run = Run(
        id="failed-chat-run",
        task="Build a board",
        target_repo=str(repo),
        base_commit="base",
        tier=Tier.T2,
        training_allowed=True,
        gate_command="true",
        status=RunStatus.FAILED,
        created_at=current_iso_timestamp(),
        result_summary="No eligible worker",
    )
    chat_app.state.db.create_run(run)
    chat_app.state.db.record_event(
        run.id,
        "no_eligible_candidate",
        payload={
            "routing_context": "T2.default",
            "skipped_candidates": [
                {
                    "provider": "opencode",
                    "model": "chosen",
                    "reason": "provider_readiness_unknown",
                    "readiness_state": "UNKNOWN",
                    "binding_id": "exact-chosen",
                }
            ],
        },
    )
    chat_id = chats.store.create(str(repo), run.task)
    chats.store.link_run(chat_id, run.id)

    def ask(
        self: ChatCoordinator, chat: dict[str, Any], *, outcome: str = ""
    ) -> tuple[str, str, str]:
        assert "No worker was dispatched" in outcome
        return "No worker started because readiness is unknown.", "work", "fake / model"

    monkeypatch.setattr(ChatCoordinator, "_ask", ask)
    chats._check_runs()
    chat = chats.store.get(chat_id)
    assert chat and not chat["busy"] and len(chat["messages"]) == 1
    assert chat["messages"][0]["run_id"] == run.id
    assert chats.store.pending_runs() == []
    chats._check_runs()
    assert len(chats.store.get(chat_id)["messages"]) == 1  # type: ignore[index]
    # An outcome's model suggestion can never launch new work.
    assert len(chat_app.state.db.list_runs()) == 1


def test_worker_bootstrap_requires_exact_blocked_owner_choice_and_keeps_policy(
    chat_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import saberops.routing_config as routing_config
    import saberops.web as web
    from saberops.config import resolve_run_config
    from saberops.db import current_iso_timestamp
    from saberops.models import Run, RunStatus, Tier, WorkerCandidate
    from saberops.supervisor import RunSupervisor

    repo = chat_app.state.project_registry.active()
    config = resolve_run_config(
        task="Original task",
        target_repo=repo,
        manual_tier=Tier.T2,
        routing_mode="manual",
        gate_command="true",
    )
    run = Run(
        id="blocked-worker-run",
        task=config.task,
        target_repo=str(repo),
        base_commit="base",
        tier=Tier.T2,
        training_allowed=True,
        gate_command="true",
        status=RunStatus.FAILED,
        created_at=current_iso_timestamp(),
        config_json=json.dumps(config.as_dict()),
    )
    chat_app.state.db.create_run(run)
    candidate = WorkerCandidate(provider="opencode", model="exact-model", binding_id="owner-picked")
    monkeypatch.setattr(
        routing_config,
        "load_effective_routing",
        lambda: SimpleNamespace(
            chain_for=lambda *args: [
                WorkerCandidate(provider=candidate.provider, model=candidate.model)
            ],
            approved_candidates=(
                SimpleNamespace(
                    candidate_id="opencode/exact-model",
                    binding_id=candidate.binding_id,
                    training_required="UNKNOWN",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        web,
        "build_run_report",
        lambda *args: {
            "termination": {
                "skipped_candidates": [
                    {
                        "binding_id": "owner-picked",
                        "provider": "opencode",
                        "model": "exact-model",
                        "readiness_state": "UNKNOWN",
                    }
                ]
            },
        },
    )
    launched: list[Any] = []

    def start(self: RunSupervisor, config: Any) -> Any:
        launched.append(config)
        return SimpleNamespace(run_id=run.id)

    monkeypatch.setattr(RunSupervisor, "start_supervised_run", start)
    client = TestClient(chat_app)
    for identity in ("", "unpicked", "same-model-different-account"):
        assert (
            client.post(f"/runs/{run.id}/try-worker", data={"binding_id": identity}).status_code
            == 400
        )
    assert not launched
    response = client.post(
        f"/runs/{run.id}/try-worker", data={"binding_id": "owner-picked"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert len(launched) == 1
    assert launched[0].provider_override == "opencode"
    assert launched[0].model_override == "exact-model"
    assert launched[0].task == config.task
    assert launched[0].target_repo == config.target_repo
    assert launched[0].gate_enabled == config.gate_enabled
    assert launched[0].gate_command == config.gate_command
    assert launched[0].review_policy_mode == config.review_policy_mode


def test_chat_mode_blocks_model_work_and_promotion_creates_separate_work_thread(
    chat_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from saberops.project_settings import set_project_workflow
    from saberops.supervisor import RunSupervisor

    chats = coordinator(chat_app)
    repo = chat_app.state.project_registry.active()
    set_project_workflow(repo, preset="quick")
    launched: list[Any] = []

    def start(self: RunSupervisor, config: Any) -> Any:
        launched.append(config)
        return SimpleNamespace(run_id="promoted-run")

    def ask(
        self: ChatCoordinator, chat: dict[str, Any], *, outcome: str = ""
    ) -> tuple[str, str, str]:
        return "A suggested implementation", "work", "fake / model"

    monkeypatch.setattr(ChatCoordinator, "_ask", ask)
    monkeypatch.setattr(RunSupervisor, "start_supervised_run", start)
    with TestClient(chat_app) as client:
        response = client.post(
            "/chats", data={"task": "Implement a board", "mode": "chat"}, follow_redirects=False
        )
        chat_id = response.headers["location"].split("/")[-1]
        initial = wait_reply(chats.store, chat_id)
        assert initial["mode"] == "chat" and not launched
        assert initial["messages"][-1]["run_id"] is None
        # Idempotent retries cannot change an accepted turn's authority.
        client.post(
            f"/chats/{chat_id}/messages",
            data={"message": "Implement a board", "request_id": "first-message", "mode": "work"},
        )
        assert not launched and chats.store.get(chat_id)["mode"] == "chat"  # type: ignore[index]
        response = client.post(
            f"/chats/{chat_id}/messages",
            data={
                "message": "Implement the following brief: board with CSV export",
                "request_id": "promote",
                "mode": "work",
            },
        )
        assert response.status_code == 202
        work_id = response.json()["work_chat_id"]
        assert work_id != chat_id
        promoted = wait_reply(chats.store, work_id)
        assert promoted["mode"] == "work" and promoted["kind"] == "work"
        assert promoted["source_chat_id"] == chat_id and len(launched) == 1
        assert "board with CSV export" in launched[0].task
        assert (
            promoted["messages"][0]["text"]
            == "Implement the following brief: board with CSV export"
        )
        original = chats.store.get(chat_id)
        assert original and original["messages"] == initial["messages"]
        assert original["kind"] == "chat" and original["mode"] == "chat"
        assert client.get(f"/chats/{chat_id}/messages").json()["runs"] == []
        assert client.get(f"/chats/{work_id}/messages").json()["runs"] == [
            {"run_id": "promoted-run"}
        ]
        retried = client.post(
            f"/chats/{chat_id}/work",
            data={
                "message": "Implement the following brief: board with CSV export",
                "request_id": "promote",
            },
        )
        assert retried.status_code == 202 and retried.json()["work_chat_id"] == work_id
        assert len(launched) == 1


def test_stopped_reply_cannot_dispatch_even_if_provider_returns_work_late(
    chat_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    chats = coordinator(chat_app)

    def ask(
        self: ChatCoordinator, chat: dict[str, Any], *, outcome: str = ""
    ) -> tuple[str, str, str]:
        entered.set()
        assert release.wait(5)
        return "Implementation suggestion", "work", "fake / model"

    monkeypatch.setattr(ChatCoordinator, "_ask", ask)
    with TestClient(chat_app) as client:
        response = client.post(
            "/chats", data={"task": "Implement this", "mode": "work"}, follow_redirects=False
        )
        chat_id = response.headers["location"].split("/")[-1]
        assert entered.wait(5)
        assert client.get(f"/chats/{chat_id}/messages").json()["can_stop"]
        assert (
            client.post(
                f"/chats/{chat_id}/stop", headers={"Origin": "https://other.example"}
            ).status_code
            == 403
        )
        assert client.post(f"/chats/{chat_id}/stop").status_code == 202
        release.set()
        stopped = wait_reply(chats.store, chat_id)
        assert "Reply stopped" in stopped["messages"][-1]["text"]
        assert chat_app.state.db.list_runs() == []
        assert client.post(f"/chats/{chat_id}/stop").status_code == 409
        assert (
            client.post(
                f"/chats/{chat_id}/messages",
                data={"message": "Hello", "request_id": "invalid", "mode": "unknown"},
            ).status_code
            == 400
        )
        assert len(chats.store.get(chat_id)["messages"]) == 2  # type: ignore[index]


def test_linked_work_retains_source_images_binds_new_files_and_rejects_cross_site(
    chat_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import base64

    seen: list[dict[str, Any]] = []

    def ask(
        self: ChatCoordinator, chat: dict[str, Any], *, outcome: str = ""
    ) -> tuple[str, str, str]:
        seen.append(chat)
        return "Work brief received", "respond", "fake / model"

    monkeypatch.setattr(ChatCoordinator, "_ask", ask)
    chats = coordinator(chat_app)
    with TestClient(chat_app) as client:
        source = chats.store.create(
            str(chat_app.state.project_registry.active()), "Discuss an image", kind="chat"
        )
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
        )
        image = chats.store.attachments.save("layout.png", png)
        token = chats.store.claim(
            source, "Explain this layout", "image", mode="chat", attachment_ids=(image["id"],)
        )
        assert token
        chats.store.finish(source, token, "Discussion of the layout")
        original = chats.store.get(source)
        file = client.post("/chat/attachments", files={"file": ("brief.txt", b"Approved scope")})
        assert file.status_code == 201
        fields = {
            "message": "Implement the approved layout",
            "request_id": "convert",
            "attachments": file.json()["id"],
        }
        assert (
            client.post(
                f"/chats/{source}/work",
                data=fields,
                headers={"Origin": "https://unrelated.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/chats/{source}/work", data={**fields, "attachments": "file_" + "0" * 32}
            ).status_code
            == 400
        )
        assert not chats.store.get(source)["work_children"]  # type: ignore[index]
        response = client.post(f"/chats/{source}/work", data=fields)
        assert response.status_code == 202
        work = wait_reply(chats.store, response.json()["work_chat_id"])
        assert len(seen) == 1 and seen[0]["source_discussion"][0]["attachments"] == [image]
        assert work["messages"][0]["attachments"] == [file.json()]
        assert chats.store.get(source)["messages"] == original["messages"]  # type: ignore[index]
        assert client.get(image["url"]).content == png
        scratch = tmp_path / "staged"
        scratch.mkdir()
        files, images = chats.store.attachments.prepare(
            work["source_discussion"] + work["messages"],
            scratch,
        )
        assert len(files) == 2 and files[1]["text"] == "Approved scope"
        assert len(images) == 1 and images[0].read_bytes() == png
        assert (
            client.post(f"/chats/{source}/work", data=fields).json()["work_chat_id"] == work["id"]
        )
        assert len(seen) == 1
