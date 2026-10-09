"""Exact-session terminal access, with a lease separate from run completion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from saberops.ownership import Liveness, ProcessIdentity, probe_liveness
from saberops.session_control import ControlConflict, SessionControls

PROVIDERS = {
    "anthropic": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "antigravity": "agy",
    "cline": "cline",
    "copilot": "copilot",
}
BUSY = ("STARTING", "ACTIVE")


@lru_cache(maxsize=16)
def _channel_flag(binary: str, modified: int) -> bool:
    """Inspect installed CLI support; never submit a provider turn."""
    try:
        result = subprocess.run([binary, "--help"], capture_output=True, text=True,
                                timeout=5, check=False)
        flag = b"--dangerously-load-development-channels"
        if result.returncode == 0 and flag.decode() in result.stdout:
            return True
        # Claude hides development flags from help (and ignores unknown options
        # with --help). Inspect only its installed executable, never account files.
        with Path(binary).open("rb") as source:
            previous = b""
            for _ in range(2048):
                chunk = source.read(256 * 1024)
                if not chunk:
                    break
                if flag in previous + chunk:
                    return True
                previous = chunk[-len(flag):]
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def supports_shared_chat(provider: str) -> bool:
    if provider == "codex":
        binary = shutil.which("codex")
        if not binary:
            return False
        try:
            result = subprocess.run([binary, "queue", "--help"], capture_output=True,
                                    text=True, timeout=5, check=False)
            return result.returncode == 0 and "--remote" in result.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    if provider not in ("anthropic", "claude-code"):
        return False
    binary = shutil.which("claude")
    if not binary:
        return False
    try:
        return _channel_flag(binary, Path(binary).stat().st_mtime_ns)
    except OSError:
        return False


def _schema(conn: Any) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS native_clients (
        key TEXT PRIMARY KEY, id TEXT NOT NULL, session_id TEXT NOT NULL,
        state TEXT NOT NULL, identity TEXT, child_identity TEXT, updated REAL NOT NULL,
        error TEXT NOT NULL DEFAULT '')""")


def client_state(controls: SessionControls, key: str) -> dict[str, Any] | None:
    if not controls.path.is_file():
        return None
    with controls.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_clients'").fetchone():
            return None
        row = conn.execute("SELECT * FROM native_clients WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    if data["state"] in BUSY:
        identities = [
            ProcessIdentity.from_json(data[k]) for k in ("identity", "child_identity") if data[k]
        ]
        if identities and all(probe_liveness(i) == Liveness.DEAD for i in identities):
            from saberops.monitor import group_has_live_members

            child = ProcessIdentity.from_json(data["child_identity"])
            if child and child.process_group_id and group_has_live_members(child.process_group_id):
                data.update(
                    error="The terminal owner stopped but its process group is not quiescent."
                )
            else:
                data.update(state="INTERRUPTED", error="The terminal client stopped unexpectedly.")
        elif not identities and time.time() - data["updated"] > 30:
            data.update(state="FAILED", error="The terminal did not start its session client.")
    return {k: v for k, v in data.items() if k not in ("identity", "child_identity")}


def session_key(data: dict[str, Any]) -> str:
    return hashlib.sha256(
        (
            str(data.get("provider"))
            + ":"
            + str(data.get("binding"))
            + ":"
            + str(data.get("native_id"))
        ).encode()
    ).hexdigest()


def is_busy(controls: SessionControls, sid: str) -> bool:
    if not controls.path.is_file():
        return False
    with controls.connect() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_clients'").fetchone():
            return False
        keys = [
            r[0] for r in conn.execute("SELECT key FROM native_clients WHERE session_id=?", (sid,))
        ]
    return any((client_state(controls, key) or {}).get("state") in BUSY for key in keys)


def reserve(controls: SessionControls, data: dict[str, Any]) -> str:
    key = session_key(data)
    with controls.connect(write=True) as conn:
        _schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        existing = client_state(controls, key)
        if existing and existing["state"] in BUSY:
            raise ControlConflict(
                "This exact session already has an open terminal client. Close it first."
            )
        messages = conn.execute(
            "SELECT COUNT(*) FROM control_messages WHERE session_id=? "
            "AND state IN ('QUEUED','RUNNING')",
            (data["id"],),
        ).fetchone()[0]
        if messages:
            raise ControlConflict("Wait for the manual reply before opening the native CLI.")
        current = controls.get(data["id"]) or {}
        if current.get("mode") == "RESUMING":
            raise ControlConflict("Automation is resuming; refresh before opening the CLI.")
        others = conn.execute(
            "SELECT id FROM controls WHERE native_id=? AND id<>? "
            "AND mode IN ('ACTIVE','REQUESTED','MANUAL','RESUMING')",
            (data["native_id"], data["id"]),
        ).fetchall()
        if any(
            (controls.get(row[0]) or {}).get("mode")
            in ("ACTIVE", "REQUESTED", "MANUAL", "RESUMING", "UNKNOWN")
            for row in others
        ):
            raise ControlConflict(
                "This provider session is in use by another invocation. Open its live card."
            )
        token = uuid.uuid4().hex
        conn.execute(
            "INSERT OR REPLACE INTO native_clients VALUES (?,?,?,?,NULL,NULL,?,?)",
            (key, token, data["id"], "STARTING", time.time(), ""),
        )
        return token


def update_client(
    controls: SessionControls,
    data: dict[str, Any],
    state: str,
    *,
    identity: ProcessIdentity | None = None,
    child: ProcessIdentity | None = None,
    error: str = "",
) -> None:
    with controls.connect(write=True) as conn:
        conn.execute(
            "UPDATE native_clients SET state=?,identity=COALESCE(?,identity), "
            "child_identity=COALESCE(?,child_identity),updated=?,error=? WHERE key=? AND id=?",
            (
                state,
                json.dumps(identity.to_dict()) if identity else None,
                json.dumps(child.to_dict()) if child else None,
                time.time(),
                error,
                data["key"],
                data["token"],
            ),
        )


def claim_client(
    controls: SessionControls, data: dict[str, Any], identity: ProcessIdentity
) -> None:
    """A delayed/duplicate terminal must never adopt a newer launch reservation."""
    with controls.connect(write=True) as conn:
        changed = conn.execute(
            "UPDATE native_clients SET identity=?,updated=? WHERE key=? AND id=? "
            "AND state='STARTING' AND identity IS NULL AND updated>=?",
            (
                json.dumps(identity.to_dict()),
                time.time(),
                data["key"],
                data["token"],
                time.time() - 30,
            ),
        )
        if changed.rowcount != 1:
            raise ControlConflict(
                "This terminal launch expired or already has an owner. "
                "Open the current session through SaberOps."
            )


def fail_launch(controls: SessionControls, data: dict[str, Any], error: str) -> None:
    """An uncertain desktop launch must not release a claimed native lease."""
    with controls.connect(write=True) as conn:
        conn.execute(
            "UPDATE native_clients SET state=CASE WHEN state='STARTING' AND identity IS NULL "
            "THEN 'FAILED' ELSE state END,error=?,updated=? WHERE key=? AND id=?",
            (error, time.time(), data["key"], data["token"]),
        )


def capability(data: dict[str, Any]) -> dict[str, Any]:
    binary = PROVIDERS.get(str(data.get("native_provider") or data.get("provider")))
    native_id = data.get("native_id")
    reason = "No verified saved provider session was recorded for this call."
    available = bool(
        binary
        and isinstance(native_id, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", native_id)
    )
    if available and not shutil.which(str(binary)):
        available = False
        reason = f"Install {binary} to reopen this saved session."
    if data.get("completion") and data["completion"].get("quiescent") is not True:
        available = False
        reason = (
            "Provider shutdown is unconfirmed; native access is held "
            "to prevent competing execution."
        )
    mode = (
        "attach"
        if data.get("native_attach_ready")
        else "reopen"
        if data.get("completion")
        else "handoff"
    )
    if mode == "handoff" and data.get("native_handoff_allowed") is False:
        available = False
        reason = (
            "This isolated execution supports synchronized takeover; native handoff is unavailable."
        )
    if data.get("selectors", {}).get("ORCH_COPILOT_PROFILE_ID") == "copilot-minimax-byok":
        available = False
        reason = "The native Copilot BYOK profile is not configured for exact-session resume."
    if data.get("control_mode") in ("UNKNOWN", "INTERRUPTED", "RESUMING") and not data.get(
        "completion"
    ):
        available = False
        reason = "The execution owner cannot currently confirm a safe handoff."
    return {
        "native_cli_available": available,
        "native_cli_mode": mode,
        "native_cli_access_reason": "" if available else reason,
    }


def native_argv(data: dict[str, Any]) -> list[str]:
    provider = str(data.get("native_provider") or data["provider"])
    binary = shutil.which(PROVIDERS.get(provider, ""))
    if not binary:
        raise ControlConflict("The provider CLI is not installed.")
    sid, model, cwd = str(data["native_id"]), str(data["model"]), str(data["directory"])
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", sid):
        raise ControlConflict("The provider session identifier is invalid.")
    readonly = bool(data.get("read_only"))
    if provider in ("anthropic", "claude-code"):
        from saberops.workers.claude import _SETTINGS

        args = [
            binary,
            "--setting-sources=",
            "--settings",
            _SETTINGS,
            "--resume",
            sid,
            "--model",
            model,
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        ]
        if data.get("role") == "orchestrator":
            args += [
                "--append-system-prompt",
                "This is direct human chat in a resumed SaberOps orchestrator session. "
                "Reply naturally in readable text, not the reply/action JSON envelope "
                "used by earlier automated turns. Earlier routing instructions apply "
                "to those automated turns only. If the SaberOps channel tools are available, "
                "follow their instructions to request governed worker dispatch. Otherwise "
                "do not claim this chat launches workers. Never accept a candidate. "
                "Keep the existing read-only and evidence requirements.",
            ]
        if data.get("shared_chat") and data.get("descriptor"):
            config = {"mcpServers": {"saberops": {
                "command": sys.executable,
                "args": ["-I", str(Path(__file__).with_name("native_channel.py")),
                         str(data["descriptor"])],
            }}}
            args[args.index("--mcp-config") + 1] = json.dumps(config)
            args += ["--dangerously-load-development-channels", "server:saberops",
                     "--allowedTools", "mcp__saberops__reply,mcp__saberops__request_work"]
        tools = "Read,Glob,Grep"
        if data.get("shared_chat"):
            tools += ",mcp__saberops__reply,mcp__saberops__request_work"
        return args + (
            ["--permission-mode", "default" if data.get("shared_chat") else "plan",
             "--tools", tools]
            if readonly
            else ["--permission-mode", "acceptEdits"]
        )
    if provider == "codex":
        if data.get("shared_chat") and data.get("descriptor"):
            return [sys.executable, "-I", str(Path(__file__).with_name("native_codex_chat.py")),
                    str(data["descriptor"])]
        return [
            binary,
            "--cd",
            cwd,
            "--sandbox",
            "read-only" if readonly else "workspace-write",
            "resume",
            sid,
            "--model",
            model,
        ]
    if provider == "opencode":
        return [binary, "--pure", "--session", sid, "--model", model]
    if provider == "antigravity":
        return [
            binary,
            "--conversation",
            sid,
            "--model",
            model,
            "--add-dir",
            cwd,
            "--mode",
            "plan" if readonly else "accept-edits",
        ]
    if provider == "cline":
        return [
            binary,
            "--tui",
            "--id",
            sid,
            "-P",
            "cline-pass",
            "-m",
            model,
            "-c",
            cwd,
            *(["--plan", "--auto-approve", "false"] if readonly else []),
        ]
    if provider == "copilot":
        return [
            binary,
            "--resume=" + sid,
            "--model",
            model,
            *(["--plan", "--available-tools=view,grep,glob"] if readonly else []),
        ]
    raise ControlConflict("This provider has no native CLI session interface.")


def interactive_environment(env: dict[str, str]) -> dict[str, str]:
    """Interactive clients detect their own UI; headless server flags stay outside."""
    result = dict(env)
    for key in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE", "CI",
                "CLAUDE_CODE_SIMPLE"):
        result.pop(key, None)
    if not result.get("TERM") or result["TERM"] == "dumb":
        result["TERM"] = "xterm-256color"
    return result


def native_environment(data: dict[str, Any]) -> dict[str, str]:
    # Native subscription clients must not inherit ambient API billing credentials.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANTHROPIC_", "ORCH_GATEWAY_", "CLAUDE_CODE_USE_"))
        and k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY")
    }
    env["CLAUDE_CODE_DISABLE_FAST_MODE"] = "1"
    env.update(data.get("selectors", {}))
    provider = data.get("native_provider") or data["provider"]
    if provider == "antigravity":
        settings = Path.home() / ".gemini/antigravity-cli/settings.json"
        if settings.is_file():
            config = json.loads(settings.read_text())
            if config.get("modelProvider") == "gemini":
                raise ControlConflict(
                    "Antigravity is configured for API-key billing. "
                    "Switch to account login before opening a subscription session."
                )
    if provider == "copilot":
        from saberops.models import WorkerRequest
        from saberops.workers.copilot import _resolve_profile_from_request, build_attempt_env

        req = WorkerRequest("", Path(data["directory"]), data["model"], env=data.get("selectors"))
        terminal_metadata: dict[str, str] = {
            k: env[k] for k in ("TERM", "COLORTERM") if k in env
        }
        env = build_attempt_env(_resolve_profile_from_request(req))
        env.update(terminal_metadata)
        # Do not silently turn a BYOK backend into a GitHub subscription turn.
        if data.get("selectors", {}).get("ORCH_COPILOT_PROFILE_ID") == "copilot-minimax-byok":
            raise ControlConflict(
                "This Copilot BYOK profile needs its native backend configuration; "
                "use the synchronized CLI until that profile is supported."
            )
    if provider == "opencode" and data.get("read_only"):
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            {
                "permission": {
                    "*": "deny",
                    "read": "allow",
                    "glob": "allow",
                    "grep": "allow",
                    "list": "allow",
                }
            }
        )
    return interactive_environment(env)


def saved_directory(data: dict[str, Any], folder: Path) -> Path:
    """Removed verification trees are reconstructed from their exact recorded tree."""
    if not data.get("worktree"):
        raise ControlConflict("The original working directory was not recorded for this session.")
    original = Path(str(data["worktree"]))
    if original.is_dir() and not data.get("completion"):
        return original
    tree = (data.get("completion") or {}).get("source_tree")
    repository = data.get("repository") or data.get("git_common_dir")
    target = folder / (str(data["id"]) + "-inspection")
    if tree and repository:
        if not re.fullmatch(r"[0-9a-f]{40,64}", tree):
            raise ControlConflict("The recorded source tree is invalid.")
        prefix = (
            ["git", "--git-dir", str(data["git_common_dir"])]
            if data.get("git_common_dir")
            else ["git", "-C", str(repository)]
        )
        if not target.is_dir():
            archive = folder / (data["id"] + ".tar")
            try:
                subprocess.run(
                    [*prefix, "archive", "--format=tar", "--output", str(archive), tree],
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                target.mkdir(mode=0o700)
                with tarfile.open(archive) as tar:
                    tar.extractall(target, filter="data")
            except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
                shutil.rmtree(target, ignore_errors=True)
                raise ControlConflict(
                    "The original source snapshot could not be restored."
                ) from exc
            finally:
                archive.unlink(missing_ok=True)
        return target
    if data.get("role") == "orchestrator" and data.get("read_only"):
        target.mkdir(mode=0o700, exist_ok=True)
        return target
    raise ControlConflict(
        "The original worktree is missing and no exact source snapshot is available."
    )


def wait_for_client_shutdown(controls: SessionControls, sid: str) -> bool:
    # The terminal-side owner sees the terminal control state and stops its own
    # process group. No verification/continuation proceeds until it confirms that.
    deadline = time.monotonic() + 8
    while is_busy(controls, sid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def tmux_workspace() -> dict[str, Any]:
    """Read workspace state without creating clients or changing execution."""
    tmux = shutil.which("tmux")
    if not tmux:
        return {"available": False, "exists": False, "clients": []}
    try:
        result = subprocess.run(
            [
                tmux,
                "list-panes",
                "-s",
                "-t",
                "=saberops-clients",
                "-F",
                "#{pane_id}\t#{@saberops_client}\t#{window_name}",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": True, "exists": False, "clients": []}
    clients = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3:
            clients.append({"pane": fields[0], "name": fields[1] or fields[2], "window": fields[2]})
    return {"available": True, "exists": result.returncode == 0, "clients": clients}


def _tmux_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    tmux = shutil.which("tmux")
    if not tmux:
        raise ControlConflict("tmux is not installed. Install it or use the desktop CLI.")
    return subprocess.run([tmux, *args], capture_output=True, text=True, timeout=5, check=False)


def _ensure_tmux_workspace() -> None:
    if _tmux_run(["has-session", "-t", "=saberops-clients"]).returncode:
        created = _tmux_run(
            ["new-session", "-d", "-s", "saberops-clients", "-x", "180", "-y", "52"]
        )
        if created.returncode and _tmux_run(["has-session", "-t", "=saberops-clients"]).returncode:
            raise OSError("tmux workspace creation failed: " + created.stderr.strip())
    # Scope mouse support to our clients, leaving the owner's other sessions alone.
    _tmux_run(["set-option", "-t", "saberops-clients", "mouse", "on"])


def focus_tmux_client(name: str) -> bool:
    client = next((c for c in tmux_workspace()["clients"] if c["name"] == name), None)
    if not client:
        return False
    target = "=saberops-clients:agents"
    if client["window"] != "agents":
        exists = _tmux_run(["list-panes", "-t", target]).returncode == 0
        moved = _tmux_run(
            ["join-pane", "-d", "-s", client["pane"], "-t", target]
            if exists
            else ["rename-window", "-t", client["pane"], "agents"]
        )
        if moved.returncode:
            raise OSError("Could not group the tmux client: " + moved.stderr.strip())
    _tmux_run(["set-option", "-p", "-t", client["pane"], "@saberops_client", name])
    _tmux_run(["set-window-option", "-t", target, "pane-border-status", "top"])
    _tmux_run(["set-window-option", "-t", target, "pane-border-format", "#{@saberops_client}"])
    _tmux_run(["select-layout", "-t", target, "tiled"])
    for args in (["select-window", "-t", client["pane"]], ["select-pane", "-t", client["pane"]]):
        result = _tmux_run(args)
        if result.returncode:
            raise OSError("Could not select the tmux client: " + result.stderr.strip())
    return True


def show_tmux_workspace(*, force_window: bool = False) -> dict[str, Any]:
    """Attach a visible desktop terminal, reusing an already attached workspace."""
    _ensure_tmux_workspace()
    connected = _tmux_run(["list-clients", "-t", "=saberops-clients", "-F", "#{client_tty}"])
    if not force_window and connected.returncode == 0 and connected.stdout.strip():
        return {
            "visible": True,
            "reused": True,
            "message": "Sessions are ready in your attached tmux terminal.",
        }
    tmux = shutil.which("tmux")
    assert tmux
    try:
        terminal_launch(
            [tmux, "attach-session", "-t", "=saberops-clients"], "desktop", "Agent workspace"
        )
    except (ControlConflict, OSError, subprocess.SubprocessError) as exc:
        return {
            "visible": False,
            "message": "The workspace is ready, but its terminal could not open: "
            + str(exc)
            + " You can attach manually with: tmux attach -t saberops-clients",
        }
    return {
        "visible": True,
        "reused": False,
        "message": "Opened the shared tmux workspace in your terminal.",
    }


def terminal_launch(args: list[str], backend: str, name: str) -> None:
    if backend == "tmux":
        _ensure_tmux_workspace()
        # tmux treats the command as shell text; quote each argv value explicitly.
        import shlex

        target = "=saberops-clients:agents"
        exists = _tmux_run(["list-panes", "-t", target]).returncode == 0
        result = _tmux_run(
            ["split-window", "-d", "-P", "-F", "#{pane_id}", "-t", target, shlex.join(args)]
            if exists
            else [
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                "saberops-clients:",
                "-n",
                "agents",
                shlex.join(args),
            ]
        )
        if result.returncode == 0 and result.stdout.strip():
            pane = result.stdout.strip()
            # The provider supervisor may already be running. Cosmetic failures
            # must not release its execution lease or report the launch as failed.
            for command in (
                ["set-option", "-p", "-t", pane, "@saberops_client", name],
                ["select-pane", "-t", pane, "-T", name],
                ["set-window-option", "-t", target, "pane-border-status", "top"],
                ["set-window-option", "-t", target, "pane-border-format", "#{@saberops_client}"],
                ["select-layout", "-t", target, "tiled"],
                ["select-window", "-t", target],
            ):
                try:
                    _tmux_run(command)
                except (OSError, subprocess.SubprocessError):
                    pass
    elif backend == "desktop":
        terminal = shutil.which("gnome-terminal")
        if not terminal or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise ControlConflict("No desktop terminal is available. Choose Open in tmux.")
        result = subprocess.run(
            [
                terminal,
                "--title=SaberOps · " + name,
                *(["--maximize"] if name == "Agent workspace" else []),
                "--",
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    else:
        raise ControlConflict("Choose desktop or tmux.")
    if result.returncode:
        raise OSError("Terminal launch failed: " + result.stderr.strip()[:400])
