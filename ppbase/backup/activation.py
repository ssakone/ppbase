"""Durable, fail-closed activation state for native backup restores.

Activation never overwrites the currently running database or ``data_dir``.
It publishes a descriptor-anchored runtime overlay that selects either the
validated staging target or the previous target across process restarts.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ppbase.backup.control import (
    ControlPlaneRoot,
    ControlPlaneSafetyError,
    fsync_directory,
    open_flags,
    verify_directory_attached_at,
)


_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_ACTIVE_FILENAME = "current.json"
_LOCK_FILENAME = ".activation.lock"
_MAX_STATE_BYTES = 128 * 1024
_MIN_RESUME_TOKEN_CHARS = 32
_MAX_RESUME_TOKEN_CHARS = 512
_LOCAL_STARTING_ACTIVATION_ID: str | None = None


class ActivationError(RuntimeError):
    """Raised when activation state is missing, unsafe, or inconsistent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_id(value: str, *, label: str) -> str:
    normalized = str(value or "")
    if not _ID_RE.fullmatch(normalized):
        raise ActivationError(f"{label} must be a 32-character lowercase hex ID")
    return normalized


def _require_sha256(value: str, *, label: str) -> str:
    normalized = str(value or "")
    if not _SHA256_RE.fullmatch(normalized):
        raise ActivationError(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def _require_text(value: str, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or "\x00" in normalized:
        raise ActivationError(f"{label} is required")
    return normalized


def _require_command(value: Sequence[str], *, label: str) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise ActivationError(f"{label} must be an argument vector")
    command = [str(item) for item in value]
    if not command or any(not item or "\x00" in item for item in command):
        raise ActivationError(f"{label} must be a non-empty safe argument vector")
    return command


def _require_resume_token(value: str) -> str:
    if not isinstance(value, str):
        raise ActivationError("activation resume token must be text")
    if "\x00" in value:
        raise ActivationError("activation resume token contains a NUL byte")
    if not _MIN_RESUME_TOKEN_CHARS <= len(value) <= _MAX_RESUME_TOKEN_CHARS:
        raise ActivationError(
            "activation resume token must contain between "
            f"{_MIN_RESUME_TOKEN_CHARS} and {_MAX_RESUME_TOKEN_CHARS} characters"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ActivationError("activation resume token is not valid UTF-8 text") from exc
    return value


def _require_database_identity(
    value: Mapping[str, Any],
    *,
    label: str = "target database",
) -> dict[str, Any]:
    required_keys = {
        "role",
        "database",
        "serverAddress",
        "serverPort",
        "postmasterStartedAt",
        "serverVersionNum",
    }
    optional_keys = {"databaseOid", "databaseMarker"}
    supplied_keys = set(value)
    if not required_keys.issubset(supplied_keys) or not supplied_keys.issubset(
        required_keys | optional_keys
    ):
        raise ActivationError(f"{label} identity is incomplete or contains unknown fields")
    try:
        port = int(value["serverPort"])
    except (TypeError, ValueError) as exc:
        raise ActivationError(f"{label} server port is invalid") from exc
    if isinstance(value["serverPort"], bool) or not 0 <= port <= 65535:
        raise ActivationError(f"{label} server port is invalid")
    result = {
        "role": _require_text(str(value["role"]), label=f"{label} role"),
        "database": _require_text(
            str(value["database"]),
            label=f"{label} name",
        ),
        "serverAddress": str(value["serverAddress"]),
        "serverPort": port,
        "postmasterStartedAt": _require_text(
            str(value["postmasterStartedAt"]),
            label=f"{label} PostgreSQL start time",
        ),
        "serverVersionNum": _require_text(
            str(value["serverVersionNum"]),
            label=f"{label} PostgreSQL version",
        ),
    }
    if "databaseOid" in value:
        try:
            database_oid = int(value["databaseOid"])
        except (TypeError, ValueError) as exc:
            raise ActivationError(f"{label} OID is invalid") from exc
        if (
            isinstance(value["databaseOid"], bool)
            or not 1 <= database_oid <= 0xFFFFFFFF
        ):
            raise ActivationError(f"{label} OID is invalid")
        result["databaseOid"] = database_oid
    if "databaseMarker" in value:
        database_marker = str(value["databaseMarker"])
        if "\x00" in database_marker:
            raise ActivationError(f"{label} marker contains a NUL byte")
        result["databaseMarker"] = database_marker
    return result


def verify_runtime_database_identity(
    runtime_identity: Mapping[str, Any],
    expected_identity: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    """Validate and compare a live PostgreSQL identity without weakening legacy state.

    Older activation journals do not contain the database OID or staging
    marker.  Their original identity fields remain mandatory.  Newer state
    pins either optional field as soon as it is present, and startup fails
    closed if the live query omits or changes it.
    """
    if not isinstance(runtime_identity, Mapping) or not isinstance(
        expected_identity, Mapping
    ):
        raise ActivationError(f"{label} database identity is missing")
    runtime = _require_database_identity(
        runtime_identity,
        label=f"runtime {label} database",
    )
    expected = _require_database_identity(
        expected_identity,
        label=f"expected {label} database",
    )
    for key, expected_value in expected.items():
        if key not in runtime or runtime[key] != expected_value:
            raise ActivationError(f"{label} PostgreSQL identity changed after validation")
    return runtime


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ActivationError("activation state is not JSON serializable") from exc
    if len(payload) > _MAX_STATE_BYTES:
        raise ActivationError("activation state exceeds its size limit")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise ActivationError("short write while persisting activation state")
        remaining = remaining[written:]


def _open_private_runtime_root(path: Path, *, create_missing: bool) -> int:
    """Open one private activation filesystem root without following symlinks."""
    if not path.is_absolute():
        raise ActivationError("activation filesystem roots must be absolute")
    if create_missing:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        expected = path.lstat()
        if not stat.S_ISDIR(expected.st_mode) or stat.S_ISLNK(expected.st_mode):
            raise ActivationError("activation filesystem root is unsafe")
        os.chmod(path, 0o700, follow_symlinks=False)
        descriptor = os.open(
            path,
            open_flags(os.O_RDONLY | os.O_DIRECTORY),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o700
                or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise ActivationError("activation filesystem root changed while opening")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    except ActivationError:
        raise
    except OSError as exc:
        raise ActivationError("activation filesystem root cannot be opened safely") from exc


def _open_private_child_directory(parent_fd: int, name: str, *, label: str) -> int:
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(expected.st_mode)
            or stat.S_ISLNK(expected.st_mode)
            or stat.S_IMODE(expected.st_mode) != 0o700
        ):
            raise ActivationError(f"{label} is unsafe")
        descriptor = os.open(
            name,
            open_flags(os.O_RDONLY | os.O_DIRECTORY),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
            ):
                raise ActivationError(f"{label} changed while opening")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    except FileNotFoundError:
        raise
    except ActivationError:
        raise
    except OSError as exc:
        raise ActivationError(f"{label} cannot be opened safely") from exc


class BackupActivationStore:
    """Descriptor-anchored activation journal below ``backup_control_dir``."""

    def __init__(self, control_root: ControlPlaneRoot) -> None:
        self._root = control_root
        try:
            self._directory_fd = control_root.open_private_directory(
                "activations",
                label="The backup activation store",
                create_missing=True,
            )
            self._lock_fd = self._open_lock_file()
            self._verify_attached()
        except BaseException:
            directory_fd = getattr(self, "_directory_fd", None)
            if directory_fd is not None:
                os.close(directory_fd)
            raise

    @property
    def path(self) -> Path:
        return self._root.path / "activations"

    def _verify_attached(self) -> None:
        try:
            self._root.verify_attached()
            verify_directory_attached_at(
                self._root.fileno(),
                "activations",
                self._directory_fd,
                label="The backup activation store",
            )
            self._root.verify_attached()
        except (ControlPlaneSafetyError, OSError) as exc:
            raise ActivationError("backup activation store was detached or substituted") from exc

    def _open_lock_file(self) -> int:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                _LOCK_FILENAME,
                open_flags(os.O_RDWR | os.O_CREAT),
                0o600,
                dir_fd=self._directory_fd,
            )
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ActivationError("backup activation lock is unsafe")
            result = descriptor
            descriptor = None
            return result
        except OSError as exc:
            raise ActivationError("backup activation lock cannot be opened safely") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if getattr(self, "_lock_fd", -1) < 0:
            raise ActivationError("backup activation store is closed")
        self._verify_attached()
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            self._verify_attached()
            yield
            self._verify_attached()
        except OSError as exc:
            raise ActivationError("backup activation state cannot be locked safely") from exc
        finally:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass

    def _read_json(self, filename: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                filename,
                open_flags(os.O_RDONLY),
                dir_fd=self._directory_fd,
            )
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ActivationError("backup activation state was not found") from None
        except OSError as exc:
            raise ActivationError("backup activation state cannot be opened safely") from exc

        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > _MAX_STATE_BYTES
            ):
                raise ActivationError("backup activation state file is unsafe")
            chunks: list[bytes] = []
            remaining = _MAX_STATE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if not payload or len(payload) > _MAX_STATE_BYTES:
                raise ActivationError("backup activation state has an invalid size")
            try:
                value = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ActivationError("backup activation state is invalid JSON") from exc
            if not isinstance(value, dict):
                raise ActivationError("backup activation state must be a JSON object")
            return value
        finally:
            os.close(descriptor)

    def _write_atomic(self, filename: str, value: Mapping[str, Any]) -> None:
        payload = _canonical_json(value)
        temporary = f".activation-{secrets.token_hex(12)}.tmp"
        descriptor: int | None = None
        temporary_exists = False
        try:
            descriptor = os.open(
                temporary,
                open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                0o600,
                dir_fd=self._directory_fd,
            )
            temporary_exists = True
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ActivationError("activation temporary file is unsafe")
            os.close(descriptor)
            descriptor = None
            self._verify_attached()
            os.replace(
                temporary,
                filename,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
            temporary_exists = False
            fsync_directory(self._directory_fd)
            self._verify_attached()
        except OSError as exc:
            raise ActivationError("backup activation state cannot be persisted safely") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_exists:
                try:
                    os.unlink(temporary, dir_fd=self._directory_fd)
                except OSError:
                    pass

    def _state_filename(self, activation_id: str) -> str:
        return f"{_require_id(activation_id, label='activation ID')}.json"

    def _read_state(self, activation_id: str) -> dict[str, Any]:
        state = self._read_json(self._state_filename(activation_id))
        assert state is not None
        if state.get("activationId") != activation_id:
            raise ActivationError("backup activation identity does not match its filename")
        return state

    def _active_id(self) -> str | None:
        marker = self._read_json(_ACTIVE_FILENAME, missing_ok=True)
        if marker is None:
            return None
        return _require_id(str(marker.get("activationId", "")), label="active activation ID")

    def active(self) -> dict[str, Any] | None:
        """Return the currently selected activation, if any."""
        with self._locked():
            activation_id = self._active_id()
            if not activation_id:
                return None
            state = self._read_state(activation_id)
            # Crash recovery for the final rollback commit point: the state
            # JSON may be durable while restoration of the previous active
            # marker was interrupted.  Chained activations must return to the
            # preceding healthy target; only the first activation removes the
            # marker entirely.
            if state.get("status") == "rolled_back":
                return self._restore_previous_active_locked(state)
            return state

    def reconcile_legacy_targets(
        self,
        staging_root: str | Path,
        target_root: str | Path,
    ) -> None:
        """Crash-resumably move the active legacy target chain out of staging."""
        staging = Path(staging_root).expanduser().resolve(strict=False)
        targets = Path(target_root).expanduser().resolve(strict=False)
        if staging == targets:
            raise ActivationError("staging and durable target roots must be distinct")

        with self._locked():
            active_id = self._active_id()
            if active_id is None:
                return
            chain: list[tuple[str, dict[str, Any]]] = []
            seen: set[str] = set()
            selected_id: str | None = active_id
            while selected_id is not None:
                if selected_id in seen:
                    raise ActivationError("backup activation chain contains a cycle")
                seen.add(selected_id)
                state = self._read_state(selected_id)
                chain.append((selected_id, state))
                previous = state.get("previousActivationId")
                selected_id = (
                    _require_id(str(previous), label="previous activation ID")
                    if previous is not None
                    else None
                )

            previous_state: dict[str, Any] | None = None
            for activation_id, original in reversed(chain):
                updated = dict(original)
                if previous_state is not None:
                    updated["previousDataDir"] = previous_state["targetDataDir"]
                    updated["previousDatabaseUrl"] = previous_state[
                        "targetDatabaseUrl"
                    ]
                    updated["previousRestartCommand"] = replace_serve_command_targets(
                        updated["previousRestartCommand"],
                        database_url=updated["previousDatabaseUrl"],
                        data_dir=updated["previousDataDir"],
                    )
                    updated["previousRestartEnvironment"] = dict(
                        previous_state["targetRestartEnvironment"]
                    )
                    if "targetDumpDatabaseUrl" in previous_state:
                        updated["previousDumpDatabaseUrl"] = previous_state[
                            "targetDumpDatabaseUrl"
                        ]
                    for target_key, previous_key in (
                        ("targetDataDevice", "previousDataDevice"),
                        ("targetDataInode", "previousDataInode"),
                        ("expectedJwtSha256", "expectedPreviousJwtSha256"),
                        (
                            "expectedDatabaseIdentity",
                            "expectedPreviousDatabaseIdentity",
                        ),
                    ):
                        if target_key in previous_state:
                            updated[previous_key] = previous_state[target_key]

                updated = self._promote_legacy_target_locked(
                    updated,
                    staging=staging,
                    targets=targets,
                )
                if updated != original:
                    updated["updatedAt"] = _now()
                    self._write_atomic(self._state_filename(activation_id), updated)
                previous_state = updated

    def _promote_legacy_target_locked(
        self,
        state: dict[str, Any],
        *,
        staging: Path,
        targets: Path,
    ) -> dict[str, Any]:
        plan_id = _require_id(str(state.get("planId", "")), label="staging plan ID")
        current = Path(
            _require_text(str(state.get("targetDataDir", "")), label="target data_dir")
        ).expanduser().resolve(strict=False)
        legacy = staging / plan_id / "data"
        promoted = targets / plan_id / "data"
        if current not in {legacy, promoted}:
            return state
        expected_device = state.get("targetDataDevice")
        expected_inode = state.get("targetDataInode")
        if (
            isinstance(expected_device, bool)
            or isinstance(expected_inode, bool)
            or not isinstance(expected_device, int)
            or not isinstance(expected_inode, int)
        ):
            raise ActivationError("legacy target identity is missing")
        expected_identity = (expected_device, expected_inode)

        if current == promoted:
            info = promoted.stat()
            if (info.st_dev, info.st_ino) != expected_identity:
                raise ActivationError("promoted target identity changed")
            return state

        staging_fd: int | None = None
        targets_fd: int | None = None
        plan_fd: int | None = None
        data_fd: int | None = None
        try:
            staging_fd = _open_private_runtime_root(staging, create_missing=False)
            targets_fd = _open_private_runtime_root(targets, create_missing=True)
            if os.fstat(staging_fd).st_dev != os.fstat(targets_fd).st_dev:
                raise ActivationError(
                    "legacy target promotion requires staging and target roots on the same filesystem"
                )
            try:
                plan_fd = _open_private_child_directory(
                    staging_fd,
                    plan_id,
                    label="legacy staging plan directory",
                )
            except FileNotFoundError:
                plan_fd = _open_private_child_directory(
                    targets_fd,
                    plan_id,
                    label="promoted target plan directory",
                )
                data_fd = _open_private_child_directory(
                    plan_fd,
                    "data",
                    label="promoted target data_dir",
                )
                if (
                    os.fstat(data_fd).st_dev,
                    os.fstat(data_fd).st_ino,
                ) != expected_identity:
                    raise ActivationError("promoted target identity changed")
            else:
                data_fd = _open_private_child_directory(
                    plan_fd,
                    "data",
                    label="legacy target data_dir",
                )
                if (
                    os.fstat(data_fd).st_dev,
                    os.fstat(data_fd).st_ino,
                ) != expected_identity:
                    raise ActivationError("legacy target identity changed")
                try:
                    os.stat(plan_id, dir_fd=targets_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ActivationError("legacy and promoted target directories both exist")
                os.rename(
                    plan_id,
                    plan_id,
                    src_dir_fd=staging_fd,
                    dst_dir_fd=targets_fd,
                )
                fsync_directory(staging_fd)
                fsync_directory(targets_fd)
            updated = dict(state)
            updated["targetDataDir"] = str(promoted)
            updated["targetRestartCommand"] = replace_serve_command_targets(
                updated["targetRestartCommand"],
                database_url=str(updated["targetDatabaseUrl"]),
                data_dir=str(promoted),
            )
            environment = dict(updated["targetRestartEnvironment"])
            environment["PPBASE_DATABASE_URL"] = str(updated["targetDatabaseUrl"])
            if "targetDumpDatabaseUrl" in updated:
                environment["PPBASE_BACKUP_DUMP_DATABASE_URL"] = str(
                    updated["targetDumpDatabaseUrl"]
                )
            updated["targetRestartEnvironment"] = environment
            return updated
        except FileNotFoundError as exc:
            raise ActivationError("legacy activation target is missing") from exc
        except OSError as exc:
            raise ActivationError("legacy activation target cannot be promoted safely") from exc
        finally:
            for descriptor in (data_fd, plan_fd, targets_fd, staging_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _linked_previous_activation(
        self,
        state: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        raw_previous_id = state.get("previousActivationId")
        if raw_previous_id is None:
            return None
        previous_id = _require_id(
            str(raw_previous_id),
            label="previous activation ID",
        )
        if previous_id == str(state.get("activationId", "")):
            raise ActivationError("backup activation cannot reference itself")
        previous = self._read_state(previous_id)
        if (
            previous.get("status") != "healthy"
            or previous.get("selectedTarget") != "target"
        ):
            raise ActivationError(
                "previous backup activation is not a healthy active target"
            )
        for previous_key, current_key, label in (
            ("targetDatabaseUrl", "previousDatabaseUrl", "database URL"),
            ("targetDataDir", "previousDataDir", "data_dir"),
        ):
            if previous.get(previous_key) != state.get(current_key):
                raise ActivationError(
                    f"previous backup activation {label} does not match"
                )
        optional_pairs = (
            ("targetDumpDatabaseUrl", "previousDumpDatabaseUrl"),
            ("expectedJwtSha256", "expectedPreviousJwtSha256"),
            ("targetDataDevice", "previousDataDevice"),
            ("targetDataInode", "previousDataInode"),
            ("expectedDatabaseIdentity", "expectedPreviousDatabaseIdentity"),
        )
        for previous_key, current_key in optional_pairs:
            previous_value = previous.get(previous_key)
            current_value = state.get(current_key)
            if previous_value != current_value:
                raise ActivationError(
                    "previous backup activation identity does not match"
                )
        return previous_id, previous

    def _restore_previous_active_locked(
        self,
        state: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        current_id = _require_id(
            str(state.get("activationId", "")),
            label="activation ID",
        )
        linked = self._linked_previous_activation(state)
        visible_id = self._active_id()
        if linked is not None:
            previous_id, previous = linked
            if visible_id not in {current_id, previous_id}:
                raise ActivationError(
                    "active activation marker changed during rollback"
                )
            if visible_id != previous_id:
                self._write_atomic(
                    _ACTIVE_FILENAME,
                    {"activationId": previous_id},
                )
            return previous

        if visible_id is None:
            return None
        if visible_id != current_id:
            raise ActivationError(
                "active activation marker changed during rollback"
            )
        try:
            info = os.stat(
                _ACTIVE_FILENAME,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(info.st_mode):
                raise ActivationError("active activation marker is unsafe")
            os.unlink(_ACTIVE_FILENAME, dir_fd=self._directory_fd)
            fsync_directory(self._directory_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ActivationError(
                "completed rollback marker cannot be reconciled"
            ) from exc
        return None

    def prepare(
        self,
        *,
        activation_id: str,
        plan_id: str,
        backup_id: str,
        plan_hash: str,
        manifest_sha256: str,
        signer_fingerprint_sha256: str,
        jwt_secret_mode: str,
        previous_database_url: str,
        previous_data_dir: str,
        previous_restart_command: Sequence[str],
        target_database_url: str,
        target_data_dir: str,
        target_restart_command: Sequence[str],
        expected_jwt_sha256: str,
        actor_id: str | None,
        previous_data_identity: tuple[int, int] | None = None,
        expected_previous_jwt_sha256: str | None = None,
        expected_previous_database_identity: Mapping[str, Any] | None = None,
        target_data_identity: tuple[int, int] | None = None,
        expected_database_identity: Mapping[str, Any] | None = None,
        resume_token: str | None = None,
        previous_dump_database_url: str | None = None,
        target_dump_database_url: str | None = None,
        pre_commit_guard: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Publish a validated target and return its one-time resume token."""
        activation_id = _require_id(activation_id, label="activation ID")
        plan_id = _require_id(plan_id, label="staging plan ID")
        if jwt_secret_mode not in {"disaster_recovery", "clone"}:
            raise ActivationError("JWT secret mode must be disaster_recovery or clone")
        if resume_token is None:
            resume_token = secrets.token_urlsafe(48)
        resume_token = _require_resume_token(resume_token)
        timestamp = _now()
        state: dict[str, Any] = {
            "activationId": activation_id,
            "planId": plan_id,
            "backupId": _require_text(backup_id, label="backup ID"),
            "planHash": _require_sha256(plan_hash, label="staging plan hash"),
            "manifestSha256": _require_sha256(manifest_sha256, label="manifest digest"),
            "signerFingerprintSha256": _require_sha256(
                signer_fingerprint_sha256,
                label="signer fingerprint",
            ),
            "jwtSecretMode": jwt_secret_mode,
            "previousDatabaseUrl": _require_text(
                previous_database_url,
                label="previous database URL",
            ),
            "previousDataDir": _require_text(previous_data_dir, label="previous data_dir"),
            "previousRestartCommand": _require_command(
                replace_serve_command_targets(
                    previous_restart_command,
                    database_url=previous_database_url,
                    data_dir=previous_data_dir,
                ),
                label="previous restart command",
            ),
            "previousRestartEnvironment": {
                "PPBASE_DATABASE_URL": _require_text(
                    previous_database_url,
                    label="previous database URL",
                )
            },
            "targetDatabaseUrl": _require_text(target_database_url, label="target database URL"),
            "targetDataDir": _require_text(target_data_dir, label="target data_dir"),
            "targetRestartCommand": _require_command(
                replace_serve_command_targets(
                    target_restart_command,
                    database_url=target_database_url,
                    data_dir=target_data_dir,
                ),
                label="target restart command",
            ),
            "targetRestartEnvironment": {
                "PPBASE_DATABASE_URL": _require_text(
                    target_database_url,
                    label="target database URL",
                )
            },
            "expectedJwtSha256": _require_sha256(
                expected_jwt_sha256,
                label="expected JWT secret digest",
            ),
            "actorId": str(actor_id) if actor_id is not None else None,
            "resumeTokenSha256": hashlib.sha256(resume_token.encode("utf-8")).hexdigest(),
            "status": "restart_scheduled",
            "selectedTarget": "target",
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "errorCode": None,
        }
        if previous_dump_database_url is not None:
            state["previousDumpDatabaseUrl"] = _require_text(
                previous_dump_database_url,
                label="previous dump database URL",
            )
            state["previousRestartEnvironment"][
                "PPBASE_BACKUP_DUMP_DATABASE_URL"
            ] = state["previousDumpDatabaseUrl"]
        if target_dump_database_url is not None:
            state["targetDumpDatabaseUrl"] = _require_text(
                target_dump_database_url,
                label="target dump database URL",
            )
            state["targetRestartEnvironment"][
                "PPBASE_BACKUP_DUMP_DATABASE_URL"
            ] = state["targetDumpDatabaseUrl"]
        if target_data_identity is not None:
            if (
                len(target_data_identity) != 2
                or any(
                    isinstance(item, bool) or not isinstance(item, int) or item < 0
                    for item in target_data_identity
                )
            ):
                raise ActivationError("target data_dir identity is invalid")
            state["targetDataDevice"] = target_data_identity[0]
            state["targetDataInode"] = target_data_identity[1]
        if previous_data_identity is not None:
            if (
                len(previous_data_identity) != 2
                or any(
                    isinstance(item, bool) or not isinstance(item, int) or item < 0
                    for item in previous_data_identity
                )
            ):
                raise ActivationError("previous data_dir identity is invalid")
            state["previousDataDevice"] = previous_data_identity[0]
            state["previousDataInode"] = previous_data_identity[1]
        if expected_previous_jwt_sha256 is not None:
            state["expectedPreviousJwtSha256"] = _require_sha256(
                expected_previous_jwt_sha256,
                label="previous JWT secret digest",
            )
        if expected_previous_database_identity is not None:
            state["expectedPreviousDatabaseIdentity"] = _require_database_identity(
                expected_previous_database_identity
            )
        if expected_database_identity is not None:
            state["expectedDatabaseIdentity"] = _require_database_identity(
                expected_database_identity
            )
        filename = self._state_filename(activation_id)
        with self._locked():
            if self._read_json(filename, missing_ok=True) is not None:
                raise ActivationError("backup activation already exists")
            previous_activation_id = self._active_id()
            if previous_activation_id is not None:
                previous_state = self._read_state(previous_activation_id)
                if previous_state.get("status") == "rolled_back":
                    previous_state = self._restore_previous_active_locked(
                        previous_state
                    )
                    previous_activation_id = (
                        str(previous_state.get("activationId", ""))
                        if previous_state is not None
                        else None
                    )
                if previous_activation_id is not None:
                    state["previousActivationId"] = previous_activation_id
                    self._linked_previous_activation(state)
            self._write_atomic(filename, state)
            if pre_commit_guard is not None:
                pre_commit_guard()
            self._write_atomic(_ACTIVE_FILENAME, {"activationId": activation_id})
        payload = self.public_payload(state)
        payload["resumeToken"] = resume_token
        return payload

    def inspect(self, activation_id: str) -> dict[str, Any]:
        with self._locked():
            return self._read_state(_require_id(activation_id, label="activation ID"))

    def statuses_for_plan(
        self,
        plan_id: str,
        *,
        backup_id: str,
    ) -> tuple[str, ...]:
        """Return every durable activation status bound to one staging plan."""
        selected_plan_id = _require_id(plan_id, label="staging plan ID")
        selected_backup_id = _require_text(backup_id, label="backup ID")
        allowed_statuses = {
            "restart_scheduled",
            "starting",
            "healthy",
            "rollback_pending",
            "rolled_back",
        }
        with self._locked():
            try:
                with os.scandir(self._directory_fd) as entries:
                    activation_ids = sorted(
                        entry.name[:-5]
                        for entry in entries
                        if entry.name.endswith(".json")
                        and _ID_RE.fullmatch(entry.name[:-5])
                    )
            except OSError as exc:
                raise ActivationError(
                    "backup activation states cannot be enumerated safely"
                ) from exc

            statuses: list[str] = []
            for activation_id in activation_ids:
                state = self._read_state(activation_id)
                if state.get("planId") != selected_plan_id:
                    continue
                if state.get("backupId") != selected_backup_id:
                    raise ActivationError(
                        "staging plan activation is bound to another backup"
                    )
                status = str(state.get("status", ""))
                if status not in allowed_statuses:
                    raise ActivationError(
                        "staging plan activation has an invalid status"
                    )
                statuses.append(status)
            return tuple(statuses)

    def authenticate(self, activation_id: str, resume_token: str) -> bool:
        try:
            return self.authenticate_state(activation_id, resume_token) is not None
        except ActivationError:
            return False

    def authenticate_state(
        self,
        activation_id: str,
        resume_token: str,
    ) -> dict[str, Any] | None:
        """Return canonical state only when the scoped token hash matches."""
        if not isinstance(resume_token, str) or not resume_token:
            return None
        state = self.inspect(activation_id)
        supplied = hashlib.sha256(resume_token.encode("utf-8")).hexdigest()
        expected = str(state.get("resumeTokenSha256", ""))
        return state if expected and hmac.compare_digest(supplied, expected) else None

    def public_payload(self, state: Mapping[str, Any]) -> dict[str, Any]:
        internal_status = str(state.get("status", ""))
        status = "succeeded" if internal_status == "healthy" else internal_status
        phases = {
            "restart_scheduled": "restarting",
            "starting": "health_check",
            "healthy": "health_check",
            "rollback_pending": "rollback_restart",
            "rolled_back": "rollback_health_check",
        }
        messages = {
            "restart_scheduled": "Activation is scheduled and PPBase is restarting.",
            "starting": "The restored target is running startup health checks.",
            "healthy": "The restored target passed all startup health checks.",
            "rollback_pending": "Target health failed; PPBase is restarting on the previous targets.",
            "rolled_back": "The previous database and data directory are active again.",
        }
        payload = {
            key: state.get(key)
            for key in (
                "activationId",
                "planId",
                "backupId",
                "planHash",
                "manifestSha256",
                "signerFingerprintSha256",
                "jwtSecretMode",
                "actorId",
                "selectedTarget",
                "createdAt",
                "updatedAt",
                "errorCode",
            )
        }
        payload["status"] = status
        payload["phase"] = phases.get(internal_status, "queued")
        payload["message"] = messages.get(internal_status)
        payload["failureCode"] = state.get("errorCode")
        payload["startedAt"] = state.get("createdAt")
        if internal_status in {"healthy", "rolled_back"}:
            payload["completedAt"] = state.get("updatedAt")
        payload["activationPerformed"] = internal_status == "healthy"
        payload["canRollback"] = internal_status in {"restart_scheduled", "starting"}
        return payload

    def _transition(
        self,
        activation_id: str,
        *,
        allowed: set[str],
        status: str,
        selected_target: str,
        error_code: str | None = None,
        clear_active: bool = False,
        preserve_error: bool = False,
    ) -> dict[str, Any]:
        activation_id = _require_id(activation_id, label="activation ID")
        with self._locked():
            state = self._read_state(activation_id)
            current = str(state.get("status", ""))
            if current == status:
                if clear_active:
                    self._restore_previous_active_locked(state)
                return state
            if current not in allowed:
                raise ActivationError(
                    f"cannot transition backup activation from {current!r} to {status!r}"
                )
            active_id = self._active_id()
            if active_id != activation_id:
                raise ActivationError("backup activation is no longer the active target")
            updated = dict(state)
            updated.update(
                {
                    "status": status,
                    "selectedTarget": selected_target,
                    "updatedAt": _now(),
                    "errorCode": (
                        state.get("errorCode") if preserve_error else error_code
                    ),
                }
            )
            self._write_atomic(self._state_filename(activation_id), updated)
            if clear_active:
                self._restore_previous_active_locked(updated)
            return updated

    def mark_starting(self, activation_id: str) -> dict[str, Any]:
        return self._transition(
            activation_id,
            allowed={"restart_scheduled"},
            status="starting",
            selected_target="target",
        )

    def mark_healthy(self, activation_id: str) -> dict[str, Any]:
        return self._transition(
            activation_id,
            allowed={"starting"},
            status="healthy",
            selected_target="target",
        )

    def mark_rollback_pending(self, activation_id: str, *, error_code: str) -> dict[str, Any]:
        return self._transition(
            activation_id,
            allowed={"restart_scheduled", "starting"},
            status="rollback_pending",
            selected_target="previous",
            error_code=_require_text(error_code, label="rollback error code")[:200],
        )

    def mark_rolled_back(self, activation_id: str) -> dict[str, Any]:
        return self._transition(
            activation_id,
            allowed={"rollback_pending"},
            status="rolled_back",
            selected_target="previous",
            clear_active=True,
            preserve_error=True,
        )

    def close(self) -> None:
        lock_fd = getattr(self, "_lock_fd", -1)
        self._lock_fd = -1
        if lock_fd >= 0:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        directory_fd = getattr(self, "_directory_fd", -1)
        self._directory_fd = -1
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass

    def __enter__(self) -> "BackupActivationStore":
        self._verify_attached()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()


def settle_failed_activation_restart(
    control_dir: str | Path,
    activation_id: str,
    *,
    error_code: str,
) -> dict[str, Any]:
    """Durably settle an accepted restart whose ``exec`` later failed."""
    selected_control = Path(control_dir).expanduser()
    if not selected_control.is_absolute():
        selected_control = Path(os.path.abspath(os.fspath(selected_control)))
    root = ControlPlaneRoot.open(selected_control, create_missing=False)
    store = BackupActivationStore(root)
    try:
        normalized_id = _require_id(activation_id, label="activation ID")
        state = store.inspect(normalized_id)
        status = str(state.get("status", ""))
        if status == "restart_scheduled":
            state = store.mark_rollback_pending(
                normalized_id,
                error_code=error_code,
            )
            status = str(state.get("status", ""))
        if status == "rollback_pending":
            state = store.mark_rolled_back(normalized_id)
        elif status != "rolled_back":
            raise ActivationError(
                "backup activation restart failure arrived after startup began"
            )
        return state
    finally:
        store.close()
        root.close()


def replace_serve_command_targets(
    command: Sequence[str],
    *,
    database_url: str,
    data_dir: str,
) -> list[str]:
    """Remove DSNs from argv and select ``data_dir`` deterministically."""
    original = _require_command(command, label="restart command")
    _require_text(database_url, label="database URL")
    result: list[str] = []
    index = 0
    while index < len(original):
        item = original[index]
        if item in {"--db", "--dir"}:
            index += 2
            continue
        if item.startswith("--db=") or item.startswith("--dir="):
            index += 1
            continue
        result.append(item)
        index += 1
    result.extend(["--dir", _require_text(data_dir, label="data_dir")])
    return result


def apply_activation_runtime_overlay(settings: Any) -> dict[str, Any] | None:
    """Apply the durable active target to mutable runtime settings.

    A missing control plane means that no activation has ever been prepared.
    An existing but unsafe control plane fails closed.
    """
    control_path = Path(str(getattr(settings, "backup_control_dir", "") or "")).expanduser()
    if not control_path.is_absolute():
        control_path = Path(os.path.abspath(os.fspath(control_path)))
    if not os.path.lexists(control_path):
        return None
    root: ControlPlaneRoot | None = None
    store: BackupActivationStore | None = None
    try:
        root = ControlPlaneRoot.open(control_path, create_missing=False)
        try:
            store = BackupActivationStore(root)
        except ControlPlaneSafetyError as exc:
            raise ActivationError("backup activation control plane is unsafe") from exc
        staging_value = str(
            getattr(settings, "backup_staging_root", "") or ""
        ).strip()
        if staging_value:
            configured_target = str(
                getattr(settings, "backup_target_root", "") or ""
            ).strip()
            store.reconcile_legacy_targets(
                staging_value,
                configured_target or f"{staging_value}_targets",
            )
        state = store.active()
        if state is None:
            return None
        # A persisted ``starting`` state means the target process died before
        # reaching the durable health commit point.  A fresh process must not
        # retry that target indefinitely; switch to the previous target first.
        if (
            state.get("status") == "starting"
            and _LOCAL_STARTING_ACTIVATION_ID
            != str(state.get("activationId", ""))
        ):
            state = store.mark_rollback_pending(
                str(state.get("activationId", "")),
                error_code="activation_startup_interrupted",
            )
        selected = str(state.get("selectedTarget", ""))
        if selected == "target":
            database_url = _require_text(str(state.get("targetDatabaseUrl", "")), label="target database URL")
            data_dir = _require_text(str(state.get("targetDataDir", "")), label="target data_dir")
            dump_database_url = state.get("targetDumpDatabaseUrl")
            # The staged target always owns the effective JWT secret through
            # its private ``data_dir/.jwt_secret`` file (preserved for DR or
            # generated for clone mode).  A deployment-level
            # ``PPBASE_JWT_SECRET`` has already been materialized into the
            # Settings instance before this durable overlay is applied; if it
            # remained set it would silently override the staged secret and
            # make every clone activation fail its health gate.
            setattr(settings, "jwt_secret", "")
        elif selected == "previous":
            database_url = _require_text(str(state.get("previousDatabaseUrl", "")), label="previous database URL")
            data_dir = _require_text(str(state.get("previousDataDir", "")), label="previous data_dir")
            dump_database_url = state.get("previousDumpDatabaseUrl")
            # A chained rollback returns to the preceding healthy activation,
            # whose effective secret is pinned by its private
            # ``data_dir/.jwt_secret``.  The first activation instead returns
            # to the deployment that launched PPBase and must preserve an
            # explicit PPBASE_JWT_SECRET if one configured that deployment.
            previous_activation_id = state.get("previousActivationId")
            if previous_activation_id is not None:
                _require_id(
                    str(previous_activation_id),
                    label="previous activation ID",
                )
                setattr(settings, "jwt_secret", "")
        else:
            raise ActivationError("active backup activation selected an invalid target")
        setattr(settings, "database_url", database_url)
        setattr(settings, "data_dir", data_dir)
        if dump_database_url is not None:
            setattr(
                settings,
                "backup_dump_database_url",
                _require_text(
                    str(dump_database_url),
                    label="activation dump database URL",
                ),
            )
        return state
    except ControlPlaneSafetyError as exc:
        raise ActivationError("backup activation control plane is unsafe") from exc
    finally:
        if store is not None:
            store.close()
        if root is not None:
            root.close()


def verify_activation_target(settings: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the selected target data directory and effective JWT secret."""
    if state.get("selectedTarget") != "target":
        raise ActivationError("activation health check is not running on the target")
    expected_data_dir = _require_text(
        str(state.get("targetDataDir", "")),
        label="target data_dir",
    )
    configured_data_dir = os.path.abspath(
        os.fspath(Path(str(getattr(settings, "data_dir", ""))).expanduser())
    )
    if configured_data_dir != os.path.abspath(expected_data_dir):
        raise ActivationError("runtime data_dir does not match the activated target")

    data_fd: int | None = None
    secret_fd: int | None = None
    storage_fd: int | None = None
    try:
        data_fd = os.open(expected_data_dir, open_flags(os.O_RDONLY | os.O_DIRECTORY))
        data_info = os.fstat(data_fd)
        if (
            not stat.S_ISDIR(data_info.st_mode)
            or data_info.st_uid != os.geteuid()
            or stat.S_IMODE(data_info.st_mode) != 0o700
        ):
            raise ActivationError("activated data_dir is not a private owned directory")
        expected_device = state.get("targetDataDevice")
        expected_inode = state.get("targetDataInode")
        if expected_device is not None or expected_inode is not None:
            if (
                isinstance(expected_device, bool)
                or isinstance(expected_inode, bool)
                or not isinstance(expected_device, int)
                or not isinstance(expected_inode, int)
                or (data_info.st_dev, data_info.st_ino)
                != (expected_device, expected_inode)
            ):
                raise ActivationError("activated data_dir identity changed after validation")

        storage_fd = os.open(
            "storage",
            open_flags(os.O_RDONLY | os.O_DIRECTORY),
            dir_fd=data_fd,
        )
        storage_info = os.fstat(storage_fd)
        if not stat.S_ISDIR(storage_info.st_mode) or storage_info.st_uid != os.geteuid():
            raise ActivationError("activated business-file storage is unsafe")

        secret_fd = os.open(".jwt_secret", open_flags(os.O_RDONLY), dir_fd=data_fd)
        secret_info = os.fstat(secret_fd)
        if (
            not stat.S_ISREG(secret_info.st_mode)
            or secret_info.st_uid != os.geteuid()
            or secret_info.st_nlink != 1
            or stat.S_IMODE(secret_info.st_mode) != 0o600
            or secret_info.st_size > 4096
        ):
            raise ActivationError("activated JWT secret file is unsafe")
        raw_secret = os.read(secret_fd, 4097)
        if not raw_secret or len(raw_secret) > 4096:
            raise ActivationError("activated JWT secret file has an invalid size")
        try:
            normalized_secret = raw_secret.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ActivationError("activated JWT secret is not UTF-8") from exc
        if not normalized_secret:
            raise ActivationError("activated JWT secret is empty")
        actual_digest = hashlib.sha256(normalized_secret.encode("utf-8")).hexdigest()
        expected_digest = _require_sha256(
            str(state.get("expectedJwtSha256", "")),
            label="expected JWT secret digest",
        )
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise ActivationError("activated JWT secret does not match the staged target")
        effective_secret = str(settings.get_jwt_secret())
        effective_digest = hashlib.sha256(effective_secret.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(effective_digest, expected_digest):
            raise ActivationError("effective runtime JWT secret does not match the staged target")
        return {
            "dataDir": "ok",
            "storage": "ok",
            "jwtSecret": "ok",
        }
    except FileNotFoundError as exc:
        raise ActivationError("activated data_dir is incomplete") from exc
    except OSError as exc:
        raise ActivationError("activated data_dir cannot be verified safely") from exc
    finally:
        for descriptor in (secret_fd, storage_fd, data_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def verify_activation_previous(settings: Any, state: Mapping[str, Any]) -> dict[str, Any]:
    """Verify that rollback selected the exact pre-activation filesystem target."""
    if state.get("selectedTarget") != "previous":
        raise ActivationError("rollback health check is not running on the previous target")
    expected_data_dir = _require_text(
        str(state.get("previousDataDir", "")),
        label="previous data_dir",
    )
    configured_data_dir = os.path.abspath(
        os.fspath(Path(str(getattr(settings, "data_dir", ""))).expanduser())
    )
    if configured_data_dir != os.path.abspath(expected_data_dir):
        raise ActivationError("runtime data_dir does not match the rollback target")

    data_fd: int | None = None
    storage_fd: int | None = None
    try:
        data_fd = os.open(expected_data_dir, open_flags(os.O_RDONLY | os.O_DIRECTORY))
        info = os.fstat(data_fd)
        expected_device = state.get("previousDataDevice")
        expected_inode = state.get("previousDataInode")
        if (
            not stat.S_ISDIR(info.st_mode)
            or isinstance(expected_device, bool)
            or isinstance(expected_inode, bool)
            or not isinstance(expected_device, int)
            or not isinstance(expected_inode, int)
            or (info.st_dev, info.st_ino) != (expected_device, expected_inode)
        ):
            raise ActivationError("previous data_dir identity changed before rollback")
        storage_fd = os.open(
            "storage",
            open_flags(os.O_RDONLY | os.O_DIRECTORY),
            dir_fd=data_fd,
        )
        if not stat.S_ISDIR(os.fstat(storage_fd).st_mode):
            raise ActivationError("previous business-file storage is unsafe")
        expected_jwt = _require_sha256(
            str(state.get("expectedPreviousJwtSha256", "")),
            label="previous JWT secret digest",
        )
        effective_jwt = hashlib.sha256(
            str(settings.get_jwt_secret()).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(expected_jwt, effective_jwt):
            raise ActivationError("previous JWT secret changed before rollback")
        return {"dataDir": "ok", "storage": "ok", "jwtSecret": "ok"}
    except FileNotFoundError as exc:
        raise ActivationError("previous data_dir is incomplete") from exc
    except OSError as exc:
        raise ActivationError("previous data_dir cannot be verified safely") from exc
    finally:
        for descriptor in (storage_fd, data_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def activation_restart_command(state: Mapping[str, Any]) -> list[str]:
    """Return the command for the target selected by an activation state."""
    selected = str(state.get("selectedTarget", ""))
    if selected == "target":
        value = state.get("targetRestartCommand")
    elif selected == "previous":
        value = state.get("previousRestartCommand")
    else:
        raise ActivationError("backup activation selected an invalid restart target")
    if not isinstance(value, list):
        raise ActivationError("backup activation restart command is invalid")
    return _require_command(value, label="activation restart command")


def activation_restart_environment(state: Mapping[str, Any]) -> dict[str, str]:
    """Return the scoped environment for the selected restart target."""
    selected = str(state.get("selectedTarget", ""))
    if selected == "target":
        value = state.get("targetRestartEnvironment")
    elif selected == "previous":
        value = state.get("previousRestartEnvironment")
    else:
        raise ActivationError("backup activation selected an invalid restart target")
    allowed_keys = {
        "PPBASE_DATABASE_URL",
        "PPBASE_BACKUP_DUMP_DATABASE_URL",
    }
    if (
        not isinstance(value, dict)
        or "PPBASE_DATABASE_URL" not in value
        or not set(value).issubset(allowed_keys)
    ):
        raise ActivationError("backup activation restart environment is invalid")
    result = {
        "PPBASE_DATABASE_URL": _require_text(
            str(value.get("PPBASE_DATABASE_URL", "")),
            label="activation database URL",
        )
    }
    if "PPBASE_BACKUP_DUMP_DATABASE_URL" in value:
        result["PPBASE_BACKUP_DUMP_DATABASE_URL"] = _require_text(
            str(value.get("PPBASE_BACKUP_DUMP_DATABASE_URL", "")),
            label="activation dump database URL",
        )
    return result


def activation_restart_spec(
    state: Mapping[str, Any],
) -> tuple[list[str], dict[str, str]]:
    return activation_restart_command(state), activation_restart_environment(state)


def note_local_activation_start(activation_id: str) -> None:
    global _LOCAL_STARTING_ACTIVATION_ID
    _LOCAL_STARTING_ACTIVATION_ID = _require_id(
        activation_id,
        label="activation ID",
    )


def is_local_activation_start(activation_id: str) -> bool:
    try:
        normalized = _require_id(activation_id, label="activation ID")
    except ActivationError:
        return False
    return _LOCAL_STARTING_ACTIVATION_ID == normalized


def clear_local_activation_start(activation_id: str | None = None) -> None:
    global _LOCAL_STARTING_ACTIVATION_ID
    if activation_id is None or _LOCAL_STARTING_ACTIVATION_ID == activation_id:
        _LOCAL_STARTING_ACTIVATION_ID = None


def begin_activation_startup(settings: Any) -> dict[str, Any] | None:
    """Commit ``starting`` before any target database probe in this process."""
    state = apply_activation_runtime_overlay(settings)
    if state is None:
        return None
    if state.get("status") != "restart_scheduled":
        return state
    control_path = Path(str(getattr(settings, "backup_control_dir", ""))).expanduser()
    if not control_path.is_absolute():
        control_path = Path(os.path.abspath(os.fspath(control_path)))
    root = ControlPlaneRoot.open(control_path, create_missing=False)
    store = BackupActivationStore(root)
    try:
        activation_id = str(state.get("activationId", ""))
        starting = store.mark_starting(activation_id)
        note_local_activation_start(activation_id)
        return starting
    finally:
        store.close()
        root.close()
