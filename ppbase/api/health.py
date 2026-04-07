"""Health check API route."""

from __future__ import annotations

from fastapi import APIRouter

from ppbase import __version__

router = APIRouter()


@router.get("")
async def health_check():
    """Return a simple health status."""
    return {"code": 200, "message": "API is healthy.", "data": {"version": __version__}}
