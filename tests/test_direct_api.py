"""Direct API execution contracts using a local HTTP server, never paid inference."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from saberops.control_plane.bindings import BindingKind, resolve_execution_binding
from saberops.model_access.api_verification import verify_api
from saberops.model_access.connections import (
    AccessProtocol,
    AccessStore,
    AuthMode,
    ProviderConnection,
    build_access_registry,
)
from saberops.models import AttemptStatus, WorkerRequest
from saberops.web import _discover_connection, _load_owner_bindings_safely
from saberops.workers.api_agent import completion_url, execute_tool
from saberops.workers.direct_api import DirectAPIAdapter
from saberops.workers.registry import AdapterRegistry


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"calls": [], "replies": [], "code": 200, "delay": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            state["calls"].append((self.path, self.headers.get("Authorization"), None))
            self.send_response(state["code"])
            if state["code"] == 302:
                self.send_header("Location", state["redirect"])
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "test-model"}]}).encode())

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append((self.path, self.headers.get("Authorization"), body))
            time.sleep(state["delay"])
            self.send_response(state["code"])
            if state["code"] == 302:
                self.send_header("Location", state["redirect"])
            self.end_headers()
            reply = (
                state["replies"].pop(0)
                if state["replies"]
                else {
                    "model": "test-model",
                    "choices": [{"finish_reason": "stop", "message": {"content": "Done"}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                }
            )
            try:
                self.wfile.write(json.dumps(reply).encode())
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    for name in (
        "SABEROPS_PROFILE",
        "ORCH_BINDING_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_READINESS_STORE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(tmp_path / "access.json"))
    monkeypatch.setenv("DIRECT_TEST_KEY", "test-key-never-log")
    connection = ProviderConnection(
        "local-api",
        "testapi",
        AccessProtocol.OPENAI_COMPATIBLE,
        "openai-compatible-api",
        AuthMode.API_KEY,
        endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        credential_ref="env:DIRECT_TEST_KEY",
    )
    AccessStore(tmp_path / "access.json").save(build_access_registry((connection,)))
    subprocess.run(["git", "init", "-q", str(tmp_path / "worktree")], check=True)
    state.update(connection=connection, root=tmp_path / "worktree", tmp=tmp_path)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def request(api: dict[str, Any], role: str = "worker", **kwargs: Any) -> WorkerRequest:
    return WorkerRequest(
        "Implement task",
        api["root"],
        "test-model",
        timeout_seconds=5,
        env={
            "ORCH_EXECUTION_PROFILE_ID": "discovered-profile:local-api",
            "ORCH_EXECUTION_BACKEND": "DIRECT_API",
            "ORCH_EXECUTION_BINDING_ID": f"discovered-binding:{role}:local-api:test-model",
        },
        **kwargs,
    )


def tool_reply(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": "test-model",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
            }
        ],
    }


def test_worker_http_tool_loop_usage_and_process_identity(api: dict[str, Any]) -> None:
    api["replies"] = [tool_reply("write_file", {"path": "result.txt", "content": "hello"})]
    identities: list[dict[str, Any]] = []
    result = DirectAPIAdapter("testapi").run(request(api, on_process_start=identities.append))
    assert result.status is AttemptStatus.SUCCESS
    assert (api["root"] / "result.txt").read_text() == "hello"
    assert result.has_changes and "result.txt" in result.changed_paths
    assert result.stdout == "Done" and result.actual_model == "test-model"
    assert result.usage[-1].total_tokens == 25 and identities
    assert len(api["calls"]) == 2
    assert api["calls"][0][:2] == ("/v1/chat/completions", "Bearer test-key-never-log")
    assert api["calls"][1][2]["messages"][-1]["content"] == "File written"
    assert "test-key-never-log" not in repr(result)


@pytest.mark.parametrize("role", ["reviewer", "orchestrator"])
def test_readonly_roles_cannot_write_even_if_request_flag_is_false(
    api: dict[str, Any],
    role: str,
) -> None:
    api["replies"] = [tool_reply("write_file", {"path": "denied.txt", "content": "no"})]
    result = DirectAPIAdapter("testapi").run(request(api, role))
    assert result.status is AttemptStatus.SUCCESS
    assert not (api["root"] / "denied.txt").exists()
    assert {t["function"]["name"] for t in api["calls"][0][2]["tools"]} == {
        "read_file",
        "list_files",
    }
    assert "denied" in api["calls"][1][2]["messages"][-1]["content"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("ORCH_EXECUTION_PROFILE_ID", "discovered-profile:other-api"),
        ("ORCH_EXECUTION_BACKEND", "DIRECT_CLI"),
        ("ORCH_EXECUTION_BINDING_ID", "discovered-binding:worker:local-api:other-model"),
    ],
)
def test_identity_mismatch_never_launches_or_calls(
    api: dict[str, Any],
    field: str,
    value: str,
) -> None:
    req = request(api)
    assert req.env is not None
    req.env[field] = value
    result = DirectAPIAdapter("testapi").run(req)
    assert result.status is AttemptStatus.FAILED and not api["calls"]


def test_missing_credential_fails_closed(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DIRECT_TEST_KEY")
    result = DirectAPIAdapter("testapi").run(request(api))
    assert result.status is AttemptStatus.FAILED and not api["calls"]
    assert verify_api(api["connection"]).reason == "CREDENTIAL_MISSING"


@pytest.mark.parametrize("code", [401, 403, 500, 302])
def test_failure_and_redirect_are_sanitized(api: dict[str, Any], code: int) -> None:
    api.update(code=code, redirect=api["connection"].endpoint + "/leak")
    result = DirectAPIAdapter("testapi").run(request(api))
    assert result.status is AttemptStatus.FAILED
    assert len(api["calls"]) == 1
    assert "test-key-never-log" not in repr(result)
    assert f"HTTP {code}" in (result.error or "")


def test_deadline_stops_real_api_child(api: dict[str, Any]) -> None:
    api["delay"] = 1.0
    req = request(api)
    req.timeout_seconds = 0.2
    started = time.monotonic()
    result = DirectAPIAdapter("testapi").run(req)
    assert result.status is AttemptStatus.TIMEOUT
    assert time.monotonic() - started < 3


def test_discovery_materializes_api_roles_and_routing_without_selecting(
    api: dict[str, Any],
) -> None:
    result = _discover_connection(api["connection"], AdapterRegistry.default())
    assert result.models and result.models[0].is_execution_supported
    owner = _load_owner_bindings_safely()
    assert owner.policy is None and not owner.reviewer_binding_id
    for role in ("worker", "reviewer", "orchestrator"):
        resolved = resolve_execution_binding(
            registry=owner.registry,
            binding_id=f"discovered-binding:{role}:local-api:test-model",
            adapter=AdapterRegistry.default().get("testapi"),
        )
        assert resolved.binding.backend is BindingKind.DIRECT_API
        assert isinstance(resolved.adapter, DirectAPIAdapter)
    from saberops.model_access import ReadinessService
    from saberops.models import ProviderReadiness
    from saberops.web import _access_store, _readiness_store

    registry = _access_store().load()
    service = ReadinessService(registry, _readiness_store())
    evidence = service.refresh(registry.get("local-api"))
    assert evidence.state is ProviderReadiness.READY
    assert evidence.reason == "API_ENDPOINT_READY"
    assert "test-key-never-log" not in json.dumps(evidence.to_dict())
    assert all(call[2] is None for call in api["calls"])


@pytest.mark.parametrize("path", ["../outside.txt", ".env", ".git/config"])
def test_file_tools_protect_worktree_and_credentials(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        execute_tool(tmp_path, "write_file", {"path": path, "content": "x"}, False)


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("https://example.com/v1/chat/completions", "https://example.com/v1/chat/completions"),
        ("https://example.com/v1/models", "https://example.com/v1/chat/completions"),
        (
            "https://openrouter.ai/api/alpha/decisions",
            "https://openrouter.ai/api/v1/chat/completions",
        ),
    ],
)
def test_completion_paths(endpoint: str, expected: str) -> None:
    assert completion_url(endpoint) == expected


def test_api_adapter_never_replaces_native_subscription_registry(api: dict[str, Any]) -> None:
    from saberops.workers.claude import ClaudeCodeAdapter

    connection = ProviderConnection(
        "claude-api",
        "anthropic",
        AccessProtocol.OPENAI_COMPATIBLE,
        "custom-api",
        AuthMode.API_KEY,
        endpoint=api["connection"].endpoint,
        credential_ref="env:DIRECT_TEST_KEY",
    )
    AccessStore(api["tmp"] / "access.json").save(build_access_registry((connection,)))
    registry = AdapterRegistry.default()
    assert isinstance(registry.get("anthropic"), ClaudeCodeAdapter)
    result = _discover_connection(connection, registry)
    assert result.models[0].is_execution_supported
    resolved = resolve_execution_binding(
        registry=_load_owner_bindings_safely().registry,
        binding_id="discovered-binding:worker:claude-api:test-model",
        adapter=registry.get("anthropic"),
    )
    assert isinstance(resolved.adapter, DirectAPIAdapter)
    assert isinstance(registry.get("anthropic"), ClaudeCodeAdapter)


def test_provider_truncation_does_not_become_success(api: dict[str, Any]) -> None:
    api["replies"] = [
        {"choices": [{"finish_reason": "length", "message": {"content": "incomplete"}}]}
    ]
    result = DirectAPIAdapter("testapi").run(request(api))
    assert result.status is AttemptStatus.FAILED


def test_model_cannot_read_credential_from_command_environment(api: dict[str, Any]) -> None:
    import sys

    api["replies"] = [
        tool_reply(
            "run_command",
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import os,json;print(json.dumps({k:v for k,v in os.environ.items() if k in "
                    '("DIRECT_TEST_KEY","SABEROPS_API_CREDENTIAL")}))',
                ]
            },
        )
    ]
    result = DirectAPIAdapter("testapi").run(request(api))
    assert result.status is AttemptStatus.SUCCESS
    tool_result = json.loads(api["calls"][1][2]["messages"][-1]["content"])
    assert tool_result["stdout"].strip() == "{}"


def test_orchestrator_assign_selects_exact_api_binding_without_inference(
    api: dict[str, Any],
) -> None:
    """Assigning the orchestrator slot keeps the exact API binding.

    L-A step 5 removed the chat model picker; the orchestrator-slot
    assignment it exercised now runs through the owner-bindings engine
    seam directly.
    """
    from saberops.control_plane.binding_store import save_owner_bindings
    from saberops.model_access import apply_discovery_result
    from saberops.web import _load_owner_bindings_safely, _model_catalog_store

    result = _discover_connection(api["connection"], AdapterRegistry.default())
    assert result.ok
    store = _model_catalog_store()
    store.save(apply_discovery_result(store.load(), result))
    identity = "discovered-binding:orchestrator:local-api:test-model"
    owner = _load_owner_bindings_safely()
    binding = owner.registry.try_get(identity)
    assert binding is not None and binding.binding_role.value == "ORCHESTRATOR"
    order = owner.role_binding_ids("orchestrator")
    save_owner_bindings(owner.with_role_order("orchestrator", (*order, identity)))
    assert identity in _load_owner_bindings_safely().role_binding_ids("orchestrator")
    assert all(call[2] is None for call in api["calls"])


def test_api_images_are_sent_as_visual_content(api: dict[str, Any]) -> None:
    import base64

    image = api['root'] / 'reference.png'
    image.write_bytes(b'owner-picture')
    result = DirectAPIAdapter('testapi').run(request(api, role='orchestrator',
                                                   read_only=True, input_images=(image,)))
    assert result.status is AttemptStatus.SUCCESS
    content = api['calls'][0][2]['messages'][1]['content']
    assert content[0] == {'type':'text', 'text':'Implement task'}
    assert content[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert base64.b64decode(content[1]['image_url']['url'].split(',')[1]) == image.read_bytes()
    assert not any(tool['function']['name'] == 'write_file' for tool in api['calls'][0][2]['tools'])


def _synthetic_catalog_model() -> Any:
    """One synthetic, non-secret catalog entry (no provider/account call)."""
    from saberops.model_access.connections import TriState
    from saberops.model_access.discovery import DiscoveredModel, ExecutionSupport

    return DiscoveredModel(
        connection_id="local-catalog",
        provider="testapi",
        backend="openai-compatible-api",
        model_id="test-model",
        display_name="Test model",
        discovery_status=TriState.TRUE,
        execution_support=ExecutionSupport.SUPPORTED,
        capabilities=(("tools", TriState.TRUE),),
        discovered_at="2026-01-02T03:04:05+00:00",
    )


def test_model_catalog_digest_and_serialization_contract(tmp_path: Path) -> None:
    """Catalog construction/serialization/digest identity is deterministic.

    Covers: D-0045 retained-behavior repair of ``ModelCatalog.digest`` and
    ``build_model_catalog`` (the deleted A35 test was their only cover).
    Synthetic entries only; never a real account or provider.
    """
    from dataclasses import replace

    from saberops.model_access.discovery import ModelCatalog, build_model_catalog

    model = _synthetic_catalog_model()
    plain = replace(model, model_id="test-model-2", discovered_at=None)
    first = build_model_catalog((model, plain))
    same = ModelCatalog(models=(model, plain))
    assert first.to_dict() == same.to_dict()
    assert model.digest == same.models[0].digest
    assert len(model.digest) == 64

    payload = first.to_dict()
    serialized = json.dumps(payload)
    assert "api_key" not in serialized.lower()
    assert "credential" not in serialized.lower()
    restored = ModelCatalog.from_mapping(payload)
    assert restored == first
    assert restored.models[0].digest == model.digest
    # An entry with no discovery timestamp stays absent, not an empty string.
    assert restored.models[1].discovered_at is None

    # Digest identity follows observable content: a changed flag changes it.
    assert replace(model, stale=True).digest != model.digest


def test_model_catalog_store_override_pins_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ORCH_ACCESS_MODELS_STORE`` pins the explicit store path.

    Covers: D-0045 retained-behavior repair of ``web._model_catalog_store``'s
    override path (the deleted A35 test was its only cover) and the concrete
    default location under the owner state dir.
    """
    from saberops.model_access.discovery import ModelCatalogStore, build_model_catalog
    from saberops.web import _model_catalog_store

    explicit = tmp_path / "pinned" / "models.json"
    monkeypatch.setenv("ORCH_ACCESS_MODELS_STORE", str(explicit))
    store = _model_catalog_store()
    assert isinstance(store, ModelCatalogStore)
    assert store.path == explicit

    catalog = build_model_catalog((_synthetic_catalog_model(),))
    store.save(catalog)
    assert store.path == explicit
    assert store.load() == catalog

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ORCH_ACCESS_MODELS_STORE")
    default = _model_catalog_store()
    assert default.path == tmp_path / "state" / "saberops" / ModelCatalogStore.FILENAME


def test_storage_audit_events_round_trip(tmp_path: Path) -> None:
    """GS-1 storage audit persistence is additive and idempotent.

    Covers: ADR-0003 D7 (bounded write transaction) and Amendment 1 §4
    (durable audit with task/run/artifact identity, bytes and reason).
    """
    from saberops.db import DB_SCHEMA_VERSION, Database

    db = Database(tmp_path / "orch.db")
    assert DB_SCHEMA_VERSION == 1  # additive tables never bump the schema version
    first = db.record_storage_event(
        at="2026-10-17T12:00:00+00:00",
        phase="intent",
        action="remove-worktree",
        artifact_class="attempt-worktree",
        artifact_ref="/tmp/wt/a1",
        artifact_identity="/tmp/wt/a1",
        reason="worktree-terminal-eligible",
        run_id="run1",
        project_id="proj-1",
        sha="a" * 40,
        policy_ref="owner-file:1:/tmp/gc.json",
        estimated_bytes=1024,
        detail={"branch": "orch/run1/a1"},
    )
    second = db.record_storage_event(
        at="2026-10-17T12:00:01+00:00",
        phase="result",
        action="remove-worktree",
        artifact_class="attempt-worktree",
        artifact_ref="/tmp/wt/a1",
        artifact_identity="/tmp/wt/a1",
        reason="worktree-removed",
        run_id="run1",
        estimated_bytes=1024,
        actual_bytes=900,
    )
    assert second == first + 1

    events = db.list_storage_events()
    assert [event["phase"] for event in events] == ["intent", "result"]
    assert events[0]["run_id"] == "run1" and events[0]["project_id"] == "proj-1"
    assert events[0]["sha"] == "a" * 40
    assert json.loads(events[0]["detail_json"]) == {"branch": "orch/run1/a1"}
    assert db.list_storage_events(run_id="other") == []
    assert len(db.list_storage_events(artifact_ref="/tmp/wt/a1")) == 2

    # The record is durable across reopen, and the DB still opens at version 1.
    reopened = Database(tmp_path / "orch.db")
    assert len(reopened.list_storage_events()) == 2
    assert reopened.get_storage_vacuum_at("owner") is None
    reopened.record_storage_vacuum("owner", __import__("datetime").datetime(
        2026, 10, 17, 12, 0, tzinfo=__import__("datetime").UTC
    ))
    assert reopened.get_storage_vacuum_at("owner") is not None
