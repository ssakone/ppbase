"""Process restart helpers for dashboard/admin controls."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RESTART_CMD_ENV = "PBBASE_RESTART_CMD"
_restart_lock = threading.Lock()
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


def schedule_process_restart(reason: str, *, delay_seconds: float = 0.35) -> bool:
    """Schedule an in-place process restart using ``os.execvpe``.

    Returns ``True`` when a restart was scheduled, otherwise ``False`` if
    restart is unsupported or already pending.
    """
    command = get_restart_command()
    if not command:
        logger.warning("Cannot restart PPBase automatically: no restart command configured")
        return False

    with _restart_lock:
        global _restart_scheduled
        if _restart_scheduled:
            return False
        _restart_scheduled = True

    env = os.environ.copy()

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
            os.execvpe(command[0], command, env)
        except Exception:
            logger.exception("Failed to restart PPBase process")
            with _restart_lock:
                global _restart_scheduled
                _restart_scheduled = False

    thread = threading.Thread(target=_restart_worker, name="ppbase-restart", daemon=True)
    thread.start()
    return True


def serialize_restart_state() -> dict[str, Any]:
    """Return restart capability state for admin APIs."""
    return {
        "canRestart": can_self_restart(),
        "restartPending": is_restart_scheduled(),
    }
