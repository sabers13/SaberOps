#!/usr/bin/env python3
"""Hold one listening socket, then leave the worker's process group.

Started by the fake worker CLI's ``children`` mode (ADR-0100 drill).  It binds
an ephemeral loopback port while it is still a member of the worker's process
group, publishes the port and its pid, waits for the rig's detach marker, then
calls ``setsid`` so it outlives the group the engine fences and kills.  That is
the honest leftover case the inventory must report: a listener the job started
that is still listening after the job ended.

Usage: port_holder.py PORT_FILE DETACH_FILE PID_FILE [HOLD_SECONDS]
"""

from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path


def main() -> int:
    port_file, detach_file, pid_file = (Path(arg) for arg in sys.argv[1:4])
    hold_seconds = float(sys.argv[4]) if len(sys.argv) > 4 else 120.0

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    port_file.write_text(f"{port} {os.getpid()}\n", encoding="utf-8")

    deadline = time.monotonic() + 120.0
    while not detach_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        os.setsid()
    except OSError:
        pass
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    # Keep the socket open (and therefore LISTEN) beyond the worker's exit.
    time.sleep(hold_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
