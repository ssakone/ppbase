"""Request Logs API routes.

Endpoints:
    GET   /api/logs         -> paginated list of request logs (admin only)
    GET   /api/logs/stats   -> aggregate stats (admin only)
    GET   /api/logs/{id}    -> single log entry (admin only)
    DELETE /api/logs/{id}   -> delete log entry (admin only)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, require_admin
from ppbase.db.system_tables import RequestLogRecord

router = APIRouter(prefix="/logs")


def _serialize(row: RequestLogRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "url": row.url,
        "method": row.method,
        "status": row.status,
        "execTime": row.exec_time,
        "remoteIp": row.remote_ip or "",
        "referer": row.referer or "",
        "userAgent": row.user_agent or "",
        "meta": row.meta or {},
        "created": row.created.isoformat() if row.created else "",
        "updated": row.updated.isoformat() if row.updated else "",
    }


@router.get("/stats")
async def get_log_stats(
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Return aggregate request log statistics."""
    total = (await session.execute(select(func.count()).select_from(RequestLogRecord))).scalar_one()
    errors = (
        await session.execute(
            select(func.count()).select_from(RequestLogRecord).where(RequestLogRecord.status >= 400)
        )
    ).scalar_one()
    avg_ms = (
        await session.execute(select(func.avg(RequestLogRecord.exec_time)).select_from(RequestLogRecord))
    ).scalar_one()
    unique_ips = (
        await session.execute(
            select(func.count(func.distinct(RequestLogRecord.remote_ip))).select_from(RequestLogRecord)
        )
    ).scalar_one()

    return {
        "total": total,
        "errors": errors,
        "avgMs": round(avg_ms or 0, 2),
        "uniqueIps": unique_ips,
    }


@router.get("")
async def list_logs(
    page: int = 1,
    perPage: int = 30,
    filter: str = "",
    sort: str = "-created",
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Return a paginated list of request logs."""
    page = max(1, page)
    perPage = min(max(1, perPage), 200)

    q = select(RequestLogRecord)

    # Simple filter support: method=GET, status=200, url~"/api/"
    if filter:
        conditions = _parse_filter(filter)
        for cond in conditions:
            q = q.where(cond)

    # Sort
    if sort.startswith("-"):
        col = _sort_col(sort[1:])
        if col is not None:
            q = q.order_by(col.desc())
    else:
        col = _sort_col(sort)
        if col is not None:
            q = q.order_by(col.asc())

    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await session.execute(q.offset((page - 1) * perPage).limit(perPage))).scalars().all()

    total_pages = max(1, (total + perPage - 1) // perPage)

    return {
        "page": page,
        "perPage": perPage,
        "totalItems": total,
        "totalPages": total_pages,
        "items": [_serialize(r) for r in rows],
    }


@router.get("/{log_id}")
async def get_log(
    log_id: str,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Return a single request log entry."""
    row = await session.get(RequestLogRecord, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"status": 404, "message": "Log not found.", "data": {}})
    return _serialize(row)


@router.delete("/{log_id}")
async def delete_log(
    log_id: str,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Delete a single request log entry."""
    row = await session.get(RequestLogRecord, log_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"status": 404, "message": "Log not found.", "data": {}})
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sort_col(name: str):
    mapping = {
        "created": RequestLogRecord.created,
        "updated": RequestLogRecord.updated,
        "status": RequestLogRecord.status,
        "method": RequestLogRecord.method,
        "exec_time": RequestLogRecord.exec_time,
        "execTime": RequestLogRecord.exec_time,
        "url": RequestLogRecord.url,
        "remote_ip": RequestLogRecord.remote_ip,
    }
    return mapping.get(name)


def _parse_filter(filter_str: str):
    """Very simple filter parser for common log fields.

    Supports:  method="GET"  status=200  url~"/api/"
    """
    import re
    from sqlalchemy import cast, String

    conditions = []
    # method="GET" or method='GET'
    m = re.search(r'method\s*=\s*["\']?(\w+)["\']?', filter_str, re.IGNORECASE)
    if m:
        conditions.append(RequestLogRecord.method == m.group(1).upper())

    # status=200 or status>=400
    m = re.search(r'status\s*(>=|<=|!=|>|<|=)\s*(\d+)', filter_str)
    if m:
        op, val = m.group(1), int(m.group(2))
        col = RequestLogRecord.status
        ops = {"=": col == val, ">": col > val, "<": col < val,
               ">=": col >= val, "<=": col <= val, "!=": col != val}
        if op in ops:
            conditions.append(ops[op])

    # url~"/api/"  (contains)
    m = re.search(r'url\s*~\s*["\']([^"\']+)["\']', filter_str)
    if m:
        conditions.append(RequestLogRecord.url.ilike(f"%{m.group(1)}%"))

    return conditions
