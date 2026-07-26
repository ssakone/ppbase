"""Descriptor-anchored primitives for the native-backup control plane."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


class ControlPlaneSafetyError(RuntimeError):
    """Raised when a control-plane path cannot be confined safely."""


def open_flags(base: int) -> int:
    return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def directory_open_flags() -> int:
    return open_flags(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))


def same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def validate_entry_name(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
    ):
        raise ControlPlaneSafetyError("The control-plane entry name is unsafe.")


def absolute_path_without_symlink_resolution(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _require_descriptor_confinement() -> None:
    required = {os.open, os.mkdir, os.stat}
    if (
        getattr(os, "O_NOFOLLOW", 0) == 0
        or getattr(os, "O_DIRECTORY", 0) == 0
        or not hasattr(os, "geteuid")
        or not required.issubset(getattr(os, "supports_dir_fd", set()))
    ):
        raise ControlPlaneSafetyError(
            "The platform cannot safely confine the backup control plane."
        )


def _assert_safe_ancestor(parent_fd: int) -> os.stat_result:
    info = os.fstat(parent_fd)
    if not stat.S_ISDIR(info.st_mode):
        raise ControlPlaneSafetyError(
            "The backup control ancestry contains a non-directory entry."
        )
    return info


def ensure_runtime_backup_roots(settings: object) -> None:
    """Create backup roots with the same policy as the project data directory."""
    for attribute in ("backup_root", "backup_control_dir"):
        configured = Path(getattr(settings, attribute)).expanduser()
        configured.mkdir(parents=True, exist_ok=True)


def _open_control_root(
    path: Path,
    *,
    create_missing: bool,
    require_private_final: bool = True,
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Open a real private control root without following path symlinks."""
    _require_descriptor_confinement()
    if not path.is_absolute():
        raise ControlPlaneSafetyError("The backup control root must be absolute.")

    components = path.parts[1:]
    if not components:
        raise ControlPlaneSafetyError(
            "The backup control root must be a private 0700 directory."
        )

    descriptors = [os.open(os.sep, directory_open_flags())]
    entry_names: list[str] = []
    try:
        for index, component in enumerate(components):
            current_fd = descriptors[-1]
            validate_entry_name(component)
            parent_info = _assert_safe_ancestor(current_fd)
            created = False
            try:
                expected = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as exc:
                if not create_missing:
                    raise ControlPlaneSafetyError(
                        "The backup control root is missing."
                    ) from exc
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    created = True
                except FileExistsError:
                    pass
                expected = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )

            if (
                stat.S_IMODE(parent_info.st_mode) & stat.S_ISVTX
                and expected.st_uid != os.geteuid()
            ):
                raise ControlPlaneSafetyError(
                    "The backup control path below a sticky directory must "
                    "belong to the service user."
                )

            if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(
                expected.st_mode
            ):
                raise ControlPlaneSafetyError(
                    "The backup control path contains an unsafe symlink or entry."
                )

            child_fd: int | None = None
            try:
                child_fd = os.open(
                    component,
                    directory_open_flags(),
                    dir_fd=current_fd,
                )
                opened = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not same_file_identity(expected, opened)
                ):
                    raise ControlPlaneSafetyError(
                        "The backup control path changed while it was opened."
                    )
                if created:
                    os.fchmod(child_fd, 0o700)
                    opened = os.fstat(child_fd)
                    fsync_directory(child_fd)
                    fsync_directory(current_fd)
                if require_private_final and index == len(components) - 1 and (
                    opened.st_uid != os.geteuid()
                    or stat.S_IMODE(opened.st_mode) != 0o700
                ):
                    raise ControlPlaneSafetyError(
                        "The backup control root must be a private 0700 directory."
                    )
            except BaseException:
                if child_fd is not None:
                    os.close(child_fd)
                raise

            descriptors.append(child_fd)
            entry_names.append(component)
            child_fd = None
            if index == len(components) - 1:
                result = (tuple(descriptors), tuple(entry_names))
                descriptors = []
                return result
        raise ControlPlaneSafetyError("The backup control root is invalid.")
    except ControlPlaneSafetyError:
        raise
    except OSError as exc:
        raise ControlPlaneSafetyError(
            "The backup control root cannot be opened safely."
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def open_private_directory_at(
    parent_fd: int,
    name: str,
    *,
    label: str,
    create_missing: bool,
    exclusive: bool = False,
) -> int:
    """Open one owned 0700 child directory without following symlinks."""
    validate_entry_name(name)
    created = False
    if exclusive:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        created = True
    else:
        try:
            expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if not create_missing:
                raise ControlPlaneSafetyError(f"{label} was not found.")
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
        else:
            if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(
                expected.st_mode
            ):
                raise ControlPlaneSafetyError(
                    f"{label} is not a safe private directory."
                )

    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise ControlPlaneSafetyError(
            f"{label} cannot be inspected safely."
        ) from exc
    if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
        raise ControlPlaneSafetyError(
            f"{label} is not a safe private directory."
        )

    descriptor: int | None = None
    try:
        descriptor = os.open(name, directory_open_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not same_file_identity(expected, opened)
        ):
            raise ControlPlaneSafetyError(f"{label} changed while it was opened.")
        if created:
            os.fchmod(descriptor, 0o700)
            opened = os.fstat(descriptor)
            fsync_directory(descriptor)
            fsync_directory(parent_fd)
        if (
            opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise ControlPlaneSafetyError(
                f"{label} must be a private 0700 directory."
            )
        result = descriptor
        descriptor = None
        return result
    except ControlPlaneSafetyError:
        raise
    except OSError as exc:
        raise ControlPlaneSafetyError(f"{label} cannot be opened safely.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def verify_directory_attached_at(
    parent_fd: int,
    name: str,
    directory_fd: int,
    *,
    label: str,
    require_private: bool = True,
) -> None:
    """Ensure a pinned directory is still the named child of its parent."""
    try:
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(directory_fd)
    except OSError as exc:
        raise ControlPlaneSafetyError(f"{label} was detached.") from exc
    if (
        stat.S_ISLNK(visible.st_mode)
        or not stat.S_ISDIR(visible.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or not same_file_identity(visible, opened)
        or (
            require_private
            and (
                opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o700
            )
        )
    ):
        raise ControlPlaneSafetyError(f"{label} was detached or substituted.")


@dataclass(slots=True)
class ControlPlaneRoot:
    """Pinned descriptor for one validated private backup control root."""

    path: Path
    _chain_descriptors: tuple[int, ...]
    _entry_names: tuple[str, ...]
    _require_private: bool = True

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        create_missing: bool = True,
        require_private: bool = True,
    ) -> "ControlPlaneRoot":
        absolute = absolute_path_without_symlink_resolution(path)
        chain_descriptors, entry_names = _open_control_root(
            absolute,
            create_missing=create_missing,
            require_private_final=require_private,
        )
        root = cls(
            path=absolute,
            _chain_descriptors=chain_descriptors,
            _entry_names=entry_names,
            _require_private=require_private,
        )
        try:
            root.verify_attached()
            return root
        except BaseException:
            root.close()
            raise

    def fileno(self) -> int:
        if not self._chain_descriptors:
            raise ControlPlaneSafetyError("The backup control root is closed.")
        return self._chain_descriptors[-1]

    def verify_attached(self) -> None:
        if (
            not self._chain_descriptors
            or len(self._chain_descriptors) != len(self._entry_names) + 1
        ):
            raise ControlPlaneSafetyError("The backup control root is closed.")
        try:
            for index, entry_name in enumerate(self._entry_names):
                parent_fd = self._chain_descriptors[index]
                child_fd = self._chain_descriptors[index + 1]
                _assert_safe_ancestor(parent_fd)
                verify_directory_attached_at(
                    parent_fd,
                    entry_name,
                    child_fd,
                    label=(
                        "The backup control root"
                        if index == len(self._entry_names) - 1
                        else "The backup control ancestry"
                    ),
                    require_private=(
                        self._require_private
                        and index == len(self._entry_names) - 1
                    ),
                )
                _assert_safe_ancestor(parent_fd)
        except ControlPlaneSafetyError:
            raise
        except OSError as exc:
            raise ControlPlaneSafetyError(
                "The backup control root ancestry cannot be verified safely."
            ) from exc

    def open_private_directory(
        self,
        name: str,
        *,
        label: str,
        create_missing: bool,
        exclusive: bool = False,
    ) -> int:
        self.verify_attached()
        descriptor = open_private_directory_at(
            self.fileno(),
            name,
            label=label,
            create_missing=create_missing,
            exclusive=exclusive,
        )
        try:
            self.verify_attached()
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        descriptors = self._chain_descriptors
        self._chain_descriptors = ()
        self._entry_names = ()
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> "ControlPlaneRoot":
        self.verify_attached()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()
