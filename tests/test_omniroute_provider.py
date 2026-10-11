"""OmniRoute as an OpenAI-compatible provider, against a local stub gateway."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from saberops.model_access.connections import (
    OMNIROUTE_DEFAULT_ENDPOINT,
    AccessStore,
    AuthMode,
    build_access_registry,
    is_virtual_route,
    preset_connection,
)
from saberops.models import AttemptStatus, WorkerRequest
from saberops.web import _discover_connection
from saberops.workers.direct_api import DirectAPIAdapter, supports_connection
from saberops.workers.registry import AdapterRegistry

MODELS = ["openai/gpt-5.5", "cc/claude-sonnet-5", "auto", "auto/coding"]


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"calls": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            state["calls"].append(("GET", self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": m} for m in MODELS]}).encode())

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append(("POST", self.path, body.get("model")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({
                "model": body.get("model"),
                "choices": [{"finish_reason": "stop", "message": {"content": "Done"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    for name in ("SABEROPS_PROFILE", "ORCH_BINDING_STORE", "ORCH_ACCESS_MODELS_STORE",
                 "ORCH_READINESS_STORE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(tmp_path / "access.json"))
    subprocess.run(["git", "init", "-q", str(tmp_path / "worktree")], check=True)
    state.update(endpoint=f"http://127.0.0.1:{server.server_port}/v1", tmp=tmp_path)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_preset_defaults_to_local_gateway_without_a_key() -> None:
    connection = preset_connection("omniroute")
    assert connection.provider == "omniroute"
    assert connection.endpoint == OMNIROUTE_DEFAULT_ENDPOINT
    assert connection.auth_mode is AuthMode.LOCAL_NO_AUTH
    assert supports_connection(connection)


def test_preset_with_env_key_uses_api_key_auth() -> None:
    connection = preset_connection("omniroute", credential_ref="env:OMNIROUTE_API_KEY")
    assert connection.auth_mode is AuthMode.API_KEY
    assert connection.credential_ref == "env:OMNIROUTE_API_KEY"


def test_virtual_routes_are_only_flagged_for_omniroute() -> None:
    assert is_virtual_route("omniroute", "auto")
    assert is_virtual_route("omniroute", "auto/coding")
    assert not is_virtual_route("omniroute", "openai/gpt-5.5")
    assert not is_virtual_route("openrouter", "auto")


def test_add_omniroute_discovers_exact_models_and_skips_auto(gateway: dict[str, Any]) -> None:
    from saberops.model_access import (
        apply_discovery_result,
        build_access_registry,
    )
    from saberops.web import _model_catalog_store

    connection = preset_connection(
        "omniroute", connection_id="omniroute",
        endpoint=gateway["endpoint"], credential_ref="",
    )
    store = AccessStore(gateway["tmp"] / "access.json")
    store.save(build_access_registry((connection,)))
    saved = AccessStore(gateway["tmp"] / "access.json").load().get("omniroute")
    assert saved.provider == "omniroute" and saved.auth_mode is AuthMode.LOCAL_NO_AUTH
    result = _discover_connection(saved, AdapterRegistry.default())
    supported = {m.model_id for m in result.models if m.is_execution_supported}
    assert supported == {"openai/gpt-5.5", "cc/claude-sonnet-5"}
    assert len(result.models) == 4
    catalog_store = _model_catalog_store()
    catalog_store.save(apply_discovery_result(catalog_store.load(), result))
    assert all(call[2] is None for call in gateway["calls"] if call[0] == "GET")


def test_omniroute_model_runs_as_worker(gateway: dict[str, Any]) -> None:
    connection = preset_connection(
        "omniroute", connection_id="omniroute", endpoint=gateway["endpoint"],
    )
    AccessStore(gateway["tmp"] / "access.json").save(build_access_registry((connection,)))
    request = WorkerRequest(
        "Say done", gateway["tmp"] / "worktree", "openai/gpt-5.5", timeout_seconds=5,
        env={
            "ORCH_EXECUTION_PROFILE_ID": "discovered-profile:omniroute",
            "ORCH_EXECUTION_BACKEND": "DIRECT_API",
            "ORCH_EXECUTION_BINDING_ID": "discovered-binding:worker:omniroute:openai/gpt-5.5",
        },
    )
    result = DirectAPIAdapter("omniroute").run(request)
    assert result.status is AttemptStatus.SUCCESS
    assert result.actual_model == "openai/gpt-5.5"
    assert ("POST", "/v1/chat/completions", "openai/gpt-5.5") in gateway["calls"]
