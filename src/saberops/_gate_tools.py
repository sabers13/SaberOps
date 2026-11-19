"""Shared loader for the standalone ``scripts/gate_tools.py`` module."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING


def _load_gate_tools() -> ModuleType:
    """Return the single ``gate_tools`` module from checkout or installed package."""
    existing = sys.modules.get("gate_tools")
    if existing is not None:
        return existing
    checkout_script = Path(__file__).resolve().parents[2] / "scripts" / "gate_tools.py"
    if checkout_script.is_file():
        spec = importlib.util.spec_from_file_location("gate_tools", checkout_script)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            sys.modules["gate_tools"] = module
            spec.loader.exec_module(module)
            return module
    return importlib.import_module("gate_tools")


if TYPE_CHECKING:
    import gate_tools
else:
    gate_tools = _load_gate_tools()

__all__ = ["gate_tools"]
