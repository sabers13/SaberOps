"""``python -m orchestrator_mvp.devscope`` entry point."""

from __future__ import annotations

import sys

from orchestrator_mvp.devscope.cli import main

if __name__ == "__main__":  # pragma: no cover - trivial process entry
    sys.exit(main())
