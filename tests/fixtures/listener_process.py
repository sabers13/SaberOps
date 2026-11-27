#!/usr/bin/env python3
"""Bind one loopback listener, publish its port, then wait to be released.

Used by the ADR-0100 drill to give a recorder a process group of its own that
holds a known listening socket.  It never detaches: the test releases it and
then checks what the recorder observed.  Usage:

    listener_process.py PORT_FILE RELEASE_FILE
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path


def main() -> int:
    port_file, release_file = Path(sys.argv[1]), Path(sys.argv[2])
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port_file.write_text(str(listener.getsockname()[1]), encoding="utf-8")

    deadline = time.monotonic() + 120.0
    while not release_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
