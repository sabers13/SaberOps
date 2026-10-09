"""Uploads retain provenance and reach model inputs without granting work authority."""

from __future__ import annotations

import base64
import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from saberops.chat import ChatCoordinator, ChatStore
from saberops.chat_attachments import MAX_FILE_BYTES, parse_ids
from saberops.models import WorkerRequest
from saberops.orchestrator_consumer import (
    OrchestratorModelConsumer,
    OrchestratorModelRequest,
    OrchestratorModelResult,
)
from saberops.process import ProcessResult
from saberops.project_bootstrap import prepare_project
from saberops.web import create_app
from saberops.workers.codex import CodexAdapter

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def test_attachment_binding_is_atomic_durable_and_message_scoped(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chats.db")
    first, second = store.create("/project", "First"), store.create("/project", "Second")
    uploaded = store.attachments.save("../../notes.md", b"Original file contents")
    assert uploaded["name"] == "notes.md"
    assert store.attachments.path(uploaded["id"]).parent == store.attachments.root.resolve()
    with pytest.raises(ValueError):
        store.claim(first, "Read this", "invalid", attachment_ids=(uploaded["id"], "missing"))
    assert not store.get(first)["busy"]  # type: ignore[index]
    token = store.claim(first, "Read this", "first-request", attachment_ids=(uploaded["id"],))
    assert token
    store.finish(first, token, "Read it")
    restored = ChatStore(store.path)
    assert restored.get(first)["messages"][0]["text"] == "Read this"  # type: ignore[index]
    assert restored.get(first)["messages"][0]["attachments"] == [uploaded]  # type: ignore[index]
    assert (
        restored.claim(first, "Read this", "first-request", attachment_ids=(uploaded["id"],))
        is None
    )
    with pytest.raises(ValueError):
        restored.claim(second, "Reuse", "other-request", attachment_ids=(uploaded["id"],))
    assert not restored.attachments.remove(uploaded["id"])
    loose = restored.attachments.save("loose.txt", b"Unsent")
    assert restored.attachments.remove(loose["id"])
    assert restored.attachments.get(loose["id"]) is None


@pytest.mark.parametrize(
    "name,content",
    [
        ("fake.png", b"not an image"),
        ("empty.txt", b""),
        ("tool.exe", b"binary"),
        ("bad.txt", b"\xff"),
        ("large.txt", b"x" * (MAX_FILE_BYTES + 1)),
    ],
)
def test_invalid_files_do_not_create_uploads(tmp_path: Path, name: str, content: bytes) -> None:
    store = ChatStore(tmp_path / "chats.db")
    with pytest.raises(ValueError):
        store.attachments.save(name, content)
    assert not store.attachments.root.exists()
    with pytest.raises(ValueError):
        parse_ids("../outside")


def test_docx_extraction_and_bounded_file_reference(tmp_path: Path) -> None:
    store = ChatStore(tmp_path / "chats.db")
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "word/document.xml", '<w:document xmlns:w="urn:test"><w:t>Hello file</w:t></w:document>'
        )
    uploaded = store.attachments.save("brief.docx", data.getvalue())
    assert store.attachments.text(uploaded["id"]) == "Hello file"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    files, images = store.attachments.prepare([{"attachments": [uploaded]}], scratch)
    assert files[0]["text"] == "Hello file" and files[0]["reference_only"]
    assert Path(files[0]["path"]).parent == scratch
    assert not images


def test_upload_routes_pass_images_and_text_without_dispatching_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    app = create_app(db_path=tmp_path / "runs.db", repo_path=repo)
    calls: list[dict[str, Any]] = []

    def invoke(
        self: OrchestratorModelConsumer, request: OrchestratorModelRequest
    ) -> OrchestratorModelResult:
        calls.append(json.loads(request.user_prompt))
        assert request.input_images and all(
            path.read_bytes() == PNG for path in request.input_images
        )
        assert request.input_images[0].parent == request.scratch_path
        assert "Please review the attached files." in request.user_prompt
        return OrchestratorModelResult(
            status="SUCCESS",
            binding_id="b",
            profile_id="p",
            quota_pool_id="q",
            provider="stub",
            model="vision",
            backend="stub",
            reasoning_effort=None,
            output_text='{"reply":"Read the picture and notes.","action":"work"}',
            summary="",
            error=None,
            prompt_bytes=0,
            duration_seconds=0,
            correlation_id=None,
            evidence_run_id=request.run_id or "",
            evidence_event_id=0,
        )

    monkeypatch.setattr(OrchestratorModelConsumer, "invoke", invoke)
    chats = cast(ChatCoordinator, app.state.chats)
    with TestClient(app) as client:
        picture = client.post("/chat/attachments", files={"file": ("image.png", PNG, "image/png")})
        notes = client.post(
            "/chat/attachments", files={"file": ("notes.md", b"Use a teal tag.", "text/plain")}
        )
        assert picture.status_code == notes.status_code == 201
        a, b = picture.json(), notes.json()
        downloaded = client.get(a["url"])
        assert (
            downloaded.content == PNG and downloaded.headers["x-content-type-options"] == "nosniff"
        )
        response = client.post(
            "/chats",
            data={"mode": "chat", "attachments": a["id"] + "," + b["id"]},
            follow_redirects=False,
        )
        assert response.status_code == 303
        chat_id = response.headers["location"].split("/")[-1]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            saved = chats.store.get(chat_id)
            if saved and not saved["busy"]:
                break
            time.sleep(0.01)
        assert saved and not saved["busy"]
        assert saved["messages"][-1]["text"] == "Read the picture and notes."
        assert len(saved["messages"][0]["attachments"]) == 2
        assert calls[0]["attachments"][1]["text"] == "Use a teal tag."
        assert not app.state.db.list_runs()
        html = client.get(f"/chats/{chat_id}").text
        assert a["url"] in html and "notes.md" in html
        assert len(calls) == 1
        assert client.delete(a["url"]).status_code == 409
        assert (
            client.post(
                "/chats/" + chat_id + "/messages",
                data={"message": "Bad file", "request_id": "bad", "attachments": "../outside"},
            ).status_code
            == 400
        )
        assert len(calls) == 1
        rejected = client.post(
            "/chats",
            data={"task": "Reuse", "mode": "chat", "attachments": a["id"]},
            follow_redirects=False,
        )
        assert rejected.status_code == 400 and len(calls) == 1
        fresh = client.post("/chat/attachments", files={"file": ("fresh.png", PNG)}).json()
        accepted = client.post(
            "/chats/" + chat_id + "/messages",
            data={"request_id": "file-only-followup", "mode": "chat", "attachments": fresh["id"]},
        )
        assert accepted.status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            followup = chats.store.get(chat_id)
            if followup and not followup["busy"]:
                break
            time.sleep(0.01)
        assert followup and not followup["busy"]
        assert followup["messages"][-2]["attachments"] == [fresh]
        assert followup["messages"][-1]["text"] == "Read the picture and notes."
        assert len(calls) == 2


def test_codex_image_input_preserves_readonly_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "reference.png"
    image.write_bytes(PNG)
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        calls.append(argv)
        return ProcessResult(argv, 0, "", "", 0.1)

    monkeypatch.setattr("saberops.workers.codex.GitManager.get_changed_paths", lambda path: [])
    adapter = CodexAdapter(runner=runner)
    adapter.run(
        WorkerRequest(
            "Describe the image", tmp_path, "test-model", read_only=True, input_images=(image,)
        )
    )
    assert calls and calls[0][calls[0].index("--image") + 1] == str(image)
    assert calls[0][calls[0].index("--sandbox") + 1] == "read-only"
