"""Native, local desktop folder selection without browser file uploads."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from pathlib import Path

_picker_lock = threading.Lock()


class FolderPickerError(RuntimeError):
    """The desktop picker could not be opened or completed."""


def choose_folder() -> str | None:
    """Return a selected absolute folder, or None when the owner cancels."""
    if sys.platform == "darwin":
        command = ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose project")']
    elif sys.platform == "win32":
        command = [
            "powershell.exe",
            "-NoProfile",
            "-STA",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$picker = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$picker.Description = 'Choose project'; "
            "if ($picker.ShowDialog() -eq 'OK') { $picker.SelectedPath }",
        ]
    elif shutil.which("zenity"):
        command = ["zenity", "--file-selection", "--directory", "--title=Choose project"]
    elif shutil.which("kdialog"):
        command = [
            "kdialog",
            "--getexistingdirectory",
            str(Path.home()),
            "--title",
            "Choose project",
        ]
    else:
        raise FolderPickerError(
            "Folder chooser unavailable. Paste the folder path and use ‘Use path’."
        )
    if not _picker_lock.acquire(blocking=False):
        raise FolderPickerError("A folder chooser is already open. Complete or close it first.")
    try:
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=120, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FolderPickerError(
                "Could not open the folder chooser. You can use a path instead."
            ) from exc
        if (
            result.returncode == 1
            and sys.platform != "darwin"
            and "cannot open display" not in result.stderr.lower()
        ):
            return None
        if sys.platform == "darwin" and "(-128)" in result.stderr:
            return None
        if result.returncode != 0:
            raise FolderPickerError(
                "Could not open the folder chooser. You can use a path instead."
            )
        selected = result.stdout.strip()
        if not selected:
            return None
        folder = Path(selected).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise FolderPickerError(
                "The selected folder is no longer available. Choose another folder."
            )
        return str(folder.resolve())
    finally:
        _picker_lock.release()
