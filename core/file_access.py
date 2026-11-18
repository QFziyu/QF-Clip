"""Restore destination-directory access after moving files out of private temp dirs."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


class FileAccessError(RuntimeError):
    """An output file could not inherit its destination directory's Windows ACL."""


def restore_inherited_access(path: str | Path) -> Path:
    """Reset one output file's Windows DACL to its parent's inherited permissions.

    Python's private temporary directories can attach a protected DACL to rendered
    files. A same-volume move retains that DACL, so a desktop user may need admin
    rights even when the output directory is normally accessible. Resetting the
    *file* after its final move applies the destination directory's access policy.
    This does not grant extra access, change the parent, or request elevation.

    Call this only for newly rendered or explicitly selected existing output
    files. Non-Windows permissions are left untouched. Failures are reported to
    the caller rather than silently returning an inaccessible export.
    """
    selected = Path(path)
    if selected.is_symlink():
        raise ValueError(f"不能通过符号链接修改输出文件权限: {selected}")
    target = selected.resolve(strict=True)
    if not target.is_file():
        raise ValueError(f"只能恢复单个输出文件的权限: {target}")
    if sys.platform != "win32":
        return target

    # Use the Windows system utility directly; paths stay one literal argument
    # even when they contain spaces, Chinese characters, or shell metacharacters.
    utility = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    command = [str(utility), str(target), "/reset", "/q"]
    try:
        result = subprocess.run(
            command, shell=False, capture_output=True, text=True, errors="replace",
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FileAccessError(f"无法恢复输出文件的继承权限: {target} ({exc})") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit={result.returncode}").strip()
        raise FileAccessError(f"无法恢复输出文件的继承权限: {target} ({detail[:1000]})")
    return target
