"""Health check API route."""

from __future__ import annotations

from fastapi import APIRouter

from ppbase import __version__
from ppbase.services.process_control import can_self_restart

router = APIRouter()


@router.get("")
async def health_check():
    """Return a simple health status."""
    return {"code": 200, "message": "API is healthy.", "data": {"version": __version__}}


@router.get("/backup-activation")
async def backup_activation_health():
    """Expose only the live process activation capability, without secrets."""
    return {
        "code": 200,
        "message": "Backup activation capability inspected.",
        "data": {"activation": {"configured": can_self_restart()}},
    }
