"""Desktop terminal viewer for one captured transcript; never sends CLI input."""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path


def display(text: str) -> str:
    # Treat provider output as text: terminal controls/OSC cannot set clipboard,
    # links, window titles, or spoof the viewer's read-only label.
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)


def main() -> None:
    path = Path(sys.argv[1])
    print('SaberOps · captured CLI output · READ ONLY\n'
          'This viewer cannot send commands or attach to the provider session.\n'
          'Press Ctrl+C to close.\n', flush=True)
    with path.open(encoding='utf-8', errors='replace') as stream:
        try:
            while True:
                data = stream.read(16384)
                if data:
                    print(display(data), end='', flush=True)
                else:
                    time.sleep(0.3)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
