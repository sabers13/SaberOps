"""Deterministic fake tmux for the F23 watch tests.

It models exactly the small command surface ``saberops.watch.Tmux`` uses (and
deliberately refuses everything else, so a test fails loudly if the watcher
ever tries to deliver input).  State and a call log live under
``$ORCH_FAKE_TMUX_STATE``:

* ``state.json`` — sessions and windows, including the owner marker and the
  ``@saberops_job`` window option;
* ``calls.jsonl`` — one JSON argv array per invocation.

The fake never talks to a real tmux server, so tests cannot touch the
owner's ``firstmate`` session or any other session on the default socket.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn, cast

STATE_ENV = "ORCH_FAKE_TMUX_STATE"


def _root() -> Path:
    raw = os.environ.get(STATE_ENV)
    if not raw:
        _fail(f"{STATE_ENV} is not set", code=2)
    root = Path(raw)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _fail(message: str, *, code: int = 1) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _load(root: Path) -> dict[str, Any]:
    path = root / "state.json"
    if not path.exists():
        return {"sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"corrupt fake tmux state: {exc}", code=2)
    if not isinstance(data, dict):
        _fail("corrupt fake tmux state: not an object", code=2)
    sessions = data.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        _fail("corrupt fake tmux state: sessions not an object", code=2)
    return cast(dict[str, Any], data)


def _save(root: Path, data: dict[str, Any]) -> None:
    (root / "state.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _log(root: Path, argv: list[str]) -> None:
    with (root / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(argv) + "\n")


def _flag(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args):
        _fail(f"{name} needs a value", code=2)
    return args[index + 1]


def _session(data: dict[str, Any], name: str) -> dict[str, Any]:
    session = data["sessions"].get(name)
    if not isinstance(session, dict):
        _fail(f"can't find session: {name}")
    return cast(dict[str, Any], session)


def _window(session: dict[str, Any], name: str) -> dict[str, Any]:
    for window in session.get("windows", []):
        if window.get("name") == name:
            return cast(dict[str, Any], window)
    _fail(f"can't find window: {name}")


def _split_target(target: str) -> tuple[str, str | None]:
    name, sep, window = target.partition(":")
    return name, (window if sep else None)


def _command_after_double_dash(args: list[str]) -> list[str]:
    if "--" not in args:
        return []
    return args[args.index("--") + 1 :]


def main(argv: list[str]) -> int:
    root = _root()
    _log(root, argv)
    data = _load(root)
    sessions = data["sessions"]
    if not argv:
        _fail("missing command", code=2)
    command, rest = argv[0], argv[1:]

    if command == "has-session":
        name = _flag(rest, "-t")
        return 0 if name in sessions else 1

    if command == "show-options":
        name = _flag(rest, "-t")
        option = _flag(rest, "-v")
        session = _session(data, name or "")
        if option == "@saberops_watch" and session.get("ours"):
            print("1")
            return 0
        _fail("invalid option")
        return 1

    if command == "set-option":
        target = _flag(rest, "-t") or ""
        window_option = "-w" in rest
        positional: list[str] = []
        index = 0
        while index < len(rest):
            part = rest[index]
            if part == "-t":
                index += 2
                continue
            if part == "-w":
                index += 1
                continue
            positional.append(part)
            index += 1
        if len(positional) < 2:
            _fail("set-option needs an option and a value", code=2)
        option, value = positional[0], positional[1]
        if window_option:
            session_name, window_name = _split_target(target)
            session = _session(data, session_name)
            if not window_name:
                _fail("window option needs a session:window target")
            window = _window(session, window_name)
            window["job"] = None if option != "@saberops_job" else value
        else:
            session = _session(data, target)
            if option == "@saberops_watch":
                session["ours"] = value == "1"
            else:
                _fail(f"unexpected session option: {option}", code=2)
        _save(root, data)
        return 0

    if command == "new-session":
        name = _flag(rest, "-s")
        window_name = _flag(rest, "-n") or "0"
        if not name:
            _fail("new-session needs -s", code=2)
        if name in sessions:
            _fail(f"duplicate session: {name}")
        sessions[name] = {
            "ours": False,
            "windows": [
                {"name": window_name, "job": None, "argv": _command_after_double_dash(rest)}
            ],
        }
        _save(root, data)
        return 0

    if command == "new-window":
        session_name = _flag(rest, "-t") or ""
        window_name = _flag(rest, "-n")
        if not window_name:
            _fail("new-window needs -n", code=2)
        session = _session(data, session_name)
        if any(window.get("name") == window_name for window in session["windows"]):
            _fail(f"duplicate window: {window_name}")
        session["windows"].append(
            {"name": window_name, "job": None, "argv": _command_after_double_dash(rest)}
        )
        _save(root, data)
        return 0

    if command == "list-windows":
        session_name = _flag(rest, "-t") or ""
        session = _session(data, session_name)
        for window in session["windows"]:
            print(f"{window.get('name', '')}\t{window.get('job') or ''}")
        return 0

    if command == "rename-window":
        target = _flag(rest, "-t") or ""
        new_name = rest[-1]
        session_name, window_name = _split_target(target)
        session = _session(data, session_name)
        window = _window(session, window_name or "")
        if any(
            other is not window and other.get("name") == new_name
            for other in session["windows"]
        ):
            _fail(f"duplicate window: {new_name}")
        window["name"] = new_name
        _save(root, data)
        return 0

    if command in ("attach-session", "switch-client"):
        session_name = _flag(rest, "-t") or ""
        _session(data, session_name)
        return 0

    if command == "kill-session":
        session_name = _flag(rest, "-t") or ""
        sessions.pop(session_name, None)
        _save(root, data)
        return 0

    _fail(f"refused command: {' '.join(argv)}", code=2)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
