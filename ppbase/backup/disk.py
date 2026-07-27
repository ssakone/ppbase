"""Conservative free-space checks for native backup and restore."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat


class BackupDiskSpaceError(RuntimeError):
    """Raised before work starts when a destination lacks usable free space."""


_MINIMUM_RESERVE_BYTES = 64 * 1024 * 1024


def _existing_ancestor(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def require_disk_space(
    destination: str | Path,
    required_bytes: int,
    *,
    operation: str,
) -> None:
    """Require payload space plus a small filesystem safety reserve."""
    required = max(0, int(required_bytes))
    target = _existing_ancestor(Path(destination))
    usage = shutil.disk_usage(target)
    reserve = max(_MINIMUM_RESERVE_BYTES, required // 10)
    total_required = required + reserve
    if usage.free < total_required:
        raise BackupDiskSpaceError(
            f"insufficient disk space for {operation} at {target}: "
            f"requires at least {total_required} bytes including reserve, "
            f"but only {usage.free} bytes are available"
        )


def local_tree_size(path: str | Path) -> int:
    """Return the size of a regular, non-symlink local tree."""
    root = Path(path)
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return 0
    if root.is_symlink() or not stat.S_ISDIR(root_info.st_mode):
        raise BackupDiskSpaceError(f"backup storage path is not a safe directory: {root}")

    total = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise BackupDiskSpaceError(
                f"backup storage path cannot be measured safely: {directory}"
            ) from exc
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise BackupDiskSpaceError(
                    f"backup storage entry cannot be measured safely: {entry.path}"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise BackupDiskSpaceError(
                    f"backup storage contains a forbidden symlink: {entry.path}"
                )
            if stat.S_ISDIR(info.st_mode):
                stack.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                total += int(info.st_size)
            else:
                raise BackupDiskSpaceError(
                    f"backup storage contains a special file: {entry.path}"
                )
    return total

