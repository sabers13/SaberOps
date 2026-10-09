"""Isolated direct API tool loop. Invoked by absolute path in the worker process."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_REPLY = 8_000_000
MAX_TOOL_TEXT = 100_000
MAX_TURNS = 40


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def completion_url(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    path = parsed.path.rstrip("/")
    if parsed.hostname == "openrouter.ai":
        path = "/api/v1/chat/completions"
    elif not path.endswith("/chat/completions"):
        for suffix in ("/models", "/responses", "/completions"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        path += "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def scoped_path(root: Path, raw: str) -> Path:
    path = (root / raw).resolve()
    if not path.is_relative_to(root) or any(
        part == ".git" or part == ".env" or part.startswith(".env.")
        for part in path.relative_to(root).parts
    ):
        raise ValueError("Path is outside the permitted worktree or is protected")
    return path


def tool_definitions(read_only: bool) -> list[dict[str, Any]]:
    specs: list[tuple[str, str, dict[str, Any], list[str]]] = [
        ("read_file", "Read a UTF-8 worktree file", {"path": {"type": "string"}}, ["path"]),
        ("list_files", "List worktree files", {"path": {"type": "string"}}, []),
    ]
    if not read_only:
        specs += [
            (
                "write_file",
                "Write a UTF-8 worktree file",
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            (
                "run_command",
                "Run a command in the worktree (no shell)",
                {"argv": {"type": "array", "items": {"type": "string"}}},
                ["argv"],
            ),
        ]
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }
        for name, description, properties, required in specs
    ]


def execute_tool(root: Path, name: str, args: dict[str, Any], read_only: bool) -> str:
    allowed = {tool["function"]["name"] for tool in tool_definitions(read_only)}
    if name not in allowed:
        raise ValueError("Tool is not permitted for this role")
    if name == "run_command":
        argv = args.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise ValueError("Command argv must be a non-empty string array")
        # The API credential has been removed from os.environ before any tool.
        tool_env = {
            name: value
            for name, value in os.environ.items()
            if name in ("PATH", "LANG", "LC_ALL", "HOME", "TMPDIR", "SYSTEMROOT")
        }
        result = subprocess.run(
            argv,
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=tool_env,
        )
        return json.dumps(
            {
                "returncode": result.returncode,
                "stdout": result.stdout[:MAX_TOOL_TEXT],
                "stderr": result.stderr[:MAX_TOOL_TEXT],
            }
        )
    path = scoped_path(root, str(args.get("path", ".")))
    if name == "list_files":
        files = []
        for directory, dirs, names in os.walk(path, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules", ".venv"))
            for filename in sorted(names):
                candidate = Path(directory) / filename
                try:
                    scoped_path(root, str(candidate))
                except ValueError:
                    continue
                files.append(str(candidate.relative_to(root)))
                if len(files) >= 2000:
                    return "\n".join(files) + "\n[listing truncated]"
        return "\n".join(files)
    if name == "read_file":
        with path.open() as handle:
            return handle.read(MAX_TOOL_TEXT)
    content = args.get("content")
    if not isinstance(content, str) or len(content.encode()) > MAX_TOOL_TEXT:
        raise ValueError("Write content is invalid or exceeds 100 KB")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return "File written"


def run_agent(payload: dict[str, Any], credential: str, emit: Any) -> str:
    root = Path.cwd().resolve()
    connection = payload["connection"]
    headers = {row["name"]: row["value"] for row in connection.get("public_headers", [])}
    headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    if credential:
        name = connection.get("auth_header_name") or "Authorization"
        headers[name] = f"Bearer {credential}" if name.lower() == "authorization" else credential
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "Work on the requested task. Use only the provided tools. "
            "Do not access credentials or files outside the worktree. "
            "Return a concise final result.",
        },
        {"role": "user", "content": payload["task"]},
    ]
    if payload.get("input_images"):
        image_content: list[dict[str, Any]] = [{"type": "text", "text": payload["task"]}]
        for raw in payload["input_images"]:
            image = scoped_path(root, raw)
            mime = mimetypes.guess_type(image.name)[0]
            image_encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            image_content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_encoded}"}}
            )
        messages[-1]["content"] = image_content
    read_only = payload.get("read_only") is True
    tools = tool_definitions(read_only)
    opener = build_opener(NoRedirect())
    for _ in range(MAX_TURNS):
        body = {"model": payload["model"], "messages": messages, "tools": tools, "stream": False}
        encoded = json.dumps(body).encode()
        if len(encoded) > 10_000_000:
            raise ValueError("API conversation exceeds the size limit")
        request = Request(
            completion_url(connection["endpoint"]),
            data=encoded,
            headers=headers,
            method="POST",
        )
        try:
            with opener.open(request, timeout=60) as response:
                raw = response.read(MAX_REPLY + 1)
        except HTTPError as exc:
            raise ValueError(f"API request failed (HTTP {exc.code})") from None
        if len(raw) > MAX_REPLY:
            raise ValueError("API response exceeds the size limit")
        data = json.loads(raw)
        usage = data.get("usage", {})

        def count(value: Any) -> int | None:
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )

        emit(
            {
                "type": "usage",
                "model": data.get("model"),
                "input_tokens": count(usage.get("prompt_tokens")),
                "output_tokens": count(usage.get("completion_tokens")),
                "total_tokens": count(usage.get("total_tokens")),
                "cached_tokens": count(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                ),
            }
        )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("API returned no completion")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("API returned an invalid assistant message")
        calls = message.get("tool_calls")
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise ValueError("API returned unsupported content")
        if content:
            emit({"type": "text", "text": content})
        if not calls:
            if choice.get("finish_reason") != "stop":
                raise ValueError("API completion ended without a successful stop")
            return content
        if not isinstance(calls, list) or len(calls) > 16:
            raise ValueError("API returned too many tool calls")
        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        for call in calls:
            function = call.get("function", {})
            status = "completed"
            try:
                args = json.loads(function.get("arguments", "{}"))
                if not isinstance(args, dict):
                    raise ValueError("Invalid tool arguments")
                text = execute_tool(root, function.get("name", ""), args, read_only)
            except (OSError, ValueError, subprocess.SubprocessError):
                text = "Tool failed or was denied. Check the permitted path, role and arguments."
                status = "failed_or_denied"
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": text[:MAX_TOOL_TEXT]}
            )
            emit({"type": "tool", "name": function.get("name", ""), "status": status})
    raise ValueError("API tool loop reached its 40-turn limit")


def main() -> None:
    credential = os.environ.pop("SABEROPS_API_CREDENTIAL", "")
    # Ambient provider credentials never reach command tools.
    for name in list(os.environ):
        if any(marker in name.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            os.environ.pop(name, None)

    def emit(event: dict[str, Any]) -> None:
        line = json.dumps(event)
        if credential:
            line = line.replace(json.dumps(credential)[1:-1], "[redacted]")
        print(line, flush=True)

    try:
        payload = json.loads(sys.stdin.read(10_000_001))
        output = run_agent(payload, credential, emit)
        emit({"type": "result", "success": True, "text": output})
    except Exception as exc:
        # Never emit raw network errors or provider error payloads.
        detail = (
            str(exc)
            if isinstance(exc, ValueError) and str(exc).startswith(("API ", "Tool "))
            else "Direct API runtime failed"
        )
        emit({"type": "result", "success": False, "text": detail})
        sys.exit(1)


if __name__ == "__main__":
    main()
