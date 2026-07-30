"""Crash-recoverable local-file cutover for destructive native restore."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import Any

from sqlalchemy import text

from ppbase.backup.control import (
    ControlPlaneSafetyError,
    RuntimeDataRoot,
    open_flags,
    same_file_identity,
)
from ppbase.backup.models import canonical_json_bytes
from ppbase.backup.storage import AnchoredStagingDataDir, VerifiedBackupInspection
from ppbase.services.file_references import (
    LocalFileReference,
    read_canonical_local_file_references,
)
from ppbase.services.file_storage import (
    open_file_stream,
    resolve_storage_config_from_settings_payload,
)


_JOURNAL_FILE = ".ppbase-restore.json"
_RESTORE_ROOT = ".ppbase-restore"
_CONTROL_SCHEMA = "_ppbase_restore_control"
_CONTROL_TABLE = "commits"
_RESTORE_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_JOURNAL_STATUSES = {
    "swap_started",
    "files_swapped",
    "database_committed",
    "finalizing",
}
_FILE_REFERENCE_INVENTORY_DOMAIN = b"PPBASE-LOCAL-FILE-REFERENCES-V1\0"
_FILE_REFERENCE_INVENTORY_VERSION = 1
_JOURNAL_FILE_REFERENCE_INVENTORY_KEY = "localFileReferenceInventory"


class DestructiveRestoreError(RuntimeError):
    """Raised when local restore state cannot be changed or recovered safely."""


def normalize_file_reference_inventory(value: Any) -> dict[str, Any]:
    """Return one canonical, structurally valid reference inventory."""
    if not isinstance(value, dict) or set(value) != {"version", "count", "sha256"}:
        raise ValueError("local-file reference inventory has an invalid shape")
    version = value.get("version")
    count = value.get("count")
    sha256 = value.get("sha256")
    if (
        isinstance(version, bool)
        or version != _FILE_REFERENCE_INVENTORY_VERSION
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise ValueError("local-file reference inventory is malformed")
    return {
        "version": _FILE_REFERENCE_INVENTORY_VERSION,
        "count": count,
        "sha256": sha256,
    }


def build_file_reference_inventory(
    references: tuple[LocalFileReference, ...],
) -> dict[str, Any]:
    """Hash the canonical database-to-storage reference set."""
    canonical = tuple(sorted(set(references)))
    digest = hashlib.sha256(_FILE_REFERENCE_INVENTORY_DOMAIN)
    for reference in canonical:
        encoded = canonical_json_bytes(list(reference))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {
        "version": _FILE_REFERENCE_INVENTORY_VERSION,
        "count": len(canonical),
        "sha256": digest.hexdigest(),
    }


def file_reference_inventory_matches(
    expected: dict[str, Any],
    references: tuple[LocalFileReference, ...],
) -> bool:
    """Compare restored references with the verified pre-cutover inventory."""
    try:
        normalized = normalize_file_reference_inventory(expected)
    except ValueError:
        return False
    actual = build_file_reference_inventory(references)
    return normalized["count"] == actual["count"] and secrets.compare_digest(
        str(normalized["sha256"]),
        str(actual["sha256"]),
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = path.lstat()
    except OSError as exc:
        raise DestructiveRestoreError(
            f"restore working directory cannot be created safely: {path}"
        ) from exc
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise DestructiveRestoreError(f"restore working directory is unsafe: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise DestructiveRestoreError(
                f"restore working directory cannot be made private: {path}"
            ) from exc


class DestructiveRestoreJournal:
    """Single durable in-flight restore record directly below ``data_dir``."""

    def __init__(
        self,
        data_root: RuntimeDataRoot,
        *,
        create_missing: bool = True,
    ) -> None:
        del create_missing
        self._root = data_root
        self._closed = False

    def close(self) -> None:
        self._closed = True

    def _require_open(self) -> int:
        if self._closed:
            raise DestructiveRestoreError("destructive restore journal is closed")
        self._root.verify_attached()
        return self._root.fileno()

    @staticmethod
    def _validate_file(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size <= 0
            or info.st_size > 32 * 1024
        ):
            raise DestructiveRestoreError(
                "restore journal has an invalid file shape"
            )

    def read(self) -> dict[str, Any] | None:
        directory_fd = self._require_open()
        try:
            expected = os.stat(
                _JOURNAL_FILE,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            self._validate_file(expected)
            descriptor = os.open(
                _JOURNAL_FILE,
                open_flags(os.O_RDONLY),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DestructiveRestoreError("restore journal cannot be opened safely") from exc
        try:
            info = os.fstat(descriptor)
            self._validate_file(info)
            if not same_file_identity(expected, info):
                raise DestructiveRestoreError(
                    "restore journal changed while it was opened"
                )
            payload = os.read(descriptor, 32 * 1024 + 1)
        finally:
            os.close(descriptor)
        self._root.verify_attached()
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DestructiveRestoreError("restore journal is invalid JSON") from exc
        if not isinstance(value, dict) or value.get("version") != 1:
            raise DestructiveRestoreError("restore journal has an unsupported format")
        return value

    def write(self, value: dict[str, Any]) -> None:
        directory_fd = self._require_open()
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > 32 * 1024:
            raise DestructiveRestoreError("restore journal exceeds its size limit")
        temporary = f".ppbase-restore-{secrets.token_hex(12)}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                0o600,
                dir_fd=directory_fd,
            )
            view = memoryview(payload)
            offset = 0
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("short restore journal write")
                offset += written
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            temporary_info = os.fstat(descriptor)
            self._validate_file(temporary_info)
            os.close(descriptor)
            descriptor = None
            os.replace(
                temporary,
                _JOURNAL_FILE,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            published = os.stat(
                _JOURNAL_FILE,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            self._validate_file(published)
            if not same_file_identity(temporary_info, published):
                raise DestructiveRestoreError(
                    "restore journal changed while it was published"
                )
            os.fsync(directory_fd)
            self._root.verify_attached()
        except OSError as exc:
            raise DestructiveRestoreError("restore journal cannot be persisted safely") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def clear(self) -> None:
        directory_fd = self._require_open()
        try:
            visible = os.stat(
                _JOURNAL_FILE,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            self._validate_file(visible)
            os.unlink(_JOURNAL_FILE, dir_fd=directory_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DestructiveRestoreError("restore journal cannot be cleared safely") from exc
        os.fsync(directory_fd)
        self._root.verify_attached()


@dataclass(slots=True)
class PreparedStorageRestore:
    restore_id: str
    data_dir: Path
    work_root: Path
    target: AnchoredStagingDataDir
    journal: DestructiveRestoreJournal
    file_reference_inventory: dict[str, Any]
    previous_storage_name: str
    previous_secret_name: str
    _swapped: bool = False

    @property
    def work_dir(self) -> Path:
        return self.target.path

    def _journal_value(self, status: str) -> dict[str, Any]:
        return {
            "version": 1,
            "restoreId": self.restore_id,
            "dataDir": os.fspath(self.data_dir),
            "workRoot": os.fspath(self.work_root),
            "workDir": os.fspath(self.work_dir),
            "previousStorage": self.previous_storage_name,
            "previousSecret": self.previous_secret_name,
            _JOURNAL_FILE_REFERENCE_INVENTORY_KEY: dict(
                self.file_reference_inventory
            ),
            "status": status,
        }

    def swap_into_place(self) -> None:
        """Atomically select restored storage and secret on the local filesystem."""
        if self._swapped:
            raise DestructiveRestoreError("restored files are already active")
        self.target.verify_attached()
        data_fd = os.open(
            self.data_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not _entry_exists(data_fd, "storage"):
                os.mkdir("storage", mode=0o700, dir_fd=data_fd)
            if not _entry_exists(data_fd, ".jwt_secret"):
                raise DestructiveRestoreError(
                    "active JWT secret disappeared before restore cutover"
                )
            self.journal.write(self._journal_value("swap_started"))
            try:
                os.rename("storage", self.previous_storage_name, src_dir_fd=data_fd, dst_dir_fd=data_fd)
            except FileNotFoundError:
                pass
            os.rename(
                "storage",
                "storage",
                src_dir_fd=self.target.fileno(),
                dst_dir_fd=data_fd,
            )
            try:
                os.rename(
                    ".jwt_secret",
                    self.previous_secret_name,
                    src_dir_fd=data_fd,
                    dst_dir_fd=data_fd,
                )
            except FileNotFoundError:
                pass
            os.rename(
                ".jwt_secret",
                ".jwt_secret",
                src_dir_fd=self.target.fileno(),
                dst_dir_fd=data_fd,
            )
            os.fsync(data_fd)
            os.fsync(self.target.fileno())
            self._swapped = True
            self.journal.write(self._journal_value("files_swapped"))
        except BaseException:
            try:
                self._rollback_with_fd(data_fd)
                self._swapped = False
                self.journal.clear()
            except BaseException as rollback_error:
                raise DestructiveRestoreError(
                    "restored files could not be activated or rolled back safely"
                ) from rollback_error
            raise
        finally:
            os.close(data_fd)

    def _rollback_with_fd(self, data_fd: int) -> None:
        previous_storage_exists = _entry_exists(data_fd, self.previous_storage_name)
        if previous_storage_exists:
            _remove_entry_at(data_fd, "storage", self.data_dir / "storage")
            os.rename(
                self.previous_storage_name,
                "storage",
                src_dir_fd=data_fd,
                dst_dir_fd=data_fd,
            )

        previous_secret_exists = _entry_exists(data_fd, self.previous_secret_name)
        if previous_secret_exists:
            _remove_entry_at(data_fd, ".jwt_secret", self.data_dir / ".jwt_secret")
            os.rename(
                self.previous_secret_name,
                ".jwt_secret",
                src_dir_fd=data_fd,
                dst_dir_fd=data_fd,
            )
        os.fsync(data_fd)

    def rollback(self) -> None:
        data_fd = os.open(
            self.data_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            self._rollback_with_fd(data_fd)
        finally:
            os.close(data_fd)
        self._swapped = False
        self.journal.clear()
        self.cleanup_work_dir()

    def mark_database_committed(self) -> None:
        self.journal.write(self._journal_value("database_committed"))

    def cleanup_work_dir(self) -> None:
        self.target.close()
        try:
            shutil.rmtree(self.work_dir)
        except FileNotFoundError:
            pass
        try:
            self.work_root.rmdir()
        except OSError:
            pass


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _remove_entry_at(directory_fd: int, name: str, display_path: Path) -> None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise DestructiveRestoreError(f"refusing to remove restore symlink: {display_path}")
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(display_path)
    elif stat.S_ISREG(info.st_mode):
        os.unlink(name, dir_fd=directory_fd)
    else:
        raise DestructiveRestoreError(f"refusing to remove special restore entry: {display_path}")


def prepare_storage_restore(
    *,
    store: Any,
    inspection: VerifiedBackupInspection,
    settings: Any,
    journal: DestructiveRestoreJournal,
    restore_id: str,
    file_reference_inventory: dict[str, Any],
) -> PreparedStorageRestore:
    try:
        normalized_inventory = normalize_file_reference_inventory(
            file_reference_inventory
        )
    except ValueError as exc:
        raise DestructiveRestoreError(
            "backup local-file reference inventory is invalid"
        ) from exc
    data_dir = Path(settings.data_dir).expanduser().resolve(strict=True)
    data_info = data_dir.lstat()
    if data_dir.is_symlink() or not stat.S_ISDIR(data_info.st_mode):
        raise DestructiveRestoreError("active PPBase data_dir is unsafe")
    # Persist the currently selected secret before the journal can describe a
    # cutover.  This guarantees that rollback always has a concrete old file.
    settings.get_jwt_secret()
    work_root = data_dir / _RESTORE_ROOT
    _ensure_private_directory(work_root)
    target = store.open_staging_data_dir(work_root, restore_id)
    try:
        target.restore_files(inspection)
        jwt_metadata = inspection.manifest.metadata.get("jwt_secret")
        jwt_mode = jwt_metadata.get("mode") if isinstance(jwt_metadata, dict) else None
        if jwt_mode == "included_resource":
            target.install_jwt(inspection)
        elif jwt_mode == "external_required":
            secret = str(settings.get_jwt_secret()).strip()
            if not secret:
                raise DestructiveRestoreError("active external JWT secret is unavailable")
            target.write_secret((secret + "\n").encode("utf-8"))
        else:
            raise DestructiveRestoreError("backup JWT-secret metadata is invalid")
        target.verify_attached()
    except BaseException:
        target.close()
        try:
            shutil.rmtree(work_root / restore_id)
        except FileNotFoundError:
            pass
        raise
    return PreparedStorageRestore(
        restore_id=restore_id,
        data_dir=data_dir,
        work_root=work_root,
        target=target,
        journal=journal,
        file_reference_inventory=normalized_inventory,
        previous_storage_name=f".ppbase-storage-before-{restore_id}",
        previous_secret_name=f".ppbase-secret-before-{restore_id}",
    )


async def read_database_restore_marker(connection: Any) -> str | None:
    """Read the marker committed in the same transaction as pg_restore."""
    exists = await connection.scalar(
        text(
            "SELECT pg_catalog.to_regclass("
            "'_ppbase_restore_control.commits'"
            ") IS NOT NULL"
        )
    )
    if not bool(exists):
        return None
    value = await connection.scalar(
        text(
            'SELECT restore_id FROM "_ppbase_restore_control".commits '
            "ORDER BY committed_at DESC LIMIT 1"
        )
    )
    return None if value is None else str(value)


async def clear_database_restore_marker(connection: Any, restore_id: str) -> None:
    marker = await read_database_restore_marker(connection)
    if marker != restore_id:
        raise DestructiveRestoreError(
            "database restore marker changed during recovery finalization"
        )
    await connection.execute(text('DROP SCHEMA "_ppbase_restore_control" CASCADE'))


def _require_active_local_restore_files(
    data_dir: Path,
    references: tuple[LocalFileReference, ...],
    storage_config: Any,
) -> None:
    """Verify the active cutover tree without trusting process-global settings."""
    storage = data_dir / "storage"
    secret = data_dir / ".jwt_secret"
    try:
        storage_info = storage.lstat()
        secret_info = secret.lstat()
    except OSError as exc:
        raise DestructiveRestoreError(
            "restored local storage or JWT secret is missing"
        ) from exc
    if storage.is_symlink() or not stat.S_ISDIR(storage_info.st_mode):
        raise DestructiveRestoreError("restored local storage root is unsafe")
    if secret.is_symlink() or not stat.S_ISREG(secret_info.st_mode):
        raise DestructiveRestoreError("restored JWT secret is unsafe")

    for collection_id, record_id, filename in references:
        try:
            opened = open_file_stream(
                collection_id,
                record_id,
                filename,
                config=storage_config,
            )
        except Exception as exc:
            raise DestructiveRestoreError(
                "restored database file reference could not be opened safely: "
                f"{collection_id}/{record_id}/{filename}"
            ) from exc
        if opened is None or opened.backend != "local" or opened.storage_path is None:
            if opened is not None:
                opened.close()
            raise DestructiveRestoreError(
                "restored database references a missing local file: "
                f"{collection_id}/{record_id}/{filename}"
            )
        opened.close()


async def validate_committed_destructive_restore(
    settings: Any,
    connection: Any,
    expected_inventory: dict[str, Any],
) -> tuple[LocalFileReference, ...]:
    """Revalidate the committed DB and active files before old files are removed."""
    try:
        normalized_inventory = normalize_file_reference_inventory(
            expected_inventory
        )
    except ValueError as exc:
        raise DestructiveRestoreError(
            "restore journal has no valid local-file reference inventory"
        ) from exc

    try:
        references = await read_canonical_local_file_references(connection)
    except Exception as exc:
        raise DestructiveRestoreError(
            "restored database local-file references cannot be inspected"
        ) from exc
    if not file_reference_inventory_matches(normalized_inventory, references):
        raise DestructiveRestoreError(
            "restored local-file references differ from the backup inventory"
        )

    try:
        durable_settings = (
            await connection.execute(
                text(
                    'SELECT value FROM public."_params" '
                    "WHERE key = 'settings'"
                )
            )
        ).scalar_one_or_none()
        storage_config = resolve_storage_config_from_settings_payload(
            settings,
            durable_settings if isinstance(durable_settings, dict) else None,
        )
        configured_data_dir = (
            Path(storage_config.data_dir).expanduser().resolve(strict=True)
        )
        active_data_dir = Path(settings.data_dir).expanduser().resolve(strict=True)
    except Exception as exc:
        raise DestructiveRestoreError(
            "restored durable storage configuration cannot be validated"
        ) from exc
    if storage_config.backend != "local" or configured_data_dir != active_data_dir:
        raise DestructiveRestoreError(
            "restored durable storage configuration is not the active local data_dir"
        )

    await asyncio.to_thread(
        _require_active_local_restore_files,
        active_data_dir,
        references,
        storage_config,
    )
    return references


async def recover_interrupted_destructive_restore(
    settings: Any,
    connection: Any,
) -> dict[str, Any] | None:
    """Finish or roll back the sole in-flight file permutation during startup."""
    data_path = Path(settings.data_dir).expanduser()
    try:
        data_root = RuntimeDataRoot.open(data_path, create_missing=False)
    except ControlPlaneSafetyError as exc:
        marker = await read_database_restore_marker(connection)
        if not data_path.exists() and marker is None:
            return None
        if marker is not None:
            raise DestructiveRestoreError(
                "database restore marker exists without its filesystem journal"
            ) from exc
        raise DestructiveRestoreError(
            "the PPBase data directory cannot be opened for restore recovery"
        ) from exc
    journal = DestructiveRestoreJournal(data_root)
    try:
        state = journal.read()
        if state is None:
            if await read_database_restore_marker(connection) is not None:
                raise DestructiveRestoreError(
                    "database restore marker exists without its filesystem journal"
                )
            return None
        restore_id = str(state.get("restoreId", ""))
        if not _RESTORE_ID_RE.fullmatch(restore_id):
            raise DestructiveRestoreError("restore journal has an invalid restore ID")
        status = state.get("status")
        if status not in _JOURNAL_STATUSES:
            raise DestructiveRestoreError("restore journal has an invalid status")
        data_dir = Path(str(state.get("dataDir", ""))).expanduser().resolve(strict=True)
        configured_data_dir = Path(settings.data_dir).expanduser().resolve(strict=True)
        if not restore_id or data_dir != configured_data_dir:
            raise DestructiveRestoreError("restore journal targets another PPBase data_dir")
        previous_storage = str(state.get("previousStorage", ""))
        previous_secret = str(state.get("previousSecret", ""))
        if (
            previous_storage != f".ppbase-storage-before-{restore_id}"
            or previous_secret != f".ppbase-secret-before-{restore_id}"
        ):
            raise DestructiveRestoreError("restore journal file permutation is incomplete")
        work_root = Path(str(state.get("workRoot", "")))
        work_dir = Path(str(state.get("workDir", "")))
        expected_work_root = data_dir / _RESTORE_ROOT
        if work_root != expected_work_root or work_dir != expected_work_root / restore_id:
            raise DestructiveRestoreError("restore journal working paths are invalid")
        marker = await read_database_restore_marker(connection)
        if marker not in {None, restore_id}:
            raise DestructiveRestoreError(
                "restore journal and database marker identify different restores"
            )
        finalizing = status == "finalizing"
        committed = marker == restore_id or finalizing
        if committed:
            try:
                expected_inventory = normalize_file_reference_inventory(
                    state.get(_JOURNAL_FILE_REFERENCE_INVENTORY_KEY)
                )
            except ValueError as exc:
                raise DestructiveRestoreError(
                    "committed restore journal has no valid file-reference inventory"
                ) from exc
            await validate_committed_destructive_restore(
                settings,
                connection,
                expected_inventory,
            )
            if not finalizing:
                state = dict(state)
                state["status"] = "finalizing"
                journal.write(state)
            if marker == restore_id:
                # Commit marker removal before deleting the rollback files. The
                # durable ``finalizing`` journal makes either side of this
                # database/filesystem boundary safe to resume after a crash.
                await clear_database_restore_marker(connection, restore_id)
                await connection.commit()
        data_fd = os.open(
            data_dir,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if committed:
                _remove_entry_at(data_fd, previous_storage, data_dir / previous_storage)
                _remove_entry_at(data_fd, previous_secret, data_dir / previous_secret)
                outcome = "committed"
            else:
                if _entry_exists(data_fd, previous_storage):
                    _remove_entry_at(data_fd, "storage", data_dir / "storage")
                    os.rename(previous_storage, "storage", src_dir_fd=data_fd, dst_dir_fd=data_fd)
                if _entry_exists(data_fd, previous_secret):
                    _remove_entry_at(data_fd, ".jwt_secret", data_dir / ".jwt_secret")
                    os.rename(previous_secret, ".jwt_secret", src_dir_fd=data_fd, dst_dir_fd=data_fd)
                outcome = "rolled_back"
            os.fsync(data_fd)
        finally:
            os.close(data_fd)
        try:
            shutil.rmtree(work_dir)
        except FileNotFoundError:
            pass
        try:
            work_root.rmdir()
        except OSError:
            pass
        _fsync_directory(data_dir)
        journal.clear()
        return {"restoreId": restore_id, "outcome": outcome}
    finally:
        journal.close()
        data_root.close()


def destructive_restore_control_schema() -> str:
    return _CONTROL_SCHEMA


def destructive_restore_control_table() -> str:
    return _CONTROL_TABLE
