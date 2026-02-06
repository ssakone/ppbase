"""File serving API route.

Endpoint:
    GET /api/files/{collectionIdOrName}/{recordId}/{filename}
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, get_settings, resolve_collection

router = APIRouter()


@router.get("/{collection_id_or_name}/{record_id}/{filename}")
async def serve_file(
    collection_id_or_name: str,
    record_id: str,
    filename: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Any = Depends(get_settings),
):
    """Serve a file from local storage."""
    collection = await resolve_collection(session, collection_id_or_name)
    data_dir = settings.data_dir

    file_path = (
        Path(data_dir)
        / "storage"
        / collection.id
        / record_id
        / filename
    )

    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail={
                "status": 404,
                "message": "The requested file wasn't found.",
                "data": {},
            },
        )

    return FileResponse(str(file_path), filename=filename)
