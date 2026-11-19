"""Billing-relevant CLI configuration pinning for Codex and OpenCode workers.

No provider, model, or network call is made: every adapter runs against a
stub executable that records its own argv and environment, and the
``opencode serve`` config builder is exercised as a pure function.  Shell
canaries prove that ambient ``OPENCODE_*`` configuration never reaches a
worker child and that the pinned values always win.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from saberops.git import GitManager
from saberops.models import AttemptStatus, DispatchOutcome, WorkerRequest
from saberops.session_runtime import (
    AgentSessionRuntime,
    SessionRuntimeError,
    opencode_serve_config,
)
from saberops.workers.codex import CodexAdapter
from saberops.workers.env import hermetic_env
from saberops.workers.opencode import OpenCodeAdapter, opencode_provider_id

MODEL = "opencode-go/mimo-v2.5"


def _write_recording_stub(bindir: Path, name: str, outfile: Path) -> None:
    """Write a stub CLI that records argv+env as JSON and exits 0 silently."""
    script = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(outfile)!r}, 'w', encoding='utf-8') as fh:\n"
        "    json.dump({'argv': sys.argv, 'cwd': os.getcwd(), 'env': dict(os.environ)},"
        " fh, sort_keys=True)\n"
    )
    path = bindir / name
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture()
def stub_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    outfile = tmp_path / "child.json"
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(GitManager, "get_changed_paths", lambda _path: [])
    return bindir, outfile


def _child(outfile: Path) -> dict[str, Any]:
    assert outfile.is_file(), "stub child was never launched"
    loaded = json.loads(outfile.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


# ---------------------------------------------------------------------------
# opencode run
# ---------------------------------------------------------------------------


def test_opencode_run_pins_enabled_providers_and_drops_shell_config(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    """Covers: HV-2 isolation contract (pinned inline config + child cwd)."""
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-opencode", outfile)
    adapter = OpenCodeAdapter(opencode_bin="stub-opencode")
    result = adapter.run(
        WorkerRequest(
            "do the thing",
            tmp_path,
            MODEL,
            env={
                # Ambient-derived overlay canaries: must never reach the child.
                "OPENCODE_CONFIG_CONTENT": '{"enabled_providers": ["evil"]} ',
                "OPENCODE_CONFIG": "/tmp/evil.json",
                "OPENCODE_CONFIG_DIR": "/tmp/evil-dir",
                "OPENCODE_MODEL": "evil/model",
                "OPENCODE_SERVER_USERNAME": "shell-user",
            },
        )
    )
    assert result.status == AttemptStatus.SUCCESS
    child = _child(outfile)
    pinned = json.loads(child["env"]["OPENCODE_CONFIG_CONTENT"])
    assert pinned["enabled_providers"] == ["opencode-go"]
    assert {"action": "integration.use", "resource": "plugin:*",
            "effect": "deny"} in pinned["policies"]
    for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_MODEL"):
        assert key not in child["env"], f"shell {key} reached the opencode child"
    # The server username/password names pass the projection by design
    # (``opencode serve`` needs them); they carry no provider/billing
    # selection, and billing itself is pinned above.
    assert child["env"]["OPENCODE_SERVER_USERNAME"] == "shell-user"
    argv = child["argv"]
    # opencode v2.0.29 documents neither ``--pure`` nor ``--dir`` (HV-1 F1,
    # fixed in Step 024/HV-2).  The worktree now travels as the child ``cwd``
    # and discovery isolation is the owned private config/home root asserted
    # below -- never an argv flag.
    assert argv[1] == "run"
    assert "--pure" not in argv and "--dir" not in argv
    assert argv[argv.index("--model") + 1] == MODEL
    assert Path(child["cwd"]).resolve() == tmp_path.resolve()
    # The child environment is the isolation-owned root, not the ambient
    # shell: project/ancestor discovery is disabled and ambient config
    # selectors (OPENCODE_CONFIG/OPENCODE_CONFIG_DIR/OPENCODE_MODEL and a
    # hostile project-disable value) never survive.
    assert child["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert "OPENCODE_CONFIG" not in child["env"]
    assert "OPENCODE_CONFIG_DIR" not in child["env"]
    assert "OPENCODE_MODEL" not in child["env"]
    config_home = Path(child["env"]["XDG_CONFIG_HOME"])
    home = Path(child["env"]["HOME"])
    assert config_home != home
    for directory in (config_home, home):
        assert "saberops-opencode-isolation-" in str(directory)
    # The owned root lives only for the dispatch: it is removed after the
    # child exits (creation-time emptiness is covered by the helper tests).
    assert not config_home.parent.exists()
    inline = json.loads(child["env"]["OPENCODE_CONFIG_CONTENT"])
    assert inline["enabled_providers"] == ["opencode-go"]
    assert {"action": "integration.use", "resource": "plugin:*",
            "effect": "deny"} in inline["policies"]
    assert {"action": "integration.use", "resource": "mcp:*",
            "effect": "deny"} in inline["policies"]


def test_opencode_run_refuses_model_without_provider_prefix(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-opencode", outfile)
    adapter = OpenCodeAdapter(opencode_bin="stub-opencode")
    result = adapter.run(WorkerRequest("do the thing", tmp_path, "bare-model"))
    assert result.status == AttemptStatus.FAILED
    assert result.outcome == DispatchOutcome.ELIGIBILITY_SKIP
    assert "provider/model" in (result.error or "")
    assert not outfile.is_file(), "refused dispatch must not launch any process"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("opencode-go/mimo-v2.5", "opencode-go"),
        ("anthropic/claude-sonnet-4-5", "anthropic"),
        ("bare-model", None),
        ("", None),
        ("/model-only", None),
        ("provider/", None),
    ],
)
def test_opencode_provider_id_parsing(model: str, expected: str | None) -> None:
    assert opencode_provider_id(model) == expected


# ---------------------------------------------------------------------------
# opencode serve (session_runtime)
# ---------------------------------------------------------------------------


def test_opencode_serve_config_starts_empty_and_pins_provider() -> None:
    permissions = {"*": "allow", "external_directory": "deny", "doom_loop": "deny"}
    config = opencode_serve_config(MODEL, permissions)
    # Frozen permission/agent keys are preserved ...
    assert config["permission"] == permissions
    assert config["model"] == MODEL
    assert config["default_agent"] == "saberops"
    assert config["agent"]["saberops"]["model"] == MODEL
    assert config["agent"]["saberops"]["permission"] == permissions
    # ... and billing is pinned to exactly the selected provider.  The
    # builder takes no shell input, so ambient OPENCODE_CONFIG_CONTENT
    # cannot enter by construction.
    assert config["enabled_providers"] == ["opencode-go"]


def test_opencode_serve_config_refuses_model_without_prefix() -> None:
    with pytest.raises(SessionRuntimeError):
        opencode_serve_config("bare-model", {"*": "allow"})


def test_opencode_serve_entry_refuses_model_without_prefix(tmp_path: Path) -> None:
    # The refusal path returns before touching any runtime state, so an
    # uninitialised instance is sufficient and no process can launch.
    runtime = AgentSessionRuntime.__new__(AgentSessionRuntime)
    result = runtime.run_opencode(WorkerRequest("task", tmp_path, "bare-model"), "opencode")
    assert result.status == AttemptStatus.FAILED
    assert result.outcome == DispatchOutcome.ELIGIBILITY_SKIP
    assert "provider/model" in (result.error or "")


# ---------------------------------------------------------------------------
# codex direct path (the gateway cut removed the gateway path entirely:
# every invocation carries the billing pins)
# ---------------------------------------------------------------------------


def _codex_argv(child: dict[str, Any]) -> list[str]:
    argv = child["argv"]
    assert isinstance(argv, list)
    return [str(a) for a in argv]


def test_codex_direct_path_carries_billing_pins(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-codex", outfile)
    adapter = CodexAdapter(codex_bin="stub-codex")
    adapter.run(WorkerRequest("do the thing", tmp_path, "gpt-6.1-sol"))
    argv = _codex_argv(_child(outfile))
    for key, value in (
        ("forced_login_method", '"chatgpt"'),
        ("model_provider", '"openai"'),
        ("service_tier", '"default"'),
    ):
        flag = f"{key}={value}"
        assert flag in argv, f"direct path is missing -c {flag}: {argv}"


def test_codex_direct_path_ignores_user_config(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    # ``--ignore-user-config`` drops $CODEX_HOME/config.toml (and with it
    # the [projects.*] trust table gating project .codex/ layers) while
    # ChatGPT auth still loads from CODEX_HOME (installed codex 0.162.0:
    # "Do not load `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`").
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-codex", outfile)
    adapter = CodexAdapter(codex_bin="stub-codex")
    adapter.run(WorkerRequest("do the thing", tmp_path, "gpt-6.1-sol"))
    argv = _codex_argv(_child(outfile))
    assert "--ignore-user-config" in argv
    assert argv.index("--ignore-user-config") < argv.index("--model")


def test_codex_direct_path_disables_hooks(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    # User-level Codex hooks (``features.hooks = true`` plus the
    # ``[hooks.state]`` trust table in ``$CODEX_HOME/config.toml``) must be
    # disabled explicitly on every invocation, not only by the coincidence
    # that ``--ignore-user-config`` drops the config file holding the flag.
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-codex", outfile)
    adapter = CodexAdapter(codex_bin="stub-codex")
    adapter.run(WorkerRequest("do the thing", tmp_path, "gpt-6.1-sol"))
    argv = _codex_argv(_child(outfile))
    assert "features.hooks=false" in argv


def test_codex_resume_path_disables_hooks(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-codex", outfile)
    adapter = CodexAdapter(codex_bin="stub-codex")
    adapter.run(
        WorkerRequest(
            "follow up", tmp_path, "gpt-6.1-sol", native_resume_id="session-123"
        )
    )
    argv = _codex_argv(_child(outfile))
    assert "resume" in argv
    assert "features.hooks=false" in argv


def test_codex_resume_path_ignores_user_config(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-codex", outfile)
    adapter = CodexAdapter(codex_bin="stub-codex")
    adapter.run(
        WorkerRequest(
            "follow up", tmp_path, "gpt-6.1-sol", native_resume_id="session-123"
        )
    )
    argv = _codex_argv(_child(outfile))
    assert "resume" in argv
    assert "--ignore-user-config" in argv
    for key, value in (
        ("forced_login_method", '"chatgpt"'),
        ("model_provider", '"openai"'),
        ("service_tier", '"default"'),
    ):
        assert f"{key}={value}" in argv


def test_codex_pins_apply_even_with_stale_gateway_env(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    # Stale ORCH_GATEWAY_* values in the request overlay must neither
    # reach the child nor divert the invocation: the gateway cut removed
    # the gateway provider selection, so the billing pins still apply.
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-codex", outfile)
    adapter = CodexAdapter(codex_bin="stub-codex")
    adapter.run(
        WorkerRequest(
            "do the thing",
            tmp_path,
            "gpt-6.1-sol",
            env={
                "ORCH_GATEWAY_ENDPOINT": "http://gateway",
                "ORCH_GATEWAY_CONNECTION": "acct-1",
                "ORCH_GATEWAY_AUTHORIZATION_ENV": "OMNIROUTE_API_KEY",
                "ORCH_GATEWAY_PROVIDER_ID": "orch_omniroute",
            },
        )
    )
    argv = _codex_argv(_child(outfile))
    for key, value in (
        ("forced_login_method", '"chatgpt"'),
        ("model_provider", '"openai"'),
        ("service_tier", '"default"'),
    ):
        flag = f"{key}={value}"
        assert flag in argv, f"direct path is missing -c {flag}: {argv}"
    assert 'model_provider = "orch_omniroute"' not in argv
    child_env = _child(outfile)["env"]
    assert isinstance(child_env, dict)
    for key in (
        "ORCH_GATEWAY_ENDPOINT",
        "ORCH_GATEWAY_CONNECTION",
        "ORCH_GATEWAY_AUTHORIZATION_ENV",
        "ORCH_GATEWAY_PROVIDER_ID",
    ):
        assert key not in child_env, f"stale {key} reached the codex child"


# ---------------------------------------------------------------------------
# model verification + interactive native terminals
# ---------------------------------------------------------------------------


def test_model_check_pins_checked_provider() -> None:
    from saberops.model_access.model_verification import verify_native_model
    from saberops.process import ProcessResult

    seen: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        seen["config"] = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        assert kwargs["inherit_env"] is False
        challenge = "SABEROPS_ACCESS_never"
        output = json.dumps({"type": "text", "part": {"text": challenge}})
        output += "\n" + json.dumps({"type": "step_finish", "part": {"reason": "stop"}})
        return ProcessResult(argv, 0, output, "", 0.01)

    verify_native_model("/fake/cli", "opencode", "opencode-go/some-model", runner=runner)
    assert seen["config"]["enabled_providers"] == ["opencode-go"]
    assert seen["config"]["permission"] == "deny"


def test_model_check_without_prefix_keeps_previous_behaviour() -> None:
    from saberops.model_access.model_verification import verify_native_model
    from saberops.process import ProcessResult

    seen: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        seen["config"] = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        assert kwargs["inherit_env"] is False
        return ProcessResult(argv, 1, "", "nope", 0.01)

    assert (
        verify_native_model("/fake/cli", "opencode", "chosen-model", runner=runner)
        == "MODEL_CHECK_FAILED"
    )
    assert "enabled_providers" not in seen["config"]


# ---------------------------------------------------------------------------
# env.py overlay projection
# ---------------------------------------------------------------------------


def test_overlay_forwards_only_saberops_opencode_names() -> None:
    overlay = {
        "OPENCODE_CONFIG": "/tmp/evil.json",
        "OPENCODE_CONFIG_CONTENT": "shell-canary",
        "OPENCODE_CONFIG_DIR": "/tmp/evil-dir",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_MODEL": "evil/model",
        "OPENCODE_SERVER_PASSWORD": "server-secret",
        "OPENCODE_SERVER_USERNAME": "opencode",
    }
    env = hermetic_env("opencode", overlay, {})
    for key in (
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "OPENCODE_MODEL",
    ):
        assert key not in env, f"ambient {key} reaches the worker child"
    # The three SaberOps-owned names still pass the projection; the
    # adapters overwrite OPENCODE_CONFIG_CONTENT with the pinned value.
    assert env["OPENCODE_SERVER_PASSWORD"] == "server-secret"
    assert env["OPENCODE_SERVER_USERNAME"] == "opencode"
    assert env["OPENCODE_CONFIG_CONTENT"] == "shell-canary"


# ---------------------------------------------------------------------------
# opencode run lifecycle: private server + session resume
# ---------------------------------------------------------------------------


def test_opencode_run_uses_standalone_server(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    # A worker turn must run on a private server, never the owner's
    # user-level OpenCode background service (the model-listing path already
    # passes ``--standalone`` for the same reason).
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-opencode", outfile)
    adapter = OpenCodeAdapter(opencode_bin="stub-opencode")
    adapter.run(WorkerRequest("do the thing", tmp_path, MODEL))
    argv = _child(outfile)["argv"]
    assert "--standalone" in argv


def test_opencode_resume_passes_standalone_and_session(
    stub_bin: tuple[Path, Path], tmp_path: Path
) -> None:
    # Resume keeps working with ``--standalone``: the session id still
    # travels as ``--session`` and the session database lives in the real
    # XDG_DATA_HOME that isolation passes through.
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-opencode", outfile)
    adapter = OpenCodeAdapter(opencode_bin="stub-opencode")
    adapter.run(
        WorkerRequest(
            "follow up", tmp_path, MODEL, native_resume_id="ses_existing_123"
        )
    )
    argv = _child(outfile)["argv"]
    assert "--standalone" in argv
    assert argv[argv.index("--session") + 1] == "ses_existing_123"


# ---------------------------------------------------------------------------
# OpenCode stored-credential billing guard
# ---------------------------------------------------------------------------


def _write_opencode_auth(data_home: Path, entries: dict[str, str]) -> None:
    """Write a structural OpenCode auth file (type only, fake secrets)."""
    auth_dir = data_home / "opencode"
    auth_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        provider: {"type": kind, "key": f"fake-secret-{provider}"}
        for provider, kind in entries.items()
    }
    (auth_dir / "auth.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("model", "auth", "allowed", "launches"),
    [
        # OpenCode has no Anthropic subscription sign-in: the stored API key
        # must never bill, even though a fake secret is present.
        ("anthropic/claude-sonnet-4-5", {"anthropic": "api"}, frozenset(), False),
        ("anthropic/claude-sonnet-4-5", {}, frozenset(), False),
        # The only escape is an explicit owner API-billing entry.
        ("anthropic/claude-sonnet-4-5", {"anthropic": "api"}, frozenset({"anthropic"}), True),
        # openai is admitted only as the ChatGPT-plan OAuth sign-in.
        ("openai/gpt-5.5", {"openai": "oauth"}, frozenset(), True),
        ("openai/gpt-5.5", {"openai": "api"}, frozenset(), False),
        ("openai/gpt-5.5", {}, frozenset(), False),
        # A plan-backed provider keeps its subscription credential.
        ("opencode-go/deepseek-v4-flash", {"opencode-go": "api"}, frozenset(), True),
        ("opencode/big-pickle", {"opencode": "api"}, frozenset(), True),
        # A provider with no packaged plan account is refused on an API key.
        ("zhipuai/glm-5", {"zhipuai": "api"}, frozenset(), False),
        ("zhipuai/glm-5", {"zhipuai": "api"}, frozenset({"zhipuai"}), True),
    ],
)
def test_opencode_stored_credential_billing_guard(
    stub_bin: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    auth: dict[str, str],
    allowed: frozenset[str],
    launches: bool,
) -> None:
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-opencode", outfile)
    data_home = tmp_path / "data"
    _write_opencode_auth(data_home, auth)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    adapter = OpenCodeAdapter(
        opencode_bin="stub-opencode", api_billing_providers=allowed
    )
    result = adapter.run(WorkerRequest("do the thing", tmp_path, model))
    if launches:
        assert result.status == AttemptStatus.SUCCESS, result.error
        assert outfile.is_file()
    else:
        assert result.status == AttemptStatus.FAILED
        assert result.outcome == DispatchOutcome.ELIGIBILITY_SKIP
        assert "OPENCODE_API_KEY_REFUSED" in (result.error or "")
        assert not outfile.is_file(), "refused route must not launch a process"


def test_opencode_stored_credential_refusal_never_leaks_secret(
    stub_bin: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir, outfile = stub_bin
    _write_recording_stub(bindir, "stub-opencode", outfile)
    data_home = tmp_path / "data"
    _write_opencode_auth(data_home, {"anthropic": "api"})
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    adapter = OpenCodeAdapter(opencode_bin="stub-opencode", api_billing_providers=frozenset())
    result = adapter.run(WorkerRequest("do the thing", tmp_path, "anthropic/claude-sonnet-4-5"))
    assert result.status == AttemptStatus.FAILED
    for field in (result.summary, result.error, result.stdout, result.stderr):
        assert "fake-secret" not in (field or "")


def test_owner_policy_api_billing_allowlist_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.control_plane.policy import load_control_plane_policy

    config_dir = tmp_path / "config" / "saberops"
    config_dir.mkdir(parents=True)
    (config_dir / "control_plane.json").write_text(
        json.dumps(
            {
                "version": 1,
                "providers": {},
                "api_billing": {"providers": ["Anthropic"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    policy = load_control_plane_policy()
    assert policy.api_billing_providers == frozenset({"anthropic"})

    # The allowlist travels through a frozen run snapshot, so in-run
    # dispatch sees the same owner decision as the scheduler seam.
    from saberops.control_plane.policy import (
        freeze_control_plane_policy,
        reconstruct_frozen_control_plane,
    )

    frozen = freeze_control_plane_policy(policy)
    rebuilt = reconstruct_frozen_control_plane(frozen.as_json(), frozen.digest)
    assert rebuilt.policy.api_billing_providers == frozenset({"anthropic"})

    (config_dir / "control_plane.json").write_text(
        json.dumps({"version": 1, "providers": {}, "api_billing": {"providers": "anthropic"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="api_billing"):
        load_control_plane_policy()
