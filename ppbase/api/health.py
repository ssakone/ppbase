"""Health check API route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ppbase import __version__
from ppbase.backup.operations import backup_operation_available
from ppbase.services.process_control import can_self_restart

router = APIRouter()


@router.get("")
async def health_check(request: Request):
    """Return a simple health status."""
    return {
        "code": 200,
        "message": "API is healthy.",
        "data": {
            "version": __version__,
            "canBackup": backup_operation_available(
                request.app.state.settings.data_dir
            ),
        },
    }


@router.get("/backup-restart")
async def backup_restart_health():
    """Expose only the live process restart capability, without secrets."""
    return {
        "code": 200,
        "message": "Backup restart capability inspected.",
        "data": {"restart": {"configured": can_self_restart()}},
    }


@router.get("/backup-activation", include_in_schema=False, deprecated=True)
async def legacy_backup_activation_health():
    """Retain the pre-0.3 health contract for old external probes only."""
    return {
        "code": 200,
        "message": "Backup activation capability inspected.",
        "data": {"activation": {"configured": can_self_restart()}},
    }
