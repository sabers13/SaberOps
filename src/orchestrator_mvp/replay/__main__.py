"""Execution entry point for python -m orchestrator_mvp.replay."""

from __future__ import annotations

import sys

from orchestrator_mvp.replay.cli import main

if __name__ == "__main__":
    sys.exit(main())
