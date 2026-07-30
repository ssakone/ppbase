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


@dataclass(slots=True)
class RuntimeDataRoot:
    """Pinned descriptor for the existing PPBase ``data_dir`` trust boundary."""

    path: Path
    _descriptor: int
    _identity: tuple[int, int]

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        create_missing: bool = True,
    ) -> "RuntimeDataRoot":
        selected = Path(path).expanduser()
        try:
            if create_missing:
                selected.mkdir(parents=True, exist_ok=True)
            resolved = selected.resolve(strict=True)
            visible = resolved.lstat()
            if not stat.S_ISDIR(visible.st_mode) or resolved.is_symlink():
                raise ControlPlaneSafetyError(
                    "The PPBase data directory is not a safe directory."
                )
            descriptor = os.open(resolved, directory_open_flags())
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not same_file_identity(visible, opened)
            ):
                os.close(descriptor)
                raise ControlPlaneSafetyError(
                    "The PPBase data directory changed while it was opened."
                )
        except ControlPlaneSafetyError:
            raise
        except (FileNotFoundError, OSError) as exc:
            raise ControlPlaneSafetyError(
                "The PPBase data directory cannot be opened safely."
            ) from exc
        return cls(
            path=resolved,
            _descriptor=descriptor,
            _identity=(opened.st_dev, opened.st_ino),
        )

    def fileno(self) -> int:
        if self._descriptor < 0:
            raise ControlPlaneSafetyError("The PPBase data directory is closed.")
        return self._descriptor

    def verify_attached(self) -> None:
        descriptor = self.fileno()
        try:
            visible = self.path.lstat()
            opened = os.fstat(descriptor)
        except OSError as exc:
            raise ControlPlaneSafetyError(
                "The PPBase data directory was detached."
            ) from exc
        if (
            self.path.is_symlink()
            or not stat.S_ISDIR(visible.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or not same_file_identity(visible, opened)
            or (opened.st_dev, opened.st_ino) != self._identity
        ):
            raise ControlPlaneSafetyError(
                "The PPBase data directory was detached or substituted."
            )

    def open_child_directory(
        self,
        name: str,
        *,
        create_missing: bool,
    ) -> int:
        """Open one real child directory without imposing a new mode policy."""
        self.verify_attached()
        validate_entry_name(name)
        parent_fd = self.fileno()
        parent_info = os.fstat(parent_fd)
        created = False
        try:
            try:
                expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if not create_missing:
                    raise ControlPlaneSafetyError(
                        f"The PPBase data directory entry {name!r} is missing."
                    )
                try:
                    os.mkdir(
                        name,
                        mode=stat.S_IMODE(parent_info.st_mode),
                        dir_fd=parent_fd,
                    )
                    created = True
                except FileExistsError:
                    pass
                expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(expected.st_mode) or not stat.S_ISDIR(expected.st_mode):
                raise ControlPlaneSafetyError(
                    f"The PPBase data directory entry {name!r} is unsafe."
                )
            descriptor = os.open(name, directory_open_flags(), dir_fd=parent_fd)
            opened = os.fstat(descriptor)
            if created:
                os.fchmod(descriptor, stat.S_IMODE(parent_info.st_mode))
                os.fsync(descriptor)
                os.fsync(parent_fd)
                opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not same_file_identity(expected, opened)
                or opened.st_uid != parent_info.st_uid
            ):
                os.close(descriptor)
                raise ControlPlaneSafetyError(
                    f"The PPBase data directory entry {name!r} changed while opening."
                )
            self.verify_attached()
            return descriptor
        except ControlPlaneSafetyError:
            raise
        except OSError as exc:
            raise ControlPlaneSafetyError(
                f"The PPBase data directory entry {name!r} cannot be opened safely."
            ) from exc

    def verify_child_directory(self, name: str, descriptor: int) -> None:
        self.verify_attached()
        try:
            visible = os.stat(name, dir_fd=self.fileno(), follow_symlinks=False)
            opened = os.fstat(descriptor)
            parent = os.fstat(self.fileno())
        except OSError as exc:
            raise ControlPlaneSafetyError(
                f"The PPBase data directory entry {name!r} was detached."
            ) from exc
        if (
            stat.S_ISLNK(visible.st_mode)
            or not stat.S_ISDIR(visible.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or not same_file_identity(visible, opened)
            or opened.st_uid != parent.st_uid
        ):
            raise ControlPlaneSafetyError(
                f"The PPBase data directory entry {name!r} was substituted."
            )
        self.verify_attached()

    def close(self) -> None:
        descriptor = self._descriptor
        self._descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)

    def __enter__(self) -> "RuntimeDataRoot":
        self.verify_attached()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass


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
    if info.st_uid not in {0, os.geteuid()}:
        raise ControlPlaneSafetyError(
            "The backup control ancestry must belong to root or the service user."
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise ControlPlaneSafetyError(
            "The backup control ancestry contains a non-sticky writable directory."
        )
    return info


def ensure_runtime_backup_roots(settings: object) -> None:
    """Create only the PocketBase-style ``data_dir/backups`` runtime layout."""
    with RuntimeDataRoot.open(getattr(settings, "data_dir")) as data_root:
        backups_fd = data_root.open_child_directory(
            "backups",
            create_missing=True,
        )
        try:
            data_root.verify_child_directory("backups", backups_fd)
        finally:
            os.close(backups_fd)


@dataclass(frozen=True, slots=True)
class RuntimeDirectoryInspection:
    """Read-only result for one configured runtime backup directory."""

    path: Path
    exists: bool
    private: bool


def inspect_runtime_backup_root(path: str | Path) -> RuntimeDirectoryInspection:
    """Inspect a runtime root through a no-follow descriptor walk."""
    _require_descriptor_confinement()
    if (
        os.access not in getattr(os, "supports_dir_fd", set())
        or os.access not in getattr(os, "supports_effective_ids", set())
    ):
        raise ControlPlaneSafetyError(
            "The platform cannot safely inspect the backup runtime roots."
        )

    absolute = absolute_path_without_symlink_resolution(path)
    components = absolute.parts[1:]
    if not components:
        raise ControlPlaneSafetyError(
            "The backup runtime root must be a private 0700 directory."
        )
    for component in components:
        validate_entry_name(component)

    descriptors = [os.open(os.sep, directory_open_flags())]
    try:
        for index, component in enumerate(components):
            current_fd = descriptors[-1]
            parent_info = _assert_safe_ancestor(current_fd)
            try:
                expected = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not os.access(
                    ".",
                    os.W_OK | os.X_OK,
                    dir_fd=current_fd,
                    effective_ids=True,
                ):
                    raise ControlPlaneSafetyError(
                        "The nearest existing backup root parent is not writable."
                    )
                return RuntimeDirectoryInspection(
                    path=absolute,
                    exists=False,
                    private=False,
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
                    "The backup runtime path contains an unsafe symlink or entry."
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
                        "The backup runtime path changed while it was opened."
                    )
                descriptors.append(child_fd)
                child_fd = None
            finally:
                if child_fd is not None:
                    os.close(child_fd)

            if index == len(components) - 1:
                if opened.st_uid != os.geteuid():
                    raise ControlPlaneSafetyError(
                        "The backup runtime root must belong to the service user."
                    )
                return RuntimeDirectoryInspection(
                    path=absolute,
                    exists=True,
                    private=stat.S_IMODE(opened.st_mode) == 0o700,
                )
        raise ControlPlaneSafetyError("The backup runtime root is invalid.")
    except ControlPlaneSafetyError:
        raise
    except OSError as exc:
        raise ControlPlaneSafetyError(
            "The backup runtime root cannot be inspected safely."
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_control_root(
    path: Path,
    *,
    create_missing: bool,
    require_private_final: bool = True,
    normalize_private_final: bool = False,
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
                if index == len(components) - 1 and normalize_private_final:
                    if opened.st_uid != os.geteuid():
                        raise ControlPlaneSafetyError(
                            "The backup control root must belong to the service user."
                        )
                    if stat.S_IMODE(opened.st_mode) != 0o700:
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
        normalize_private: bool = False,
    ) -> "ControlPlaneRoot":
        absolute = absolute_path_without_symlink_resolution(path)
        chain_descriptors, entry_names = _open_control_root(
            absolute,
            create_missing=create_missing,
            require_private_final=require_private,
            normalize_private_final=normalize_private,
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
