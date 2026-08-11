"""Process restart helpers for dashboard/admin controls."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RESTART_CMD_ENV = "PBBASE_RESTART_CMD"
_restart_lock = threading.Lock()
_restart_scheduled = False
_restart_reservation: "ProcessRestartReservation | None" = None


class ProcessRestartReservation:
    """Single-flight restart slot reserved before a durable cutover commit."""

    def __init__(self) -> None:
        self._active = True
        self._claimed = False

    def release(self) -> None:
        """Return an unconsumed slot; consumed reservations are a no-op."""
        global _restart_reservation, _restart_scheduled
        with _restart_lock:
            if not self._active or _restart_reservation is not self:
                return
            self._active = False
            self._claimed = False
            _restart_reservation = None
            _restart_scheduled = False


def reserve_process_restart() -> ProcessRestartReservation | None:
    """Reserve restart single-flight before a destructive cutover commits."""
    global _restart_reservation, _restart_scheduled
    with _restart_lock:
        if _restart_scheduled:
            return None
        reservation = ProcessRestartReservation()
        _restart_reservation = reservation
        _restart_scheduled = True
        return reservation


def _claim_restart_reservation(
    reservation: ProcessRestartReservation,
) -> bool:
    with _restart_lock:
        if (
            not reservation._active
            or reservation._claimed
            or not _restart_scheduled
            or _restart_reservation is not reservation
        ):
            return False
        reservation._claimed = True
        return True


def _detach_restart_reservation(
    reservation: ProcessRestartReservation,
) -> None:
    """Transfer a successfully started reservation to the restart worker."""
    global _restart_reservation
    with _restart_lock:
        reservation._active = False
        reservation._claimed = False
        if _restart_reservation is reservation:
            _restart_reservation = None


def _clear_restart_scheduled() -> None:
    global _restart_reservation, _restart_scheduled
    with _restart_lock:
        if _restart_reservation is not None:
            _restart_reservation._active = False
            _restart_reservation._claimed = False
        _restart_reservation = None
        _restart_scheduled = False


def _normalize_command(value: object) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    command = [str(item) for item in value if str(item or "").strip()]
    return command or None


def _infer_restart_command() -> list[str] | None:
    """Best-effort restart command inference for script-based launches.

    This allows applications started as ``python main.py`` or ``python3 main.py``
    to restart without requiring explicit environment configuration.
    """
    main_module = sys.modules.get("__main__")
    main_file = str(getattr(main_module, "__file__", "") or "").strip()
    executable = str(sys.executable or "").strip()
    if not main_file or not executable:
        return None

    script_path = str(Path(main_file).resolve())
    argv = [str(item) for item in sys.argv[1:]]
    return [executable, script_path, *argv]


def get_restart_command() -> list[str] | None:
    """Return the configured restart command, if available."""
    raw = str(os.environ.get(_RESTART_CMD_ENV, "") or "").strip()
    if raw:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring invalid %s value", _RESTART_CMD_ENV)
        else:
            command = _normalize_command(value)
            if command is not None:
                return command

    return _infer_restart_command()


def can_self_restart() -> bool:
    """Return True when the current process has a restart command."""
    return get_restart_command() is not None


def is_restart_scheduled() -> bool:
    """Return True if a restart has already been scheduled."""
    with _restart_lock:
        return _restart_scheduled


def _validated_restart_command(command: Sequence[str] | None) -> list[str] | None:
    if command is None:
        return get_restart_command()
    if isinstance(command, (str, bytes)):
        return None
    normalized = [str(item) for item in command]
    if not normalized or any(not item or "\x00" in item for item in normalized):
        return None
    return normalized


def _restart_environment(overrides: Mapping[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    for key, value in (overrides or {}).items():
        normalized_key = str(key)
        normalized_value = str(value)
        if (
            not normalized_key
            or "=" in normalized_key
            or "\x00" in normalized_key
            or "\x00" in normalized_value
        ):
            raise ValueError("restart environment contains an unsafe entry")
        env[normalized_key] = normalized_value
    return env


def restart_process_now(
    reason: str,
    *,
    command: Sequence[str] | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> None:
    """Replace this process immediately with a validated restart command."""
    selected = _validated_restart_command(command)
    if not selected:
        raise RuntimeError("PPBase has no valid restart command")
    env = _restart_environment(env_overrides)
    logger.warning("Restarting PPBase process immediately: %s", reason)
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        sys.stderr.flush()
    except Exception:
        pass
    os.execvpe(selected[0], selected, env)


def schedule_process_restart(
    reason: str,
    *,
    delay_seconds: float = 0.35,
    command: Sequence[str] | None = None,
    env_overrides: Mapping[str, str] | None = None,
    on_failure: Callable[[BaseException], None] | None = None,
    reservation: ProcessRestartReservation | None = None,
    before_exec: Callable[[], None] | None = None,
) -> bool:
    """Schedule an in-place process restart using ``os.execvpe``.

    Returns ``True`` when a restart was scheduled, otherwise ``False`` if
    restart is unsupported or already pending.
    """
    selected = _validated_restart_command(command)
    if not selected:
        logger.warning("Cannot restart PPBase automatically: no restart command configured")
        return False

    if reservation is not None:
        if not _claim_restart_reservation(reservation):
            return False
    else:
        with _restart_lock:
            global _restart_scheduled
            if _restart_scheduled:
                return False
            _restart_scheduled = True

    try:
        env = _restart_environment(env_overrides)
    except ValueError:
        logger.exception("Cannot restart PPBase with an invalid environment")
        if reservation is None:
            _clear_restart_scheduled()
        return False

    def _restart_worker() -> None:
        logger.warning("Restarting PPBase process: %s", reason)
        time.sleep(max(0.0, delay_seconds))
        try:
            try:
                sys.stdout.flush()
            except Exception:
                pass
            try:
                sys.stderr.flush()
            except Exception:
                pass
            # This must be the final fallible callback before exec. In
            # particular, potentially blocking stream flushes happen first so
            # they cannot widen the verified cutover interval.
            if before_exec is not None:
                before_exec()
            os.execvpe(selected[0], selected, env)
        except Exception as exc:
            logger.exception("Failed to restart PPBase process")
            failure_settled = True
            try:
                if on_failure is not None:
                    try:
                        on_failure(exc)
                    except Exception:
                        failure_settled = False
                        logger.exception(
                            "Failed to persist the rejected process restart outcome"
                        )
            finally:
                if failure_settled:
                    _clear_restart_scheduled()

    try:
        thread = threading.Thread(
            target=_restart_worker,
            name="ppbase-restart",
            daemon=True,
        )
        thread.start()
    except Exception:
        if reservation is None:
            _clear_restart_scheduled()
        logger.exception("Failed to start the PPBase restart worker")
        return False
    if reservation is not None:
        _detach_restart_reservation(reservation)
    return True




def serialize_restart_state() -> dict[str, Any]:
    """Return restart capability state for admin APIs."""
    return {
        "canRestart": can_self_restart(),
        "restartPending": is_restart_scheduled(),
    }
