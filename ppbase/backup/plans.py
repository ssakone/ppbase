"""Filesystem control-plane store for immutable restore staging plans."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from ppbase.backup.control import (
    ControlPlaneRoot,
    ControlPlaneSafetyError,
    open_flags as _open_flags,
    open_private_directory_at as _control_open_private_directory_at,
    same_file_identity as _same_file_identity,
    validate_entry_name as _control_validate_entry_name,
    verify_directory_attached_at as _control_verify_directory_attached_at,
)
from ppbase.backup.models import (
    BackupManifestError,
    canonical_json_bytes,
    validate_backup_id,
)


_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_PLAN_DOMAIN = b"PPBASE-RESTORE-STAGING-PLAN-V1\0"
_TERMINAL_STATUS_FILENAME = "terminal.json"
_TERMINAL_TEMP_PREFIX = ".terminal-"
_TERMINAL_TEMP_SUFFIX = ".tmp"
_ABANDONED_FILENAME = "ABANDONED"
_ABANDONED_TEMP_PREFIX = ".abandoned-"
_MAX_TERMINAL_TEMP_FILES = 32
_MAX_PLAN_JSON_BYTES = 64 * 1024
JwtSecretMode = Literal["disaster_recovery", "clone"]


class StagingPlanError(RuntimeError):
    """Raised for unsafe, missing, or non-executable staging plans."""


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return canonical_json_bytes(value)
    except BackupManifestError as exc:
        raise StagingPlanError(str(exc)) from exc


def _validate_entry_name(name: str) -> None:
    try:
        _control_validate_entry_name(name)
    except ControlPlaneSafetyError as exc:
        raise StagingPlanError(str(exc)) from exc


def _open_private_directory_at(
    parent_fd: int,
    name: str,
    *,
    label: str,
    create_missing: bool,
    exclusive: bool = False,
) -> int:
    try:
        return _control_open_private_directory_at(
            parent_fd,
            name,
            label=label,
            create_missing=create_missing,
            exclusive=exclusive,
        )
    except ControlPlaneSafetyError as exc:
        raise StagingPlanError(str(exc)) from exc


def _verify_directory_attached_at(
    parent_fd: int,
    name: str,
    directory_fd: int,
    *,
    label: str,
) -> None:
    try:
        _control_verify_directory_attached_at(
            parent_fd,
            name,
            directory_fd,
            label=label,
        )
    except ControlPlaneSafetyError as exc:
        raise StagingPlanError(str(exc)) from exc


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise StagingPlanError("Short write while persisting staging state.")
        remaining = remaining[written:]


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _write_exclusive_at(
    parent_fd: int,
    name: str,
    payload: bytes,
    *,
    keep_open: bool = False,
) -> int | None:
    _validate_entry_name(name)
    descriptor: int | None = os.open(
        name,
        _open_flags(
            (os.O_RDWR if keep_open else os.O_WRONLY)
            | os.O_CREAT
            | os.O_EXCL
        ),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise StagingPlanError("The staging plan file is unsafe.")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        if keep_open:
            result = descriptor
            descriptor = None
            return result
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _remove_owned_regular_file_at(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
) -> None:
    try:
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(visible.st_mode)
        or not _same_file_identity(visible, expected)
    ):
        raise StagingPlanError(
            "The staging terminal file changed before rollback."
        )
    os.unlink(name, dir_fd=parent_fd)
    _fsync_directory(parent_fd)


def _cleanup_terminal_temporaries_at(parent_fd: int) -> int:
    try:
        names: list[str] = []
        with os.scandir(parent_fd) as entries:
            for entry in entries:
                if not (
                    entry.name.startswith(_TERMINAL_TEMP_PREFIX)
                    and entry.name.endswith(_TERMINAL_TEMP_SUFFIX)
                ):
                    continue
                names.append(entry.name)
                if len(names) >= _MAX_TERMINAL_TEMP_FILES:
                    break
    except OSError as exc:
        raise StagingPlanError(
            "Terminal status temporaries cannot be inspected safely."
        ) from exc

    removed_count = 0
    for name in names:
        _validate_entry_name(name)
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink not in {1, 2}
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise StagingPlanError(
                "A terminal status temporary is unsafe."
            )
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            continue
        removed_count += 1
    if removed_count:
        _fsync_directory(parent_fd)
    return removed_count


def _publish_terminal_at(
    parent_fd: int,
    payload: bytes,
    *,
    pre_commit_guard: Callable[[], None] | None = None,
) -> None:
    """Publish a complete terminal JSON atomically without replacement."""
    if len(payload) > _MAX_PLAN_JSON_BYTES:
        raise StagingPlanError("Staging plan JSON exceeds its size limit.")

    temporary_name = (
        f"{_TERMINAL_TEMP_PREFIX}{secrets.token_hex(8)}"
        f"{_TERMINAL_TEMP_SUFFIX}"
    )
    temporary_exists = False
    terminal_linked = False
    expected: os.stat_result | None = None
    try:
        _write_exclusive_at(parent_fd, temporary_name, payload)
        temporary_exists = True
        expected = os.stat(
            temporary_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(expected.st_mode)
            or expected.st_uid != os.geteuid()
            or expected.st_nlink != 1
            or stat.S_IMODE(expected.st_mode) != 0o600
        ):
            raise StagingPlanError("The staging terminal file is unsafe.")
        _fsync_directory(parent_fd)
        if pre_commit_guard is not None:
            pre_commit_guard()
        os.link(
            temporary_name,
            _TERMINAL_STATUS_FILENAME,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        terminal_linked = True
        published = os.stat(
            _TERMINAL_STATUS_FILENAME,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if not _same_file_identity(expected, published):
            raise StagingPlanError(
                "The staging terminal file changed during publication."
            )
        os.unlink(temporary_name, dir_fd=parent_fd)
        temporary_exists = False
        _fsync_directory(parent_fd)
    except BaseException as exc:
        cleanup_failed = False
        if terminal_linked and expected is not None:
            try:
                _remove_owned_regular_file_at(
                    parent_fd,
                    _TERMINAL_STATUS_FILENAME,
                    expected,
                )
            except BaseException:
                cleanup_failed = True
        if temporary_exists and expected is not None:
            try:
                _remove_owned_regular_file_at(
                    parent_fd,
                    temporary_name,
                    expected,
                )
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            raise StagingPlanError(
                "Terminal status publication failed and could not be "
                "rolled back safely."
            ) from exc
        raise

    # Commit point: terminal.json and removal of its publication temporary are
    # both durable. Cleanup must never re-enter the rollback path from here.
    try:
        _cleanup_terminal_temporaries_at(parent_fd)
    except (OSError, StagingPlanError):
        pass


def _publish_plan_seal_at(
    parent_fd: int,
    *,
    pre_commit_guard: Callable[[], None] | None = None,
) -> None:
    """Publish the immutable plan seal after its commit guard passes."""
    temporary_name = f".SEALED-{secrets.token_hex(8)}.tmp"
    temporary_exists = False
    seal_linked = False
    expected: os.stat_result | None = None
    try:
        _write_exclusive_at(parent_fd, temporary_name, b"sealed\n")
        temporary_exists = True
        expected = os.stat(
            temporary_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(expected.st_mode)
            or expected.st_uid != os.geteuid()
            or expected.st_nlink != 1
            or stat.S_IMODE(expected.st_mode) != 0o600
        ):
            raise StagingPlanError("The staging plan seal is unsafe.")
        _fsync_directory(parent_fd)
        if pre_commit_guard is not None:
            pre_commit_guard()
        os.link(
            temporary_name,
            "SEALED",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        seal_linked = True
        published = os.stat("SEALED", dir_fd=parent_fd, follow_symlinks=False)
        if not _same_file_identity(expected, published):
            raise StagingPlanError(
                "The staging plan seal changed during publication."
            )
        os.unlink(temporary_name, dir_fd=parent_fd)
        temporary_exists = False
        _fsync_directory(parent_fd)
    except BaseException as exc:
        cleanup_failed = False
        if seal_linked and expected is not None:
            try:
                _remove_owned_regular_file_at(parent_fd, "SEALED", expected)
            except BaseException:
                cleanup_failed = True
        if temporary_exists and expected is not None:
            try:
                _remove_owned_regular_file_at(
                    parent_fd,
                    temporary_name,
                    expected,
                )
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            raise StagingPlanError(
                "Staging plan seal publication failed and could not be "
                "rolled back safely."
            ) from exc
        raise


def _publish_abandoned_at(
    parent_fd: int,
    payload: bytes,
    *,
    pre_commit_guard: Callable[[], None] | None = None,
) -> None:
    """Publish one immutable abandonment marker without replacement."""
    if len(payload) > _MAX_PLAN_JSON_BYTES:
        raise StagingPlanError("Staging plan JSON exceeds its size limit.")
    temporary_name = f"{_ABANDONED_TEMP_PREFIX}{secrets.token_hex(8)}.tmp"
    temporary_exists = False
    marker_linked = False
    expected: os.stat_result | None = None
    try:
        _write_exclusive_at(parent_fd, temporary_name, payload)
        temporary_exists = True
        expected = os.stat(
            temporary_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(expected.st_mode)
            or expected.st_uid != os.geteuid()
            or expected.st_nlink != 1
            or stat.S_IMODE(expected.st_mode) != 0o600
        ):
            raise StagingPlanError("The staging abandonment marker is unsafe.")
        _fsync_directory(parent_fd)
        if pre_commit_guard is not None:
            pre_commit_guard()
        os.link(
            temporary_name,
            _ABANDONED_FILENAME,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
        marker_linked = True
        published = os.stat(
            _ABANDONED_FILENAME,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if not _same_file_identity(expected, published):
            raise StagingPlanError(
                "The staging abandonment marker changed during publication."
            )
        os.unlink(temporary_name, dir_fd=parent_fd)
        temporary_exists = False
        _fsync_directory(parent_fd)
    except BaseException as exc:
        cleanup_failed = False
        if marker_linked and expected is not None:
            try:
                _remove_owned_regular_file_at(
                    parent_fd,
                    _ABANDONED_FILENAME,
                    expected,
                )
            except BaseException:
                cleanup_failed = True
        if temporary_exists and expected is not None:
            try:
                _remove_owned_regular_file_at(
                    parent_fd,
                    temporary_name,
                    expected,
                )
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            raise StagingPlanError(
                "Staging plan abandonment failed and could not be rolled back safely."
            ) from exc
        raise


def _read_json_at(
    parent_fd: int,
    name: str,
    *,
    allowed_link_counts: tuple[int, ...] = (1,),
    require_canonical: bool = False,
) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise StagingPlanError(f"Duplicate JSON key {key!r}.")
            result[key] = value
        return result

    descriptor: int | None = None
    try:
        _validate_entry_name(name)
        descriptor = os.open(
            name,
            _open_flags(os.O_RDONLY),
            dir_fd=parent_fd,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink not in allowed_link_counts
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise StagingPlanError("Staging plan JSON is not a private regular file.")
        if info.st_size > _MAX_PLAN_JSON_BYTES:
            raise StagingPlanError("Staging plan JSON exceeds its size limit.")
        chunks: list[bytes] = []
        remaining = _MAX_PLAN_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_uid != os.geteuid()
            or after.st_nlink not in allowed_link_counts
            or stat.S_IMODE(after.st_mode) != 0o600
            or not _same_file_identity(info, after)
            or not _same_file_identity(after, visible)
        ):
            raise StagingPlanError(
                "Staging plan JSON changed while it was read."
            )
        raw = b"".join(chunks)
        if len(raw) > _MAX_PLAN_JSON_BYTES:
            raise StagingPlanError("Staging plan JSON exceeds its size limit.")
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate,
            parse_float=lambda _value: (_ for _ in ()).throw(
                StagingPlanError("Floating-point plan values are forbidden.")
            ),
        )
    except StagingPlanError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise StagingPlanError("Staging plan JSON is unreadable.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise StagingPlanError("Staging plan JSON must be an object.")
    if require_canonical and raw != _canonical_json_bytes(value):
        raise StagingPlanError("Staging plan JSON is not canonical.")
    return value


def _path_exists_at(parent_fd: int, name: str) -> bool:
    _validate_entry_name(name)
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StagingPlanError("The staging plan entry cannot be inspected.") from exc
    return True


def _is_private_regular_file_at(parent_fd: int, name: str) -> bool:
    _validate_entry_name(name)
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and info.st_nlink == 1
        and stat.S_IMODE(info.st_mode) == 0o600
    )


@dataclass(frozen=True, slots=True)
class StagingPlan:
    plan_id: str
    backup_id: str
    manifest_sha256: str
    destination_fingerprint_sha256: str
    target_database: str
    target_data_dir: str
    jwt_secret_mode: JwtSecretMode
    created_at: str
    actor_id: str | None
    plan_hash: str
    status: str
    status_data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.status_data,
            "id": self.plan_id,
            "backupId": self.backup_id,
            "manifestSha256": self.manifest_sha256,
            "destinationFingerprintSha256": self.destination_fingerprint_sha256,
            "targetDatabase": self.target_database,
            "targetDataDir": self.target_data_dir,
            "jwtSecretMode": self.jwt_secret_mode,
            "createdAt": self.created_at,
            "actorId": self.actor_id,
            "planHash": self.plan_hash,
            "status": self.status,
        }


@dataclass(slots=True)
class _ExecutionLease:
    attempt_id: str
    lease_fd: int
    plan_fd: int


class StagingPlanStore:
    """Persist immutable plans and mutable execution status outside the DB."""

    def __init__(
        self,
        control_dir: str | Path | ControlPlaneRoot,
        staging_root: str | Path,
        target_root: str | Path | None = None,
    ):
        if isinstance(control_dir, ControlPlaneRoot):
            self._control_root = control_dir
            self._owns_control_root = False
        else:
            try:
                self._control_root = ControlPlaneRoot.open(control_dir)
            except ControlPlaneSafetyError as exc:
                raise StagingPlanError(str(exc)) from exc
            self._owns_control_root = True
        self.control_dir = self._control_root.path
        self.staging_root = Path(staging_root).expanduser().resolve(strict=False)
        self.target_root = (
            Path(target_root).expanduser().resolve(strict=False)
            if target_root is not None
            else self.staging_root
        )
        self.plans_dir = self.control_dir / "plans"
        self._execution_leases: dict[str, _ExecutionLease] = {}

        try:
            plans_fd = self._control_root.open_private_directory(
                "plans",
                label="The backup plans directory",
                create_missing=True,
            )
            os.close(plans_fd)
        except ControlPlaneSafetyError as exc:
            if self._owns_control_root:
                self._control_root.close()
            raise StagingPlanError(str(exc)) from exc
        except BaseException:
            if self._owns_control_root:
                self._control_root.close()
            raise

    @contextmanager
    def _open_plans_directory(self) -> Iterator[int]:
        plans_fd: int | None = None
        try:
            try:
                plans_fd = self._control_root.open_private_directory(
                    "plans",
                    label="The backup plans directory",
                    create_missing=False,
                )
            except ControlPlaneSafetyError as exc:
                raise StagingPlanError(str(exc)) from exc
            self._verify_plans_attachment(plans_fd)
            yield plans_fd
        finally:
            if plans_fd is not None:
                try:
                    os.close(plans_fd)
                except OSError:
                    pass

    def _verify_plans_attachment(self, plans_fd: int) -> None:
        try:
            self._control_root.verify_attached()
        except ControlPlaneSafetyError as exc:
            raise StagingPlanError(str(exc)) from exc
        _verify_directory_attached_at(
            self._control_root.fileno(),
            "plans",
            plans_fd,
            label="The backup plans directory",
        )
        try:
            self._control_root.verify_attached()
        except ControlPlaneSafetyError as exc:
            raise StagingPlanError(str(exc)) from exc

    def _verify_plan_attachment_chain(
        self,
        plans_fd: int,
        plan_id: str,
        plan_fd: int,
    ) -> None:
        self._verify_plans_attachment(plans_fd)
        _verify_directory_attached_at(
            plans_fd,
            plan_id,
            plan_fd,
            label="Staging plan",
        )
        self._verify_plans_attachment(plans_fd)

    def close(self) -> None:
        for plan_id, owned in list(
            getattr(self, "_execution_leases", {}).items()
        ):
            self._release_execution_lease(plan_id, owned.attempt_id)
        if getattr(self, "_owns_control_root", False):
            self._control_root.close()

    def __del__(self) -> None:
        self.close()

    @staticmethod
    def _open_plan_directory(plans_fd: int, plan_id: str) -> int:
        if not _ID_RE.fullmatch(str(plan_id)):
            raise StagingPlanError("Invalid staging plan ID.")
        try:
            return _open_private_directory_at(
                plans_fd,
                plan_id,
                label="Staging plan",
                create_missing=False,
            )
        except StagingPlanError as exc:
            raise StagingPlanError("Staging plan was not found or is unsafe.") from exc

    def create(
        self,
        *,
        backup_id: str,
        manifest_sha256: str,
        destination_fingerprint_sha256: str,
        jwt_secret_mode: str,
        actor_id: str | None,
        pre_commit_guard: Callable[[], None] | None = None,
    ) -> StagingPlan:
        if jwt_secret_mode not in {"disaster_recovery", "clone"}:
            raise StagingPlanError(
                "jwtSecretMode must be disaster_recovery or clone."
            )
        if not re.fullmatch(r"[a-f0-9]{64}", manifest_sha256):
            raise StagingPlanError("The manifest SHA-256 is invalid.")
        if not re.fullmatch(r"[a-f0-9]{64}", destination_fingerprint_sha256):
            raise StagingPlanError("The destination fingerprint SHA-256 is invalid.")
        try:
            validated_backup_id = validate_backup_id(backup_id)
        except BackupManifestError as exc:
            raise StagingPlanError("The backup ID is invalid.") from exc
        if actor_id is not None and not isinstance(actor_id, str):
            raise StagingPlanError("The actor ID is invalid.")

        plan_id = secrets.token_hex(16)
        target_database = f"ppbase_stage_{plan_id[:24]}"
        format_version = 2 if self.target_root != self.staging_root else 1
        target_data_dir = self.target_root / plan_id / "data"
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        immutable = {
            "formatVersion": format_version,
            "id": plan_id,
            "backupId": validated_backup_id,
            "manifestSha256": manifest_sha256,
            "destinationFingerprintSha256": destination_fingerprint_sha256,
            "targetDatabase": target_database,
            "targetDataDir": str(target_data_dir),
            "jwtSecretMode": jwt_secret_mode,
            "createdAt": created_at,
            "actorId": actor_id,
        }
        plan_hash = hashlib.sha256(
            _PLAN_DOMAIN + _canonical_json_bytes(immutable)
        ).hexdigest()
        payload = {**immutable, "planHash": plan_hash}

        with self._open_plans_directory() as plans_fd:
            plan_fd = _open_private_directory_at(
                plans_fd,
                plan_id,
                label="Staging plan",
                create_missing=True,
                exclusive=True,
            )
            try:
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    plan_fd,
                )
                _write_exclusive_at(
                    plan_fd,
                    "plan.json",
                    _canonical_json_bytes(payload),
                )
                _write_exclusive_at(
                    plan_fd,
                    "status.json",
                    _canonical_json_bytes({"status": "planned"}),
                )
                def plan_seal_commit_guard() -> None:
                    self._verify_plan_attachment_chain(
                        plans_fd,
                        plan_id,
                        plan_fd,
                    )
                    if pre_commit_guard is not None:
                        pre_commit_guard()
                    self._verify_plan_attachment_chain(
                        plans_fd,
                        plan_id,
                        plan_fd,
                    )

                _publish_plan_seal_at(
                    plan_fd,
                    pre_commit_guard=plan_seal_commit_guard,
                )
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    plan_fd,
                )
                _fsync_directory(plans_fd)
            except BaseException:
                # A failed unpublished plan is never returned or reused. It is
                # safe to leave forensic partial files under its random ID.
                raise
            finally:
                os.close(plan_fd)
        return self.inspect(plan_id)

    def inspect(self, plan_id: str) -> StagingPlan:
        with self._open_plans_directory() as plans_fd:
            plan_fd = self._open_plan_directory(plans_fd, plan_id)
            try:
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    plan_fd,
                )
                result = self._inspect_open_plan(plan_id, plan_fd)
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    plan_fd,
                )
                return result
            finally:
                os.close(plan_fd)

    def validated_references(self, backup_id: str) -> tuple[StagingPlan, ...]:
        """Return sealed validated plans that still depend on one backup."""
        try:
            selected_backup_id = validate_backup_id(backup_id)
        except BackupManifestError as exc:
            raise StagingPlanError("The backup ID is invalid.") from exc

        references: list[StagingPlan] = []
        with self._open_plans_directory() as plans_fd:
            try:
                with os.scandir(plans_fd) as entries:
                    names = sorted(
                        entry.name
                        for entry in entries
                        if _ID_RE.fullmatch(entry.name)
                    )
            except OSError as exc:
                raise StagingPlanError(
                    "Staging plans cannot be enumerated safely."
                ) from exc

            for plan_id in names:
                plan_fd = self._open_plan_directory(plans_fd, plan_id)
                try:
                    self._verify_plan_attachment_chain(
                        plans_fd,
                        plan_id,
                        plan_fd,
                    )
                    # Failed creation may deliberately leave a forensic random
                    # directory. Only a published SEALED plan is authoritative.
                    if not _is_private_regular_file_at(plan_fd, "SEALED"):
                        continue
                    plan = self._inspect_open_plan(plan_id, plan_fd)
                    self._verify_plan_attachment_chain(
                        plans_fd,
                        plan_id,
                        plan_fd,
                    )
                    if (
                        plan.backup_id == selected_backup_id
                        and plan.status == "validated"
                    ):
                        references.append(plan)
                finally:
                    os.close(plan_fd)
            self._verify_plans_attachment(plans_fd)
        return tuple(references)

    def _inspect_open_plan(self, plan_id: str, plan_fd: int) -> StagingPlan:
        if not _is_private_regular_file_at(plan_fd, "SEALED"):
            raise StagingPlanError("The staging plan is not sealed.")
        payload = _read_json_at(plan_fd, "plan.json")
        terminal_exists = _path_exists_at(plan_fd, _TERMINAL_STATUS_FILENAME)
        running_exists = _path_exists_at(plan_fd, "RUNNING")
        abandoned_exists = _path_exists_at(plan_fd, _ABANDONED_FILENAME)
        expected_fields = {
            "formatVersion",
            "id",
            "backupId",
            "manifestSha256",
            "destinationFingerprintSha256",
            "targetDatabase",
            "targetDataDir",
            "jwtSecretMode",
            "createdAt",
            "actorId",
            "planHash",
        }
        if set(payload) != expected_fields:
            raise StagingPlanError("The staging plan has unknown or missing fields.")
        if not isinstance(payload.get("planHash"), str) or not (
            _SHA256_RE.fullmatch(payload["planHash"])
        ):
            raise StagingPlanError("The staging plan hash is invalid.")
        declared_hash = payload["planHash"]
        immutable = dict(payload)
        immutable.pop("planHash", None)
        actual_hash = hashlib.sha256(
            _PLAN_DOMAIN + _canonical_json_bytes(immutable)
        ).hexdigest()
        if declared_hash != actual_hash:
            raise StagingPlanError("The staging plan hash is invalid.")
        running_marker: dict[str, Any] | None = None
        if running_exists:
            running_marker = _read_json_at(plan_fd, "RUNNING")
            self._validate_running_marker(running_marker, declared_hash)
            reconciled_terminal, owner_active = self._reconcile_orphaned_execution(
                plan_id,
                plan_fd,
                running_marker,
                payload,
                terminal_exists=terminal_exists,
            )
            if owner_active:
                # A terminal hard-link can be visible briefly before its
                # directory fsync commits. The owner lease is the commit gate.
                terminal_exists = False
            elif reconciled_terminal:
                terminal_exists = _path_exists_at(
                    plan_fd,
                    _TERMINAL_STATUS_FILENAME,
                )
        format_version = payload.get("formatVersion")
        if (
            isinstance(format_version, bool)
            or format_version not in {1, 2}
            or payload.get("id") != plan_id
        ):
            raise StagingPlanError("The staging plan identity is invalid.")
        if not isinstance(payload.get("backupId"), str):
            raise StagingPlanError("The staging backup ID is invalid.")
        try:
            validate_backup_id(payload["backupId"])
        except BackupManifestError as exc:
            raise StagingPlanError("The staging backup ID is invalid.") from exc
        if not isinstance(payload.get("manifestSha256"), str) or not (
            _SHA256_RE.fullmatch(payload["manifestSha256"])
        ):
            raise StagingPlanError("The staging manifest SHA-256 is invalid.")
        if not isinstance(payload.get("destinationFingerprintSha256"), str) or not (
            _SHA256_RE.fullmatch(payload["destinationFingerprintSha256"])
        ):
            raise StagingPlanError("The staging destination fingerprint is invalid.")
        expected_database = f"ppbase_stage_{plan_id[:24]}"
        expected_data_dir = (
            self.staging_root if format_version == 1 else self.target_root
        ) / plan_id / "data"
        if payload.get("targetDatabase") != expected_database:
            raise StagingPlanError("The staging database target is not derived from its ID.")
        if Path(str(payload.get("targetDataDir", ""))) != expected_data_dir:
            raise StagingPlanError("The staging data target is not derived from its ID.")
        if payload.get("jwtSecretMode") not in {"disaster_recovery", "clone"}:
            raise StagingPlanError("The staging JWT-secret mode is invalid.")
        if not isinstance(payload.get("createdAt"), str) or not str(
            payload["createdAt"]
        ).endswith("Z"):
            raise StagingPlanError("The staging creation timestamp is invalid.")
        if payload.get("actorId") is not None and not isinstance(
            payload.get("actorId"), str
        ):
            raise StagingPlanError("The staging actor ID is invalid.")
        if terminal_exists:
            status = _read_json_at(
                plan_fd,
                _TERMINAL_STATUS_FILENAME,
                allowed_link_counts=(1, 2),
            )
        elif running_exists:
            status = {
                "status": "running",
                "attemptId": str((running_marker or {}).get("attemptId", "")),
                "startedAt": str((running_marker or {}).get("startedAt", "")),
            }
        else:
            status = _read_json_at(plan_fd, "status.json")
        if not isinstance(status.get("status"), str):
            raise StagingPlanError("The staging status is invalid.")
        status_value = str(status["status"])
        allowed_statuses = {"planned", "running", "validated", "failed", "quarantined"}
        if status_value not in allowed_statuses:
            raise StagingPlanError("The staging status is invalid.")
        if terminal_exists and status_value in {"planned", "running"}:
            raise StagingPlanError("The terminal staging status is invalid.")
        if terminal_exists and not running_exists:
            raise StagingPlanError(
                "A terminal staging status requires its running marker."
            )
        if not terminal_exists and status_value not in {
            "planned",
            "running",
        }:
            raise StagingPlanError("A terminal staging status is not sealed.")
        if running_exists and status_value == "planned":
            raise StagingPlanError("A running staging plan cannot appear planned.")
        if not running_exists and not terminal_exists and status_value != "planned":
            raise StagingPlanError("An unstarted staging plan must remain planned.")

        if abandoned_exists:
            abandoned = _read_json_at(
                plan_fd,
                _ABANDONED_FILENAME,
                require_canonical=True,
            )
            if set(abandoned) != {
                "planHash",
                "previousStatus",
                "abandonedAt",
                "actorId",
            }:
                raise StagingPlanError(
                    "The staging abandonment marker has invalid fields."
                )
            if abandoned.get("planHash") != declared_hash:
                raise StagingPlanError(
                    "The staging abandonment marker does not match the plan."
                )
            if abandoned.get("previousStatus") != status_value:
                raise StagingPlanError(
                    "The staging abandonment marker has an invalid prior status."
                )
            if status_value == "running":
                raise StagingPlanError(
                    "A running staging plan cannot be abandoned."
                )
            if not isinstance(abandoned.get("abandonedAt"), str) or not str(
                abandoned["abandonedAt"]
            ).endswith("Z"):
                raise StagingPlanError(
                    "The staging abandonment timestamp is invalid."
                )
            if abandoned.get("actorId") is not None and not isinstance(
                abandoned.get("actorId"),
                str,
            ):
                raise StagingPlanError(
                    "The staging abandonment actor is invalid."
                )
            status_value = "abandoned"
            status = abandoned

        return StagingPlan(
            plan_id=str(payload.get("id", "")),
            backup_id=str(payload.get("backupId", "")),
            manifest_sha256=str(payload.get("manifestSha256", "")),
            destination_fingerprint_sha256=str(
                payload.get("destinationFingerprintSha256", "")
            ),
            target_database=str(payload.get("targetDatabase", "")),
            target_data_dir=str(expected_data_dir),
            jwt_secret_mode=str(payload.get("jwtSecretMode", "")),  # type: ignore[arg-type]
            created_at=str(payload.get("createdAt", "")),
            actor_id=(
                str(payload["actorId"])
                if payload.get("actorId") is not None
                else None
            ),
            plan_hash=declared_hash,
            status=status_value,
            status_data={key: value for key, value in status.items() if key != "status"},
        )

    def abandon(
        self,
        plan_id: str,
        *,
        expected_plan_hash: str,
        actor_id: str | None,
        pre_commit_guard: Callable[[], None] | None = None,
    ) -> StagingPlan:
        """Durably make a non-running plan non-activatable and releasable."""
        if not _SHA256_RE.fullmatch(str(expected_plan_hash or "")):
            raise StagingPlanError("The supplied planHash is invalid.")
        if actor_id is not None and not isinstance(actor_id, str):
            raise StagingPlanError("The actor ID is invalid.")

        with self._open_plans_directory() as plans_fd:
            plan_fd = self._open_plan_directory(plans_fd, plan_id)
            try:
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    plan_fd,
                )
                plan = self._inspect_open_plan(plan_id, plan_fd)
                if plan.plan_hash != expected_plan_hash:
                    raise StagingPlanError(
                        "The supplied planHash does not match the plan."
                    )
                if plan.status == "abandoned":
                    return plan
                if plan.status == "running":
                    raise StagingPlanError(
                        "A running staging plan cannot be abandoned."
                    )
                payload = _canonical_json_bytes(
                    {
                        "planHash": plan.plan_hash,
                        "previousStatus": plan.status,
                        "abandonedAt": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "actorId": actor_id,
                    }
                )

                def abandonment_commit_guard() -> None:
                    self._verify_plan_attachment_chain(
                        plans_fd,
                        plan_id,
                        plan_fd,
                    )
                    if pre_commit_guard is not None:
                        pre_commit_guard()
                    self._verify_plan_attachment_chain(
                        plans_fd,
                        plan_id,
                        plan_fd,
                    )

                try:
                    _publish_abandoned_at(
                        plan_fd,
                        payload,
                        pre_commit_guard=abandonment_commit_guard,
                    )
                except FileExistsError:
                    pass
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    plan_fd,
                )
                result = self._inspect_open_plan(plan_id, plan_fd)
                if result.status != "abandoned":
                    raise StagingPlanError(
                        "The staging plan abandonment did not reach its commit point."
                    )
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    plan_fd,
                )
                return result
            finally:
                os.close(plan_fd)

    def begin_execution(self, plan_id: str, *, expected_plan_hash: str) -> StagingPlan:
        attempt_id = secrets.token_hex(16)
        lease_name = f"attempt-{attempt_id}.lease"
        plan_fd: int | None = None
        lease_fd: int | None = None
        lease_registered = False
        try:
            with self._open_plans_directory() as plans_fd:
                plan_fd = self._open_plan_directory(plans_fd, plan_id)
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    plan_fd,
                )
                plan = self._inspect_open_plan(plan_id, plan_fd)
                if plan.plan_hash != expected_plan_hash:
                    raise StagingPlanError(
                        "The supplied planHash does not match the plan."
                    )
                if plan.status != "planned":
                    raise StagingPlanError(
                        "Staging plan is not executable from status "
                        f"{plan.status!r}."
                    )

                lease_fd = _write_exclusive_at(
                    plan_fd,
                    lease_name,
                    b"lease\n",
                    keep_open=True,
                )
                if lease_fd is None:
                    raise StagingPlanError(
                        "The staging execution lease could not be retained."
                    )
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lease = _ExecutionLease(
                    attempt_id=attempt_id,
                    lease_fd=lease_fd,
                    plan_fd=plan_fd,
                )
                self._execution_leases[plan_id] = lease
                lease_registered = True
                lease_fd = None
                plan_fd = None

                _write_exclusive_at(
                    lease.plan_fd,
                    "RUNNING",
                    _canonical_json_bytes(
                        {
                            "planHash": plan.plan_hash,
                            "attemptId": attempt_id,
                            "ownerPid": os.getpid(),
                            "startedAt": datetime.now(timezone.utc)
                            .isoformat()
                            .replace("+00:00", "Z"),
                            "leaseFile": lease_name,
                        }
                    ),
                )
                _fsync_directory(lease.plan_fd)
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    lease.plan_fd,
                )
                result = self._inspect_open_plan(plan_id, lease.plan_fd)
                self._verify_plan_attachment_chain(
                    plans_fd,
                    plan_id,
                    lease.plan_fd,
                )
                return result
        except FileExistsError as exc:
            if lease_registered:
                self._release_execution_lease(plan_id, attempt_id)
            raise StagingPlanError("The staging plan was already executed.") from exc
        except BaseException:
            if lease_registered:
                self._release_execution_lease(plan_id, attempt_id)
            raise
        finally:
            if lease_fd is not None:
                os.close(lease_fd)
            if plan_fd is not None:
                os.close(plan_fd)

    def finish(
        self,
        plan_id: str,
        *,
        status: Literal["validated", "failed", "quarantined"],
        expected_attempt_id: str,
        data: dict[str, Any] | None = None,
        pre_commit_guard: Callable[[], None] | None = None,
    ) -> StagingPlan:
        if status not in {"validated", "failed", "quarantined"}:
            raise StagingPlanError("Invalid terminal staging status.")
        owned = self._execution_leases.get(plan_id)
        if owned is None:
            current = self.inspect(plan_id)
            if current.status != "running":
                raise StagingPlanError(
                    f"Staging plan cannot finish from status {current.status!r}."
                )
            raise StagingPlanError(
                "The staging execution attempt is not owned by this process."
            )
        if owned.attempt_id != expected_attempt_id:
            raise StagingPlanError(
                "The staging execution attempt is not owned by this process."
            )
        with self._open_plans_directory() as plans_fd:
            self._verify_plan_attachment_chain(
                plans_fd,
                plan_id,
                owned.plan_fd,
            )
            plan = self._inspect_open_plan(plan_id, owned.plan_fd)
            if plan.status != "running":
                raise StagingPlanError(
                    f"Staging plan cannot finish from status {plan.status!r}."
                )
            if not _is_private_regular_file_at(owned.plan_fd, "RUNNING"):
                raise StagingPlanError("The staging plan has not started.")
            running_marker = _read_json_at(owned.plan_fd, "RUNNING")
            self._validate_running_marker(running_marker, plan.plan_hash)
            if running_marker["attemptId"] != expected_attempt_id:
                raise StagingPlanError(
                    "The staging execution attempt does not match."
                )
            terminal_payload = {
                **(data or {}),
                "attemptId": expected_attempt_id,
                "status": status,
            }
            terminal_bytes = _canonical_json_bytes(terminal_payload)
            if len(terminal_bytes) > _MAX_PLAN_JSON_BYTES:
                raise StagingPlanError(
                    "Staging plan JSON exceeds its size limit."
                )
            self._verify_plan_attachment_chain(
                plans_fd,
                plan_id,
                owned.plan_fd,
            )
            try:
                def terminal_commit_guard() -> None:
                    self._verify_plan_attachment_chain(
                        plans_fd,
                        plan_id,
                        owned.plan_fd,
                    )
                    if pre_commit_guard is not None:
                        pre_commit_guard()
                    self._verify_plan_attachment_chain(
                        plans_fd,
                        plan_id,
                        owned.plan_fd,
                    )

                _publish_terminal_at(
                    owned.plan_fd,
                    terminal_bytes,
                    pre_commit_guard=terminal_commit_guard,
                )
            except FileExistsError as exc:
                raise StagingPlanError(
                    "The staging plan already has a terminal status."
                ) from exc
            self._verify_plan_attachment_chain(
                plans_fd,
                plan_id,
                owned.plan_fd,
            )
            persisted_terminal = _read_json_at(
                owned.plan_fd,
                _TERMINAL_STATUS_FILENAME,
                allowed_link_counts=(1,),
                require_canonical=True,
            )
            if (
                persisted_terminal.get("status") != status
                or persisted_terminal.get("attemptId") != expected_attempt_id
                or _canonical_json_bytes(persisted_terminal) != terminal_bytes
            ):
                raise StagingPlanError(
                    "Persisted terminal status does not match the execution."
                )
            result = replace(
                plan,
                status=str(persisted_terminal["status"]),
                status_data={
                    key: value
                    for key, value in persisted_terminal.items()
                    if key != "status"
                },
            )
            self._verify_plan_attachment_chain(
                plans_fd,
                plan_id,
                owned.plan_fd,
            )
        self._release_execution_lease(plan_id, expected_attempt_id)
        return result

    def abandon_execution(self, plan_id: str, *, expected_attempt_id: str) -> None:
        """Release a quiescent attempt whose terminal status could not persist."""
        owned = self._execution_leases.get(plan_id)
        if owned is None or owned.attempt_id != expected_attempt_id:
            raise StagingPlanError(
                "The staging execution attempt is not owned by this process."
            )
        self._release_execution_lease(plan_id, expected_attempt_id)

    def _validate_running_marker(
        self,
        marker: dict[str, Any],
        declared_hash: str,
    ) -> None:
        if set(marker) != {
            "planHash",
            "attemptId",
            "ownerPid",
            "startedAt",
            "leaseFile",
        }:
            raise StagingPlanError("The running marker has invalid fields.")
        attempt_id = marker.get("attemptId")
        owner_pid = marker.get("ownerPid")
        started_at = marker.get("startedAt")
        lease_file = marker.get("leaseFile")
        if (
            marker.get("planHash") != declared_hash
            or not isinstance(attempt_id, str)
            or not _ID_RE.fullmatch(attempt_id)
            or isinstance(owner_pid, bool)
            or not isinstance(owner_pid, int)
            or owner_pid <= 0
            or not isinstance(started_at, str)
            or not started_at.endswith("Z")
            or lease_file != f"attempt-{attempt_id}.lease"
        ):
            raise StagingPlanError(
                "The running marker does not match the sealed plan."
            )

    def _reconcile_orphaned_execution(
        self,
        plan_id: str,
        plan_fd: int,
        marker: dict[str, Any],
        payload: dict[str, Any],
        *,
        terminal_exists: bool,
    ) -> tuple[bool, bool]:
        attempt_id = str(marker["attemptId"])
        owned = self._execution_leases.get(plan_id)
        if owned is not None and owned.attempt_id == attempt_id:
            return False, True

        lease_fd: int | None = None
        try:
            lease_fd = os.open(
                str(marker["leaseFile"]),
                _open_flags(os.O_RDWR),
                dir_fd=plan_fd,
            )
        except FileNotFoundError:
            lease_fd = None
        except OSError as exc:
            raise StagingPlanError(
                "The staging execution lease cannot be opened safely."
            ) from exc

        lock_acquired = False
        try:
            if lease_fd is not None:
                info = os.fstat(lease_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise StagingPlanError(
                        "The staging execution lease is unsafe."
                    )
                try:
                    fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False, True
                lock_acquired = True
            _cleanup_terminal_temporaries_at(plan_fd)
            if terminal_exists:
                return False, False
            try:
                _publish_terminal_at(
                    plan_fd,
                    _canonical_json_bytes(
                        {
                            "status": "quarantined",
                            "attemptId": attempt_id,
                            "failureCode": "staging_owner_lost",
                            "targetDatabase": str(payload["targetDatabase"]),
                            "targetDataDir": str(payload["targetDataDir"]),
                        }
                    ),
                )
            except FileExistsError:
                pass
            return True, False
        finally:
            if lease_fd is not None:
                try:
                    if lock_acquired:
                        fcntl.flock(lease_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lease_fd)

    def _release_execution_lease(self, plan_id: str, attempt_id: str) -> None:
        owned = self._execution_leases.get(plan_id)
        if owned is None or owned.attempt_id != attempt_id:
            return
        owned = self._execution_leases.pop(plan_id)
        try:
            fcntl.flock(owned.lease_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(owned.lease_fd)
        except OSError:
            pass
        try:
            os.close(owned.plan_fd)
        except OSError:
            pass
