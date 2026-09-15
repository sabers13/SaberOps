"""``python -m saberops.devscope`` entry point."""

from __future__ import annotations

import sys

from saberops.devscope.cli import main

if __name__ == "__main__":  # pragma: no cover - trivial process entry
    sys.exit(main())
