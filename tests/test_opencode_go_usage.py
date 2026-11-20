"""Go quotas use native OpenCode auth, without inference or leaking keys."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from saberops.cli_usage import read_limits
from saberops.opencode_go_usage import GoUsageUnavailable, parse_go_limits, read_go_limits

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "src/saberops/defaults/opencode_go_usage"


def _fake_opencode(
    tmp_path: Path,
    *,
    version: str = "opencode v2.3.2",
    usage: object | None = None,
    api_exit: int = 0,
    api_sleeps: bool = False,
    hold_until: Path | None = None,
) -> Path:
    """Write an executable that mimics the OpenCode 2 usage surface.

    ``--version`` reports the CLI version, ``serve`` listens on the requested
    port, and the read-only ``api ... /api/plugin`` call writes the configured
    plugin output, exactly as OpenCode loads the shipped plugin.
    """
    exe = tmp_path / "opencode"
    trace = tmp_path / "trace.jsonl"
    payload = json.dumps(usage if usage is not None else {"usage": {}})
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, socket, sys, time\n"
        f"trace = {str(trace)!r}\n"
        f"payload = {payload!r}\n"
        "args = sys.argv[1:]\n"
        "selected = {k: os.environ.get(k) for k in (\n"
        "    'OPENCODE_CONFIG_CONTENT', 'OPENCODE_PASSWORD',\n"
        "    'SABEROPS_GO_USAGE_OUTPUT', 'XDG_DATA_HOME', 'HOME')}\n"
        "with open(trace, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'argv': args, 'env': selected}) + '\\n')\n"
        "if args == ['--version']:\n"
        f"    print({version!r})\n"
        "elif args and args[0] == 'serve':\n"
        "    port = int(args[args.index('--port') + 1])\n"
        "    srv = socket.socket()\n"
        "    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "    srv.bind(('127.0.0.1', port)); srv.listen(8)\n"
        "    print('server listening on http://127.0.0.1:%d' % port, flush=True)\n"
        + (f"    hold = {str(hold_until)!r}\n"
           "    while not os.path.exists(hold):\n"
           "        time.sleep(0.05)\n"
           if hold_until is not None else "")
        + ("    while True: time.sleep(0.1)\n")
        + "elif args and args[0] == 'api':\n"
        + ("    time.sleep(60)\n" if api_sleeps else "")
        + "    out = os.environ.get('SABEROPS_GO_USAGE_OUTPUT')\n"
        "    if out and " + str(api_exit == 0) + ":\n"
        "        with open(out, 'w', encoding='utf-8') as fh:\n"
        "            fh.write(payload)\n"
        "    print('{\"data\": []}')\n"
        f"    sys.exit({api_exit})\n"
    )
    exe.chmod(0o755)
    return exe


def _trace(tmp_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _plugin_env(entry: dict[str, object]) -> dict[str, str]:
    return entry["env"]  # type: ignore[return-value]


def test_go_quota_windows_precision_unknowns_and_overage() -> None:
    rows = parse_go_limits(
        {
            "usage": {
                "rolling": {"percent": 12.25, "resetsAt": "2030-01-01T01:00:00Z"},
                "weekly": {"percent": 101.5, "resetsAt": "do not expose this field"},
                "monthly": {"percent": None},
                "token": "not-returned",
            }
        }
    )
    assert len(rows) == 2
    assert rows[0]["remaining"] == 87.75
    assert rows[0]["reset_at"] == "2030-01-01T01:00:00Z"
    assert rows[1]["remaining"] == 0 and rows[1]["used"] == 101.5
    assert rows[1]["reset_at"] is None
    assert "not-returned" not in json.dumps(rows)
    assert parse_go_limits({"usage": {"weekly": {"percent": False}}}) == []
    assert parse_go_limits({}) == []


def test_go_reader_uses_opencode_two_local_api_without_prompts(tmp_path: Path) -> None:
    """Covers: the A7 regression (v2 no longer loads auth hooks for `models`)."""
    exe = _fake_opencode(
        tmp_path,
        usage={"usage": {"monthly": {"percent": 16, "resetsAt": "2030-01-01T00:00:00Z"}}},
    )
    rows = read_limits(str(exe), "opencode")
    assert rows[0]["remaining"] == 84
    assert "hidden" not in json.dumps(rows)

    trace = _trace(tmp_path)
    assert [entry["argv"] for entry in trace][:1] == [["--version"]]
    serve = trace[1]["argv"]
    assert isinstance(serve, list) and serve[0] == "serve"
    assert serve[serve.index("--hostname") + 1] == "127.0.0.1"
    port = serve[serve.index("--port") + 1]
    assert trace[2]["argv"] == [
        "api",
        "--server",
        f"http://127.0.0.1:{port}",
        "GET",
        "/api/plugin",
    ]
    for entry in trace:
        argv = entry["argv"]
        assert isinstance(argv, list)
        assert "run" not in argv and "prompt" not in argv and "models" not in argv

    env = _plugin_env(trace[1])
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["enabled_providers"] == ["opencode-go"]
    assert config["plugins"] == [str(_PLUGIN_DIR)]
    policies = {(p["resource"], p["effect"]) for p in config["policies"]}
    assert ("plugin:*", "deny") in policies
    assert ("mcp:*", "deny") in policies
    exact = next(p for p in config["policies"] if p["resource"] == f"plugin:{_PLUGIN_DIR}")
    assert exact["effect"] == "allow"
    assert env["SABEROPS_GO_USAGE_OUTPUT"]
    assert env["OPENCODE_PASSWORD"]
    assert env["XDG_DATA_HOME"]
    assert _plugin_env(trace[2])["OPENCODE_PASSWORD"] == env["OPENCODE_PASSWORD"]
    root = Path(env["HOME"]).parent
    assert "saberops-opencode-isolation-" in str(root)
    assert not root.exists(), "owned root is removed after the reader exits"


def test_go_reader_refuses_pre_v2_cli_before_launching_anything(tmp_path: Path) -> None:
    exe = _fake_opencode(tmp_path, version="opencode v1.15.5")
    with pytest.raises(GoUsageUnavailable, match="OpenCode 2"):
        read_go_limits(str(exe))
    assert [entry["argv"] for entry in _trace(tmp_path)] == [["--version"]]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("HTTP_401_EXPIRED", "Create a new key in OpenCode Console"),
        ("HTTP_403", "no OpenCode Go subscription"),
        ("GO_NOT_CONNECTED", "Connect OpenCode Go"),
        ("HTTP_429", "rate-limiting"),
    ],
)
def test_go_reader_explains_known_failures_in_plain_words(
    tmp_path: Path, error: str, expected: str
) -> None:
    exe = _fake_opencode(tmp_path, usage={"error": error})
    with pytest.raises(GoUsageUnavailable, match=expected):
        read_go_limits(str(exe))


def test_go_reader_reports_a_plugin_that_never_loads(tmp_path: Path) -> None:
    exe = _fake_opencode(tmp_path, api_exit=3)
    with pytest.raises(GoUsageUnavailable, match="could not load its usage plugin"):
        read_go_limits(str(exe))


def test_go_usage_launch_uses_owned_isolation_root_with_exact_plugin_exception(
    tmp_path: Path,
) -> None:
    """Covers: HV-2 go-usage isolation (deny-all plus exact owned plugin)."""
    exe = _fake_opencode(tmp_path, usage={"usage": {"monthly": {"percent": 16}}})
    rows = read_go_limits(str(exe))
    assert rows[0]["remaining"] == 84
    env = _plugin_env(_trace(tmp_path)[1])
    root = Path(env["HOME"]).parent
    assert "saberops-opencode-isolation-" in str(root)
    assert not root.exists()


def test_go_usage_root_survives_whole_child_lifetime(tmp_path: Path) -> None:
    """Covers: HV-2 go-usage lifetime (root alive through wait, removed after reap)."""
    import threading
    import time

    release = tmp_path / "release"
    exe = _fake_opencode(
        tmp_path, usage={"usage": {"monthly": {"percent": 16}}}, hold_until=release
    )
    outcome: dict[str, object] = {}

    def invoke() -> None:
        try:
            outcome["rows"] = read_go_limits(str(exe))
        except Exception as exc:  # never silently swallow a reader failure
            outcome["error"] = exc

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 30
        trace_path = tmp_path / "trace.jsonl"
        while not trace_path.is_file() or "serve" not in trace_path.read_text():
            assert time.monotonic() < deadline, "held child never started"
            time.sleep(0.05)
        env = _plugin_env(_trace(tmp_path)[1])
        root = Path(env["HOME"]).parent
        assert "saberops-opencode-isolation-" in str(root)
        for member in ("config", "home", "cache", "state"):
            assert (root / member).is_dir()
        assert root.is_dir()
    finally:
        release.write_text("go\n")
    worker.join(timeout=30)
    assert not worker.is_alive(), "reader thread never returned"
    assert "error" not in outcome, f"reader failed: {outcome.get('error')!r}"
    assert outcome["rows"][0]["remaining"] == 84  # type: ignore[index]
    assert not root.exists()


def test_go_usage_serve_failure_cleans_owned_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: HV-2 go-usage cleanup (a serve that dies still removes the root)."""
    import saberops.workers.opencode_isolation as isolation_module

    roots: list[Path] = []
    real_isolation = isolation_module.opencode_isolation

    def recording(base_env: object, **kwargs: object) -> object:
        isolation = real_isolation(base_env, **kwargs)  # type: ignore[arg-type]
        roots.append(isolation.root)
        return isolation

    exe = tmp_path / "opencode"
    exe.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('opencode v2.3.2')\n"
        "else:\n"
        "    sys.exit(1)\n"
    )
    exe.chmod(0o755)
    monkeypatch.setattr(isolation_module, "opencode_isolation", recording)
    with pytest.raises(GoUsageUnavailable, match="stopped before it was ready"):
        read_go_limits(str(exe))
    assert len(roots) == 1
    assert not roots[0].exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable fixture")
def test_go_timeout_terminates_native_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: HV-2 go-usage lifetime (root held through timeout kill/reap)."""
    monkeypatch.setattr("saberops.opencode_go_usage.GO_TIMEOUT", 2.0)
    exe = _fake_opencode(tmp_path, api_sleeps=True)
    with pytest.raises(GoUsageUnavailable, match="timed out"):
        read_go_limits(str(exe))
    env = _plugin_env(_trace(tmp_path)[1])
    root = Path(env["HOME"]).parent
    assert "saberops-opencode-isolation-" in str(root)
    assert not root.exists()


def test_native_plugin_picks_the_go_credential_and_never_writes_secrets(
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the OpenCode plugin hook")
    # A local plugin directory resolves @opencode/plugin from its own
    # node_modules; the shipped file is copied byte-for-byte into it.
    package = tmp_path / "plugin"
    package.mkdir()
    shutil.copyfile(_PLUGIN_DIR / "index.mjs", package / "index.mjs")
    shutil.copyfile(_PLUGIN_DIR / "package.json", package / "package.json")
    module = package / "node_modules/@opencode/plugin"
    module.mkdir(parents=True)
    (module / "package.json").write_text(
        json.dumps({"name": "@opencode/plugin", "type": "module", "exports": "./index.js"})
    )
    (module / "index.js").write_text(
        "export const Plugin = { define: (definition) => definition };\n"
    )
    output = tmp_path / "usage.json"
    script = tmp_path / "test.mjs"
    script.write_text(
        'import assert from "node:assert/strict";\n'
        'import {readFile} from "node:fs/promises";\n'
        f"const plugin=(await import({json.dumps((package / 'index.mjs').as_uri())})).default;\n"
        "const output=process.env.SABEROPS_GO_USAGE_OUTPUT;\n"
        "const read=async()=>JSON.parse(await readFile(output,'utf8'));\n"
        "const ctxFor=(connections)=>({integration:{connection:{\n"
        "  active:async(id)=>connections[id]?{id}:undefined,\n"
        "  resolve:async(connection)=>{const value=connections[connection.id];"
        "return value?{type:'key',key:value}:undefined;},\n"
        "}}});\n"
        "const calls=[];\n"
        "globalThis.fetch=async(url,opts)=>{\n"
        " calls.push(opts.headers.Authorization);\n"
        " assert.equal(url,'https://opencode.ai/zen/go/v1/usage');\n"
        " assert.equal(opts.redirect,'error');\n"
        " return {ok:true,text:async()=>JSON.stringify({usage:{monthly:{percent:16,"
        "resetsAt:'2030-01-01T00:00:00Z'}}})};\n"
        "};\n"
        "await plugin.setup(ctxFor({'opencode-go':'go-secret','opencode':'console-secret'}));\n"
        "assert.equal(calls.at(-1),'Bearer go-secret');\n"
        "assert.equal((await read()).usage.monthly.percent,16);\n"
        "await plugin.setup(ctxFor({'opencode':'console-secret'}));\n"
        "assert.equal(calls.at(-1),'Bearer console-secret');\n"
        "await plugin.setup(ctxFor({}));\n"
        "assert.equal((await read()).error,'GO_NOT_CONNECTED');\n"
        "globalThis.fetch=async()=>({ok:false,status:401,json:async()=>"
        "({error:{code:'api_key_expired'}})});\n"
        "await plugin.setup(ctxFor({'opencode-go':'go-secret'}));\n"
        "assert.equal((await read()).error,'HTTP_401_EXPIRED');\n"
        "globalThis.fetch=async()=>({ok:false,status:403,json:async()=>({})});\n"
        "await plugin.setup(ctxFor({'opencode-go':'go-secret'}));\n"
        "assert.equal((await read()).error,'HTTP_403');\n"
        "assert(!JSON.stringify(await read()).includes('secret'));\n"
    )
    result = subprocess.run(
        [node, str(script)],
        env={**os.environ, "SABEROPS_GO_USAGE_OUTPUT": str(output)},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
