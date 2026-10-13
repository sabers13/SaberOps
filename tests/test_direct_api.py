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


@pytest.mark.parametrize("target", ["T1", "T2", "T3", "reviewer", "orchestrator"])
def test_api_routing_assignment_uses_the_exact_role(api: dict[str, Any], target: str) -> None:
    from saberops.control_plane.binding_store import save_owner_bindings as _save_owner_bindings
    from saberops.control_plane.bindings import BindingRole
    from saberops.model_access import apply_discovery_result
    from saberops.routing_config import (
        DynamicCandidate,
        add_dynamic_candidate_to_chain,
        format_candidate_id,
        load_effective_routing_payload,
        save_user_routing,
    )
    from saberops.web import (
        _load_catalog_safely,
        _model_catalog_store,
        _resolve_exact_binding,
    )

    result = _discover_connection(api["connection"], AdapterRegistry.default())
    assert result.ok
    store = _model_catalog_store()
    store.save(apply_discovery_result(store.load(), result))
    entry = _load_catalog_safely().try_get(
        connection_id="local-api", provider="testapi",
        backend="openai-compatible-api", model_id="test-model",
    )
    assert entry is not None and entry.is_discovery_verified
    base_id = "discovered-binding:"
    owner = _load_owner_bindings_safely()
    registry = AdapterRegistry.default()
    if target.startswith("T"):
        exact = _resolve_exact_binding(
            owner, registry, provider="testapi", model="test-model",
            connection_id="local-api", role=BindingRole.WORKER,
        )
        assert exact is not None and exact.binding_id == base_id + "worker:local-api:test-model"
        candidate_id = format_candidate_id("testapi", "test-model")
        approval = DynamicCandidate(
            candidate_id=candidate_id, provider="testapi", model="test-model",
            connection_id="local-api", backend="openai-compatible-api",
            binding_id=exact.binding_id,
            discovery_status=entry.discovery_status.value,
            execution_support=entry.execution_support.value,
            training_required="UNKNOWN",
            capabilities=tuple((n, s.value) for n, s in entry.capabilities),
        )
        payload = load_effective_routing_payload()
        save_user_routing(add_dynamic_candidate_to_chain(
            payload, role="WORKER", candidate=approval,
        ))
        worker_ids = [
            cid
            for group in load_effective_routing_payload()["roles"]["WORKER"]
            for cid in group["routes"]
        ]
        assert "testapi/test-model" in worker_ids
    else:
        wanted = base_id + f"{target}:local-api:test-model"
        binding = owner.registry.try_get(wanted)
        assert binding is not None and binding.binding_role.value == target.upper()
        order = owner.role_binding_ids(target)
        assert wanted not in order
        _save_owner_bindings(owner.with_role_order(target, (*order, wanted)))
        owner = _load_owner_bindings_safely()
        if target == "reviewer":
            assert owner.reviewer_binding_id == wanted
        else:
            assert owner.policy is not None
            assert wanted in repr(owner.policy)
    assert all(call[2] is None for call in api["calls"])


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
