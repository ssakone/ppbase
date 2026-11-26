"""SQL console API routes.

Endpoints:
    POST  /api/sql/execute  -> analyze and run a raw SQL script (admin only)

Scripts that alter, modify or delete data are not executed unless the request
sets ``confirm: true``; otherwise the response carries
``requiresConfirmation: true`` and the list of statements to confirm.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, require_admin
from ppbase.db.engine import get_engine
from ppbase.services.sql_console import DEFAULT_MAX_ROWS, SqlConsoleError, execute_script
from ppbase.services.write_barrier import WriteBarrierError

router = APIRouter(prefix="/sql")


class SqlExecuteBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    query: str = Field(max_length=1_000_000)
    confirm: bool = False
    max_rows: int = Field(default=DEFAULT_MAX_ROWS, ge=1, le=5000, alias="maxRows")


@router.post("/execute")
async def execute_sql(
    body: SqlExecuteBody,
    admin: dict[str, Any] = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Run a SQL script; mutating scripts require ``confirm: true``."""
    # The script runs on its own dedicated connection; return the auth
    # transaction to the pool first so this also works with pool_size=1.
    await session.rollback()

    try:
        return await execute_script(
            get_engine(),
            body.query,
            confirm=body.confirm,
            max_rows=body.max_rows,
            actor=str(admin.get("email") or admin.get("id") or ""),
        )
    except SqlConsoleError as exc:
        raise HTTPException(
            status_code=400,
            detail={"status": 400, "message": exc.message, "data": exc.data},
        )
    except WriteBarrierError as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": 503, "message": str(exc), "data": {}},
        )
