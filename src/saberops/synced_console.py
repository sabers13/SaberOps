"""SaberOps terminal chat client: the same provider CLI/session as the app.

This managed interface goes through the coordinator. Independently opened vendor
TUIs are not silently imported or granted execution authority.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


def request(url: str, data: dict[str, str] | None = None) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            value = json.loads(response.read())
            if not isinstance(value, dict):
                raise ValueError("Invalid response from SaberOps")
            return value
    except urllib.error.HTTPError as exc:
        raise ValueError(json.loads(exc.read()).get("error", str(exc))) from exc


def display(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c >= " " or c in "\n\t")


def chat_loop(base: str, chat_id: str) -> None:
    endpoint = base + "/chats/" + urllib.parse.quote(chat_id, safe="") + "/messages"
    seen: set[int] = set()
    print(
        "SaberOps synchronized chat\n"
        "Messages use the app coordinator and saved provider sessions.\n"
        "/resume and /interrupt control a held agent; /exit closes this window.\n"
    )
    while True:
        state = request(endpoint)
        for message in state["messages"]:
            if message["id"] not in seen:
                seen.add(message["id"])
                print(
                    "\n"
                    + (
                        "Orchestrator"
                        if message["role"] == "assistant"
                        else message["role"].replace("_", " ").title()
                    )
                    + ":\n"
                    + display(message["text"])
                )
        manual = state.get("manual_control")
        blocked = bool(manual and manual["mode"] != "MANUAL")
        blocked = blocked or (state["busy"] and not manual)
        if manual and manual["mode"] == "MANUAL":
            control = request(base + manual["endpoint"])
            blocked = any(m["state"] in ("QUEUED", "RUNNING") for m in control.get("messages", []))
        if blocked:
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                if manual and manual["mode"] == "MANUAL":
                    request(base + manual["endpoint"] + "/interrupt", {})
                else:
                    raise
            continue
        text = input("\nYou > ").strip()
        if text == "/exit":
            return
        if not text:
            continue
        try:
            if text in ("/resume", "/interrupt"):
                if not manual:
                    print("No agent is under manual control. Take control in the app first.")
                else:
                    request(base + manual["endpoint"] + text, {})
                continue
            target = base + manual["endpoint"] + "/messages" if manual else endpoint
            request(target, {"message": text, "request_id": uuid.uuid4().hex})
        except ValueError as exc:
            print(str(exc))


def control_loop(base: str, path: str) -> None:
    endpoint = base + path
    seen: set[tuple[str, str]] = set()
    print(
        "SaberOps synchronized agent CLI\n"
        "Automation is held. Messages go to the actual provider CLI in its saved session.\n"
        "/resume returns control; /interrupt stops a manual reply; /exit keeps the run held.\n"
    )
    while True:
        state = request(endpoint)
        for message in state.get("messages", []):
            key = (message["id"], message["state"])
            if key in seen:
                continue
            seen.add(key)
            if message["response"]:
                print("\nAgent:\n" + display(message["response"]))
        if state["mode"] != "MANUAL":
            print("Control state: " + state["mode"])
            return
        if any(m["state"] in ("QUEUED", "RUNNING") for m in state.get("messages", [])):
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                request(endpoint + "/interrupt", {})
            continue
        try:
            text = input("\nYou > ").strip()
            if text == "/exit":
                return
            if text == "/resume":
                request(endpoint + "/resume", {})
                return
            if text == "/interrupt":
                request(endpoint + "/interrupt", {})
            elif text:
                request(endpoint + "/messages", {"message": text, "request_id": uuid.uuid4().hex})
        except ValueError as exc:
            print(str(exc))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--chat")
    group.add_argument("--control")
    args = parser.parse_args()
    parsed = urllib.parse.urlparse(args.url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Use the local SaberOps URL")
    try:
        if args.chat:
            chat_loop(args.url.rstrip("/"), args.chat)
        else:
            control_loop(args.url.rstrip("/"), args.control)
    except (KeyboardInterrupt, EOFError):
        print("\nWindow closed. No automatic resume was requested.")
    except (ValueError, OSError) as exc:
        print(
            "Connection stopped: " + str(exc) + "\nThe app remains the authority for control state."
        )


if __name__ == "__main__":
    main()
