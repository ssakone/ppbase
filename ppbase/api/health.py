"""Health check API route."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("")
async def health_check():
    """Return a simple health status."""
    return {"code": 200, "message": "API is healthy.", "data": {}}
