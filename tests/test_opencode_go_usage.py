"""Go quotas use native OpenCode auth, without inference or leaking keys."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from saberops.cli_usage import CliUsageService, read_limits
from saberops.opencode_go_usage import GoUsageUnavailable, parse_go_limits, read_go_limits


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


def test_native_go_wrapper_only_enumerates_models(tmp_path: Path) -> None:
    exe = tmp_path / "opencode"
    trace = tmp_path / "trace"
    exe.write_text(
        "#!/usr/bin/env python3\nimport os,sys,json\n"
        f"open({str(trace)!r},'w').write(json.dumps(sys.argv[1:]))\n"
        "config=json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])\n"
        "assert config['enabled_providers']==['opencode-go']\n"
        "assert len(config['plugin'])==1\n"
        "assert config['plugin'][0].endswith('opencode_go_usage.mjs')\n"
        "with open(os.environ['SABEROPS_GO_USAGE_OUTPUT'],'w') as f:\n"
        " json.dump({'usage':{'monthly':{'percent':16,"
        "'resetsAt':'2030-01-01T00:00:00Z'}},'secret':'hidden'},f)\n"
    )
    exe.chmod(0o755)
    rows = read_limits(str(exe), "opencode")
    assert trace.read_text() == '["models", "opencode-go"]'
    assert rows[0]["remaining"] == 84
    assert "hidden" not in json.dumps(rows)


def test_go_usage_launch_uses_owned_isolation_root_with_exact_plugin_exception(
    tmp_path: Path,
) -> None:
    """Covers: HV-2 go-usage isolation (deny-all plus exact owned plugin)."""
    exe = tmp_path / "opencode"
    trace = tmp_path / "trace"
    exe.write_text(
        "#!/usr/bin/env python3\nimport os,sys,json\n"
        "env=dict(os.environ)\n"
        f"open({str(trace)!r},'w').write(json.dumps({{'argv':sys.argv[1:],'env':env}}))\n"
        "config=json.loads(env['OPENCODE_CONFIG_CONTENT'])\n"
        "assert config['enabled_providers']==['opencode-go']\n"
        "assert len(config['plugin'])==1\n"
        "assert config['plugin'][0].endswith('opencode_go_usage.mjs')\n"
        "policies={(p['resource'],p['effect']) for p in config['policies']}\n"
        "assert ('plugin:*','deny') in policies\n"
        "assert ('mcp:*','deny') in policies\n"
        "uri=config['plugin'][0]\n"
        f"open({str(tmp_path / 'saw.txt')!r},'w').write(uri)\n"
        "with open(os.environ['SABEROPS_GO_USAGE_OUTPUT'],'w') as f:\n"
        " json.dump({'usage':{'monthly':{'percent':16,"
        "'resetsAt':'2030-01-01T00:00:00Z'}}},f)\n"
    )
    exe.chmod(0o755)
    rows = read_go_limits(str(exe))
    child = json.loads(trace.read_text())
    assert child["argv"] == ["models", "opencode-go"]
    env = child["env"]
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert "OPENCODE_CONFIG" not in env and "OPENCODE_CONFIG_DIR" not in env
    root = Path(env["XDG_CONFIG_HOME"]).parent
    assert "saberops-opencode-isolation-" in str(root)
    # The exact-allow exception names the resolved shipped plugin path, and the
    # shipment itself is byte-identical to the committed plugin file.
    uri = (tmp_path / "saw.txt").read_text()
    assert uri.startswith("file://")
    assert Path(uri.removeprefix("file://")).resolve() == (
        Path(__file__).resolve().parents[1]
        / "src/saberops/defaults/opencode_go_usage.mjs"
    ).resolve()
    inline = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    exact = next(
        p
        for p in inline["policies"]
        if p["resource"].startswith("plugin:") and p["resource"] != "plugin:*"
    )
    assert exact["effect"] == "allow"
    assert inline["policies"].index(exact) > inline["policies"].index(
        {"action": "integration.use", "resource": "plugin:*", "effect": "deny"}
    )
    assert not root.exists(), "owned root is removed after the reader exits"
    assert rows[0]["remaining"] == 84


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable fixture")
def test_go_timeout_terminates_native_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("saberops.opencode_go_usage.GO_TIMEOUT", 0.4)
    pid = tmp_path / "pid"
    exe = tmp_path / "opencode"
    exe.write_text(
        "#!/usr/bin/env python3\nimport os,time\n"
        f"open({str(pid)!r},'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    exe.chmod(0o755)
    with pytest.raises(GoUsageUnavailable, match="timed out"):
        read_go_limits(str(exe))
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


def test_go_cache_preserves_last_good_on_missing_subscription(tmp_path: Path) -> None:
    calls = [0]

    def limits(exe: str, provider: str) -> list[dict[str, object]]:
        calls[0] += 1
        if calls[0] > 1:
            raise GoUsageUnavailable(
                "No OpenCode Go subscription is available for the connected account."
            )
        return parse_go_limits({"usage": {"monthly": {"percent": 16}}})

    service = CliUsageService(
        home=tmp_path, which=lambda exe: exe if exe == "opencode" else None, limits=limits
    )
    first = next(c for c in service.snapshot(refresh=True) if c["provider"] == "opencode")
    failed = next(
        c for c in service.snapshot(refresh=True, force=True) if c["provider"] == "opencode"
    )
    assert failed["windows"] == first["windows"]
    assert failed["checked_at"] == first["checked_at"]
    assert "No OpenCode Go subscription" in failed["limits_error"]


def test_native_auth_hook_sanitizes_usage_and_never_follows_redirects(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the OpenCode plugin hook")
    plugin = Path(__file__).resolve().parents[1] / "src/saberops/defaults/opencode_go_usage.mjs"
    output = tmp_path / "usage.json"
    script = tmp_path / "test.mjs"
    script.write_text(
        'import assert from "node:assert/strict";\n'
        'import {readFile} from "node:fs/promises";\n'
        f"import plugin from {json.dumps(plugin.as_uri())};\n"
        "const hooks=await plugin();\n"
        "assert.equal(hooks.auth.provider,'opencode-go');\n"
        "globalThis.fetch=async(url,opts)=>{\n"
        " assert.equal(url,'https://opencode.ai/zen/go/v1/usage');\n"
        " assert.equal(opts.redirect,'error');\n"
        " assert.equal(opts.headers.Authorization,'Bearer test-secret');\n"
        " return {ok:true,text:async()=>JSON.stringify({secret:'test-secret',"
        "usage:{monthly:{percent:16,resetsAt:'2030-01-01T00:00:00Z',"
        "secret:'test-secret'}}})};\n"
        "};\n"
        "await hooks.auth.loader(async()=>({type:'api',key:'test-secret'}));\n"
        "const data=await readFile(process.env.SABEROPS_GO_USAGE_OUTPUT,'utf8');\n"
        "assert(!data.includes('test-secret'));\n"
        "assert.equal(JSON.parse(data).usage.monthly.percent,16);\n"
        "globalThis.fetch=async()=>({ok:false,status:403});\n"
        "await hooks.auth.loader(async()=>({type:'api',key:'test-secret'}));\n"
        "assert.equal(JSON.parse(await readFile(process.env.SABEROPS_GO_USAGE_OUTPUT,"
        "'utf8')).error,'HTTP_403');\n"
        "globalThis.fetch=async()=>{throw Error('must not query without API auth')};\n"
        "await hooks.auth.loader(async()=>({type:'oauth'}));\n"
        "assert.equal(JSON.parse(await readFile(process.env.SABEROPS_GO_USAGE_OUTPUT,"
        "'utf8')).error,'GO_NOT_CONNECTED');\n"
    )
    result = subprocess.run(
        [node, str(script)],
        env={**os.environ, "SABEROPS_GO_USAGE_OUTPUT": str(output)},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode()
