"""OmniRoute as a direct-API provider, against a local stub server.

The OmniRoute route runs entirely through
:mod:`saberops.model_access.connections` presets plus
:mod:`saberops.workers.direct_api`, without importing
:mod:`saberops.gateway`.  Its billing posture is API-key (METERED_KEY):
every test below configures an explicit ``env:`` credential reference,
a missing key is refused, and no CLI/subscription credential is ever
read or forwarded.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from saberops.model_access.connections import (
    OMNIROUTE_DEFAULT_ENDPOINT,
    AccessProtocol,
    AccessStore,
    AuthMode,
    ProviderConnection,
    build_access_registry,
    is_virtual_route,
    preset_connection,
)
from saberops.model_access.discovery import discover_connection_models
from saberops.models import AttemptStatus, DispatchOutcome, WorkerRequest
from saberops.workers.direct_api import (
    DirectAPIAdapter,
    credential_value,
    supports_connection,
)
from saberops.workers.registry import AdapterRegistry

MODELS = ["openai/gpt-5.5", "cc/claude-sonnet-5", "auto", "auto/coding"]

OMNI_KEY = "OMNIROUTE_TEST_KEY"
OMNI_SECRET = "omni-test-secret-never-log"


@pytest.fixture()
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
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
            state["calls"].append(
                ("POST", self.path, body.get("model"), self.headers.get("Authorization"))
            )
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
    monkeypatch.setenv(OMNI_KEY, OMNI_SECRET)
    # Decoy CLI/subscription credentials: the route must never read or
    # forward any of these; only the configured env: key is used.
    monkeypatch.setenv("CODEX_API_KEY", "decoy-codex-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "decoy-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "decoy-openai-key")
    subprocess.run(["git", "init", "-q", str(tmp_path / "worktree")], check=True)
    state.update(endpoint=f"http://127.0.0.1:{server.server_port}/v1", tmp=tmp_path)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def keyed_connection(endpoint: str) -> ProviderConnection:
    return preset_connection(
        "omniroute", credential_ref=f"env:{OMNI_KEY}", endpoint=endpoint,
    )


def test_preset_requires_an_explicit_api_key() -> None:
    with pytest.raises(ValueError, match="credential_ref is required"):
        preset_connection("omniroute")
    connection = preset_connection("omniroute", credential_ref=f"env:{OMNI_KEY}")
    assert connection.provider == "omniroute"
    assert connection.endpoint == OMNIROUTE_DEFAULT_ENDPOINT
    assert connection.auth_mode is AuthMode.API_KEY
    assert connection.credential_ref == f"env:{OMNI_KEY}"
    assert connection.is_configured
    assert supports_connection(connection)


def test_keyless_omniroute_connection_is_never_executable() -> None:
    legacy = ProviderConnection(
        "omniroute-legacy",
        "omniroute",
        AccessProtocol.OPENAI_COMPATIBLE,
        "openai-compatible-api",
        AuthMode.LOCAL_NO_AUTH,
        endpoint=OMNIROUTE_DEFAULT_ENDPOINT,
    )
    assert not supports_connection(legacy)


def test_virtual_routes_are_only_flagged_for_omniroute() -> None:
    assert is_virtual_route("omniroute", "auto")
    assert is_virtual_route("omniroute", "auto/coding")
    assert not is_virtual_route("omniroute", "openai/gpt-5.5")
    assert not is_virtual_route("openrouter", "auto")


def test_route_does_not_import_gateway() -> None:
    import re

    pattern = re.compile(r"^\s*(?:from|import)\s+saberops\.gateway\b", re.MULTILINE)
    for module in (
        "src/saberops/model_access/connections.py",
        "src/saberops/model_access/discovery.py",
        "src/saberops/model_access/api_verification.py",
        "src/saberops/workers/direct_api.py",
        "src/saberops/workers/api_agent.py",
    ):
        source = Path(module).read_text(encoding="utf-8")
        assert pattern.search(source) is None, module
    # A fresh interpreter exercising the route must never load the
    # gateway package, regardless of what this session imported.
    probe = (
        "import sys;"
        "from saberops.model_access.connections import preset_connection;"
        "from saberops.workers.direct_api import DirectAPIAdapter, supports_connection;"
        "from saberops.workers.registry import AdapterRegistry;"
        "from saberops.model_access.api_verification import verify_api;"
        "c = preset_connection('omniroute', credential_ref='env:X');"
        "assert supports_connection(c);"
        "hit = [m for m in sys.modules if m.partition('.')[0] == 'saberops'"
        " and 'gateway' in m];"
        "assert hit == [], hit"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_discovery_lists_exact_models_and_skips_auto(stub: dict[str, Any]) -> None:
    connection = keyed_connection(stub["endpoint"])

    def list_models() -> list[str]:
        request = urllib.request.Request(
            f"{stub['endpoint']}/models",
            headers={"Authorization": f"Bearer {credential_value(connection)}"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        return [str(row["id"]) for row in payload["data"]]

    result = discover_connection_models(connection, list_models=list_models)
    assert result.ok
    discovered = [m.model_id for m in result.models]
    assert discovered == MODELS
    assert not result.models[0].is_execution_supported  # evidence-only here
    executable = [m for m in discovered if not is_virtual_route("omniroute", m)]
    assert executable == ["openai/gpt-5.5", "cc/claude-sonnet-5"]
    assert all(call[2] == f"Bearer {OMNI_SECRET}" for call in stub["calls"])
    assert "decoy" not in json.dumps(stub["calls"])


def test_registry_resolves_omniroute_to_direct_api(stub: dict[str, Any]) -> None:
    AccessStore(stub["tmp"] / "access.json").save(
        build_access_registry((keyed_connection(stub["endpoint"]),))
    )
    adapter = AdapterRegistry.default().get("omniroute")
    assert isinstance(adapter, DirectAPIAdapter)


def _worker_request(stub: dict[str, Any], model: str) -> WorkerRequest:
    connection_id = "byok-omniroute"
    AccessStore(stub["tmp"] / "access.json").save(
        build_access_registry((
            preset_connection(
                "omniroute",
                credential_ref=f"env:{OMNI_KEY}",
                endpoint=stub["endpoint"],
                connection_id=connection_id,
            ),
        ))
    )
    return WorkerRequest(
        "Say done", stub["tmp"] / "worktree", model, timeout_seconds=10,
        env={
            "ORCH_EXECUTION_PROFILE_ID": f"discovered-profile:{connection_id}",
            "ORCH_EXECUTION_BACKEND": "DIRECT_API",
            "ORCH_EXECUTION_BINDING_ID": f"discovered-binding:worker:{connection_id}:{model}",
        },
    )


def test_omniroute_model_runs_as_direct_api_worker(stub: dict[str, Any]) -> None:
    result = DirectAPIAdapter("omniroute").run(_worker_request(stub, "openai/gpt-5.5"))
    assert result.status is AttemptStatus.SUCCESS
    assert result.actual_model == "openai/gpt-5.5"
    posts = [call for call in stub["calls"] if call[0] == "POST"]
    assert ("POST", "/v1/chat/completions", "openai/gpt-5.5", f"Bearer {OMNI_SECRET}") in posts
    assert "decoy" not in json.dumps(stub["calls"])
    assert OMNI_SECRET not in repr(result)


def test_missing_key_is_refused_without_network_access(
    stub: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(OMNI_KEY)
    request = _worker_request(stub, "openai/gpt-5.5")
    result = DirectAPIAdapter("omniroute").run(request)
    assert result.status is AttemptStatus.FAILED
    assert result.outcome is DispatchOutcome.ELIGIBILITY_SKIP
    assert stub["calls"] == []


def test_api_verification_is_ready_with_key(stub: dict[str, Any]) -> None:
    from saberops.model_access.api_verification import verify_api

    evidence = verify_api(keyed_connection(stub["endpoint"]))
    assert evidence.reason in ("READY", "API_ENDPOINT_READY")
    assert "decoy" not in json.dumps(evidence.to_dict())
