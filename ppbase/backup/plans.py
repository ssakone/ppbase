"""Filesystem control-plane store for immutable restore staging plans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import fcntl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ppbase.backup.models import (
    BackupManifestError,
    canonical_json_bytes,
    validate_backup_id,
)


_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_PLAN_DOMAIN = b"PPBASE-RESTORE-STAGING-PLAN-V1\0"
_TERMINAL_STATUS_FILENAME = "terminal.json"
_MAX_PLAN_JSON_BYTES = 64 * 1024
JwtSecretMode = Literal["disaster_recovery", "clone"]


class StagingPlanError(RuntimeError):
    """Raised for unsafe, missing, or non-executable staging plans."""


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return canonical_json_bytes(value)
    except BackupManifestError as exc:
        raise StagingPlanError(str(exc)) from exc


def _write_exclusive(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise StagingPlanError(f"Duplicate JSON key {key!r}.")
            result[key] = value
        return result

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
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
    return value


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


class StagingPlanStore:
    """Persist immutable plans and mutable execution status outside the DB."""

    def __init__(self, control_dir: str | Path, staging_root: str | Path):
        self.control_dir = Path(control_dir).expanduser().resolve(strict=False)
        self.staging_root = Path(staging_root).expanduser().resolve(strict=False)
        self.plans_dir = self.control_dir / "plans"
        self.plans_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.plans_dir, 0o700)
        self._execution_leases: dict[str, tuple[str, int]] = {}

    def create(
        self,
        *,
        backup_id: str,
        manifest_sha256: str,
        destination_fingerprint_sha256: str,
        jwt_secret_mode: str,
        actor_id: str | None,
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
        target_data_dir = self.staging_root / plan_id / "data"
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        immutable = {
            "formatVersion": 1,
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

        plan_dir = self.plans_dir / plan_id
        plan_dir.mkdir(mode=0o700)
        try:
            _write_exclusive(plan_dir / "plan.json", _canonical_json_bytes(payload))
            _write_exclusive(
                plan_dir / "status.json",
                _canonical_json_bytes({"status": "planned"}),
            )
            _write_exclusive(plan_dir / "SEALED", b"sealed\n")
            _fsync_directory(plan_dir)
            _fsync_directory(self.plans_dir)
        except BaseException:
            # A failed unpublished plan is never returned or reused. It is safe
            # to leave forensic partial files under its random control-plane ID.
            raise
        return self.inspect(plan_id)

    def inspect(self, plan_id: str) -> StagingPlan:
        plan_dir = self._plan_dir(plan_id)
        if not self._is_private_regular_file(plan_dir / "SEALED"):
            raise StagingPlanError("The staging plan is not sealed.")
        payload = _read_json(plan_dir / "plan.json")
        terminal_path = plan_dir / _TERMINAL_STATUS_FILENAME
        running_path = plan_dir / "RUNNING"
        terminal_exists = self._path_exists(terminal_path)
        running_exists = self._path_exists(running_path)
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
            running_marker = _read_json(running_path)
            self._validate_running_marker(running_marker, declared_hash)
            if not terminal_exists and self._reconcile_orphaned_execution(
                plan_id,
                plan_dir,
                running_marker,
                payload,
            ):
                terminal_exists = self._path_exists(terminal_path)
        if (
            isinstance(payload.get("formatVersion"), bool)
            or payload.get("formatVersion") != 1
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
        expected_data_dir = self.staging_root / plan_id / "data"
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
            status = _read_json(terminal_path)
        elif running_exists:
            status = {
                "status": "running",
                "attemptId": str((running_marker or {}).get("attemptId", "")),
                "startedAt": str((running_marker or {}).get("startedAt", "")),
            }
        else:
            status = _read_json(plan_dir / "status.json")
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

    def begin_execution(self, plan_id: str, *, expected_plan_hash: str) -> StagingPlan:
        plan = self.inspect(plan_id)
        if plan.plan_hash != expected_plan_hash:
            raise StagingPlanError("The supplied planHash does not match the plan.")
        if plan.status != "planned":
            raise StagingPlanError(
                f"Staging plan is not executable from status {plan.status!r}."
            )
        plan_dir = self._plan_dir(plan_id)
        attempt_id = secrets.token_hex(16)
        lease_name = f"attempt-{attempt_id}.lease"
        lease_path = plan_dir / lease_name
        _write_exclusive(lease_path, b"lease\n")
        lease_fd = os.open(
            lease_path,
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        lease_registered = False
        try:
            lease_info = os.fstat(lease_fd)
            if (
                not stat.S_ISREG(lease_info.st_mode)
                or stat.S_IMODE(lease_info.st_mode) != 0o600
            ):
                raise StagingPlanError("The staging execution lease is unsafe.")
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._execution_leases[plan_id] = (attempt_id, lease_fd)
            lease_registered = True
            _write_exclusive(
                plan_dir / "RUNNING",
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
            _fsync_directory(plan_dir)
            return self.inspect(plan_id)
        except FileExistsError as exc:
            if lease_registered:
                self._release_execution_lease(plan_id, attempt_id)
            else:
                os.close(lease_fd)
            raise StagingPlanError("The staging plan was already executed.") from exc
        except BaseException:
            if lease_registered:
                self._release_execution_lease(plan_id, attempt_id)
            else:
                os.close(lease_fd)
            raise

    def finish(
        self,
        plan_id: str,
        *,
        status: Literal["validated", "failed", "quarantined"],
        expected_attempt_id: str,
        data: dict[str, Any] | None = None,
    ) -> StagingPlan:
        if status not in {"validated", "failed", "quarantined"}:
            raise StagingPlanError("Invalid terminal staging status.")
        plan = self.inspect(plan_id)
        if plan.status != "running":
            raise StagingPlanError(
                f"Staging plan cannot finish from status {plan.status!r}."
            )
        plan_dir = self._plan_dir(plan_id)
        owned = self._execution_leases.get(plan_id)
        if owned is None or owned[0] != expected_attempt_id:
            raise StagingPlanError(
                "The staging execution attempt is not owned by this process."
            )
        if not self._is_private_regular_file(plan_dir / "RUNNING"):
            raise StagingPlanError("The staging plan has not started.")
        running_marker = _read_json(plan_dir / "RUNNING")
        self._validate_running_marker(running_marker, plan.plan_hash)
        if running_marker["attemptId"] != expected_attempt_id:
            raise StagingPlanError("The staging execution attempt does not match.")
        try:
            _write_exclusive(
                plan_dir / _TERMINAL_STATUS_FILENAME,
                _canonical_json_bytes(
                    {
                        **(data or {}),
                        "attemptId": expected_attempt_id,
                        "status": status,
                    }
                ),
            )
        except FileExistsError as exc:
            raise StagingPlanError(
                "The staging plan already has a terminal status."
            ) from exc
        _fsync_directory(plan_dir)
        self._release_execution_lease(plan_id, expected_attempt_id)
        return self.inspect(plan_id)

    def abandon_execution(self, plan_id: str, *, expected_attempt_id: str) -> None:
        """Release a quiescent attempt whose terminal status could not persist."""
        owned = self._execution_leases.get(plan_id)
        if owned is None or owned[0] != expected_attempt_id:
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
        plan_dir: Path,
        marker: dict[str, Any],
        payload: dict[str, Any],
    ) -> bool:
        attempt_id = str(marker["attemptId"])
        owned = self._execution_leases.get(plan_id)
        if owned is not None and owned[0] == attempt_id:
            return False

        lease_fd: int | None = None
        try:
            lease_fd = os.open(
                plan_dir / str(marker["leaseFile"]),
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            info = os.fstat(lease_fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise StagingPlanError("The staging execution lease is unsafe.")
            try:
                fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        except FileNotFoundError:
            lease_fd = None

        try:
            try:
                _write_exclusive(
                    plan_dir / _TERMINAL_STATUS_FILENAME,
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
                _fsync_directory(plan_dir)
            except FileExistsError:
                pass
            return True
        finally:
            if lease_fd is not None:
                try:
                    fcntl.flock(lease_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lease_fd)

    def _release_execution_lease(self, plan_id: str, attempt_id: str) -> None:
        owned = self._execution_leases.get(plan_id)
        if owned is None or owned[0] != attempt_id:
            return
        _owned_attempt, descriptor = self._execution_leases.pop(plan_id)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _plan_dir(self, plan_id: str) -> Path:
        if not _ID_RE.fullmatch(str(plan_id)):
            raise StagingPlanError("Invalid staging plan ID.")
        path = self.plans_dir / plan_id
        try:
            info = path.lstat()
        except OSError as exc:
            raise StagingPlanError("Staging plan was not found.") from exc
        if (
            not stat.S_ISDIR(info.st_mode)
            or path.is_symlink()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise StagingPlanError("Staging plan was not found.")
        return path

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _is_private_regular_file(path: Path) -> bool:
        try:
            info = path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(info.st_mode)
            and not path.is_symlink()
            and stat.S_IMODE(info.st_mode) == 0o600
        )

    def _write_status(self, plan_dir: Path, value: dict[str, Any]) -> None:
        temporary = plan_dir / f".status-{secrets.token_hex(8)}.tmp"
        _write_exclusive(temporary, _canonical_json_bytes(value))
        os.replace(temporary, plan_dir / "status.json")
        _fsync_directory(plan_dir)
