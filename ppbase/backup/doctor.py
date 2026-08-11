"""Runtime diagnostics for native backup and destructive restore."""

from __future__ import annotations

import asyncio
import http.client
import json
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from ppbase.backup.postgres import (
    preflight_destructive_restore_role,
    set_backup_control_search_path,
)
from ppbase.services.process_control import can_self_restart


DOCTOR_EXIT_READY = 0
DOCTOR_EXIT_NOT_READY = 2


class BackupDoctorError(RuntimeError):
    """Raised when the doctor command cannot inspect its requested target."""


def _backup_root_check(data_dir: str | Path) -> dict[str, Any]:
    """Inspect ``pb_data/backups`` without creating or normalizing it."""
    selected = Path(data_dir).expanduser().resolve(strict=False) / "backups"
    try:
        data_path = Path(data_dir).expanduser().resolve(strict=False)
        if not data_path.exists():
            return {
                "name": "backup_root",
                "ready": True,
                "status": "warn",
                "detail": "pb_data/backups will be created with pb_data",
                "path": str(selected),
            }
        data_info = data_path.stat()
        try:
            info = selected.lstat()
        except FileNotFoundError:
            return {
                "name": "backup_root",
                "ready": True,
                "status": "warn",
                "detail": "directory is absent and will be created on startup",
                "path": str(selected),
            }
        ready = (
            not selected.is_symlink()
            and stat.S_ISDIR(info.st_mode)
            and info.st_uid == data_info.st_uid
        )
        detail = (
            "pb_data/backups directory available"
            if ready
            else "pb_data/backups is unsafe or not owned like pb_data"
        )
    except OSError:
        ready = False
        detail = "pb_data/backups is inaccessible"
    return {
        "name": "backup_root",
        "ready": ready,
        "status": "pass" if ready else "fail",
        "detail": detail,
        "path": str(selected),
    }


async def _postgres_server_identity(connection: AsyncConnection) -> dict[str, Any]:
    """Return non-secret fields describing the connected database instance."""
    await set_backup_control_search_path(connection)
    row = (
        await connection.execute(
            text(
                "SELECT pg_catalog.current_database() AS database, "
                "pg_catalog.current_setting('server_version_num')::integer AS version"
            )
        )
    ).mappings().one()
    return {
        "database": str(row["database"]),
        "version": int(row["version"]),
    }


def _probe_server_restart(server_url: str) -> dict[str, Any]:
    """Probe the live process without requiring a Dashboard credential."""
    parsed = urllib.parse.urlsplit(str(server_url).strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise BackupDoctorError(
            "--server must be an http(s) origin without credentials, query, or path"
        )
    endpoint = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/api/health/backup-restart", "", "")
    )
    request = urllib.request.Request(
        endpoint,
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            status = int(getattr(response, "status", 0))
            observed_url = (
                str(response.geturl())
                if callable(getattr(response, "geturl", None))
                else ""
            )
            if status < 200 or status >= 300 or observed_url != endpoint:
                raise ValueError("non-authoritative HTTP response")
            payload = response.read(64 * 1024 + 1)
            if len(payload) > 64 * 1024:
                raise ValueError("oversized response")
            decoded = json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {
            "name": "restart",
            "ready": False,
            "status": "fail",
            "detail": f"running PPBase server probe failed (HTTP {exc.code})",
        }
    except (
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        http.client.HTTPException,
    ):
        return {
            "name": "restart",
            "ready": False,
            "status": "fail",
            "detail": "running PPBase server is unreachable or returned invalid JSON",
        }
    try:
        if (
            not isinstance(decoded, dict)
            or decoded.get("code") != 200
            or decoded.get("message") != "Backup restart capability inspected."
        ):
            raise KeyError("invalid PPBase health envelope")
        configured = decoded["data"]["restart"]["configured"]
    except (KeyError, TypeError):
        configured = None
    if configured is True:
        return {
            "name": "restart",
            "ready": True,
            "status": "pass",
            "detail": "running PPBase server confirms destructive-restore restart support",
        }
    if configured is False:
        return {
            "name": "restart",
            "ready": False,
            "status": "fail",
            "detail": "running PPBase server confirms destructive-restore restart is unavailable",
        }
    return {
        "name": "restart",
        "ready": False,
        "status": "fail",
        "detail": "running server returned no authoritative restart capability",
    }


async def backup_doctor(
    settings: Any,
    *,
    server_url: str | None = None,
) -> dict[str, Any]:
    """Check the concrete runtime requirements for backup and restore."""
    checks: list[dict[str, Any]] = []
    storage_backend = str(getattr(settings, "storage_backend", "local") or "local")
    checks.append(
        {
            "name": "storage",
            "ready": storage_backend == "local",
            "detail": f"backend={storage_backend}",
        }
    )
    checks.append(_backup_root_check(settings.data_dir))
    if server_url:
        checks.append(await asyncio.to_thread(_probe_server_restart, server_url))
    else:
        restart_visible = can_self_restart()
        checks.append(
            {
                "name": "restart",
                "ready": True,
                "status": "warn" if restart_visible else "skip",
                "detail": (
                    "restart command is visible locally but does not prove the running server"
                    if restart_visible
                    else "not observable from a standalone process; use --server URL"
                ),
            }
        )

    runtime_engine = create_async_engine(
        str(settings.database_url),
        poolclass=NullPool,
    )
    try:
        async with runtime_engine.connect() as connection:
            identity = await _postgres_server_identity(connection)
            checks.append(
                {
                    "name": "runtime_database",
                    "ready": True,
                    "detail": (
                        f"{identity['database']} PostgreSQL {identity['version']}"
                    ),
                }
            )
            try:
                restore_report = await preflight_destructive_restore_role(connection)
            except Exception:
                checks.append(
                    {
                        "name": "restore_database",
                        "ready": False,
                        "detail": "destructive restore preflight failed",
                    }
                )
            else:
                if not restore_report.ok:
                    checks.append(
                        {
                            "name": "restore_database",
                            "ready": False,
                            "detail": "configured database cannot be restored in place",
                        }
                    )

            allowed_extensions = {
                tuple(str(item).split("=", 1))
                for item in (getattr(settings, "backup_allowed_extensions", ()) or ())
                if "=" in str(item)
            }
            installed_extensions = {
                (str(row["name"]), str(row["version"]))
                for row in (
                    await connection.execute(
                        text(
                            "SELECT e.extname AS name, e.extversion AS version "
                            "FROM pg_catalog.pg_extension AS e ORDER BY e.extname"
                        )
                    )
                ).mappings().all()
            }
            unsupported_extensions = sorted(
                installed_extensions - allowed_extensions
            )
            checks.append(
                {
                    "name": "extensions",
                    "ready": not unsupported_extensions,
                    "detail": (
                        "installed extensions match the allowlist"
                        if not unsupported_extensions
                        else "unsupported installed extensions: "
                        + ", ".join(
                            f"{name}={version}"
                            for name, version in unsupported_extensions
                        )
                    ),
                }
            )
            await connection.rollback()
    except Exception:
        checks.append(
            {
                "name": "runtime_database",
                "ready": False,
                "detail": "runtime database connection or inspection failed",
            }
        )
    finally:
        await runtime_engine.dispose()

    backup_checks = {
        "storage",
        "backup_root",
        "runtime_database",
        "extensions",
    }
    restore_checks = {
        "storage",
        "backup_root",
        "restart",
        "runtime_database",
        "restore_database",
        "extensions",
    }
    for check in checks:
        check.setdefault("status", "pass" if check.get("ready") else "fail")
        name = str(check.get("name", ""))
        check["operations"] = [
            operation
            for operation, names in (
                ("backup", backup_checks),
                ("restore", restore_checks),
            )
            if name in names
        ]

    def operation_ready(names: set[str]) -> bool:
        return all(
            str(check["status"]) != "fail"
            for check in checks
            if str(check.get("name", "")) in names
        )

    def operation_fully_verified(names: set[str]) -> bool:
        return all(
            str(check["status"]) == "pass"
            for check in checks
            if str(check.get("name", "")) in names
        )

    backup_ready = operation_ready(backup_checks)
    restore_ready = operation_ready(restore_checks)
    backup_fully_verified = operation_fully_verified(backup_checks)
    restore_fully_verified = operation_fully_verified(restore_checks)
    return {
        "formatVersion": 1,
        "ready": backup_ready,
        "fullyVerified": backup_fully_verified,
        "backupReady": backup_ready,
        "backupFullyVerified": backup_fully_verified,
        "restoreReady": restore_ready,
        "restoreFullyVerified": restore_fully_verified,
        "exitCode": DOCTOR_EXIT_READY if backup_ready else DOCTOR_EXIT_NOT_READY,
        "checks": checks,
        "commands": {
            "doctor": (
                f"ppbase backup doctor --server {server_url}"
                if server_url
                else "ppbase backup doctor"
            ),
        },
    }


def doctor_human(report: Mapping[str, Any]) -> str:
    """Render a compact human-readable doctor report."""
    lines = ["PPBase native backup doctor"]
    for check in report.get("checks", []):
        status = str(
            check.get("status")
            or ("pass" if check.get("ready") else "fail")
        ).upper()
        marker = "OK" if status == "PASS" else status
        lines.append(f"[{marker}] {check.get('name')}: {check.get('detail')}")
    backup_ready = bool(report.get("backupReady", report.get("ready")))
    restore_ready = bool(report.get("restoreReady", report.get("ready")))
    if not backup_ready:
        lines.append("Backup is not ready; fix the backup failures above.")
    elif not restore_ready:
        lines.append(
            "Backup is ready; destructive restore is not ready. "
            "Fix the restore-specific failures above."
        )
    elif (
        report.get("backupFullyVerified", report.get("fullyVerified", True))
        and report.get("restoreFullyVerified", report.get("fullyVerified", True))
    ):
        lines.append("Backup and destructive restore are ready.")
    else:
        lines.append("No confirmed blocker; readiness is partial.")
    return "\n".join(lines)
