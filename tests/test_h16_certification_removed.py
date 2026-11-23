"""H16: the pre-phase-1 certification engine and ``orch certify`` are gone.

The benchmark rig owns the live drills now.  This guard keeps a later change
from reintroducing a second, evidence-file-driven certification path beside it.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys


def test_certification_module_is_not_importable() -> None:
    assert importlib.util.find_spec("saberops.certification") is None


def test_certify_verb_is_not_accepted() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "certify", "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "invalid choice" in proc.stderr
    help_proc = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0
    assert "certify" not in help_proc.stdout
