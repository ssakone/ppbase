"""Hooks management API routes.

Endpoints:
    GET   /api/hooks              -> list all hook files with status (admin only)
    GET   /api/hooks/{hook_id}    -> single hook detail (admin only)
    PATCH /api/hooks/{hook_id}    -> enable/disable a hook file (admin only)
    POST  /api/hooks/rescan       -> rescan hooks directory (admin only)
    POST  /api/hooks/{hook_id}/reload -> reload a specific hook file (admin only)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ppbase.api.deps import get_session, require_admin
from ppbase.core.id_generator import generate_id
from ppbase.db.system_tables import ParamRecord
from ppbase.services.process_control import schedule_process_restart, serialize_restart_state

router = APIRouter(prefix="/hooks")

_HOOKS_RUNTIME_KEY = "hooks_runtime"


def _get_hook_manager(request: Request):
    hook_manager = getattr(request.app.state, "hook_manager", None)
    if hook_manager is None:
        raise HTTPException(
            status_code=503,
            detail={
                "status": 503,
                "message": "Hook manager is not available.",
                "data": {},
            },
        )
    return hook_manager


async def _get_hooks_runtime_value(session: AsyncSession) -> dict[str, Any]:
    q = select(ParamRecord).where(ParamRecord.key == _HOOKS_RUNTIME_KEY)
    row = (await session.execute(q)).scalars().first()
    if row is None or not isinstance(row.value, dict):
        return {"disabled": [], "autoRestartOnChange": False}
    value = dict(row.value)
    disabled = value.get("disabled", [])
    if not isinstance(disabled, list):
        disabled = []
    return {
        "disabled": [str(item) for item in disabled if str(item).strip()],
        "autoRestartOnChange": bool(value.get("autoRestartOnChange", False)),
    }


async def _persist_hooks_runtime(
    session: AsyncSession,
    *,
    disabled: set[str] | None = None,
    auto_restart_on_change: bool | None = None,
) -> dict[str, Any]:
    current = await _get_hooks_runtime_value(session)
    if disabled is not None:
        current["disabled"] = sorted(disabled)
    if auto_restart_on_change is not None:
        current["autoRestartOnChange"] = bool(auto_restart_on_change)

    q = select(ParamRecord).where(ParamRecord.key == _HOOKS_RUNTIME_KEY)
    row = (await session.execute(q)).scalars().first()
    if row is None:
        row = ParamRecord(
            id=generate_id(),
            key=_HOOKS_RUNTIME_KEY,
            value=current,
        )
        session.add(row)
    else:
        row.value = current
    await session.flush()
    await session.commit()
    return current


@router.get("")
async def list_hooks(
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin: dict = Depends(require_admin),
):
    hook_manager = _get_hook_manager(request)
    runtime = await _get_hooks_runtime_value(session)
    return {
        "items": hook_manager.get_all_states(),
        "hooksDir": str(hook_manager.hooks_dir),
        "runtime": {
            **runtime,
            **serialize_restart_state(),
        },
    }


@router.patch("/runtime")
async def update_hooks_runtime(
    request: Request,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    hook_manager = _get_hook_manager(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"status": 400, "message": "Invalid request body.", "data": {}},
        )

    auto_restart = body.get("autoRestartOnChange")
    if auto_restart is None:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "Missing 'autoRestartOnChange' field.",
                "data": {},
            },
        )

    hook_manager.set_auto_restart_on_change(bool(auto_restart))
    runtime = await _persist_hooks_runtime(
        session,
        auto_restart_on_change=bool(auto_restart),
    )
    return {
        **runtime,
        **serialize_restart_state(),
    }


@router.post("/restart", status_code=202)
async def restart_ppbase(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    _get_hook_manager(request)
    scheduled = schedule_process_restart("Requested from Hooks dashboard")
    if not scheduled:
        raise HTTPException(
            status_code=409,
            detail={
                "status": 409,
                "message": "PPBase restart is unavailable or already pending.",
                "data": serialize_restart_state(),
            },
        )
    return {
        "scheduled": True,
        **serialize_restart_state(),
    }


@router.get("/{hook_id}")
async def get_hook(
    hook_id: str,
    request: Request,
    _admin: dict = Depends(require_admin),
):
    hook_manager = _get_hook_manager(request)
    state = hook_manager.get_state(hook_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"status": 404, "message": "Hook not found.", "data": {}},
        )
    return state


@router.patch("/{hook_id}")
async def update_hook(
    hook_id: str,
    request: Request,
    _admin: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    hook_manager = _get_hook_manager(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"status": 400, "message": "Invalid request body.", "data": {}},
        )

    enabled = body.get("enabled")
    if enabled is None:
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "Missing 'enabled' field.",
                "data": {},
            },
        )

    if enabled:
        state = hook_manager.enable_hook(hook_id)
    else:
        state = hook_manager.disable_hook(hook_id)

    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"status": 404, "message": "Hook not found.", "data": {}},
        )

    await _persist_hooks_runtime(session, disabled=hook_manager.get_disabled_hooks())

    return hook_manager.get_state(hook_id)


@router.post("/rescan", status_code=200)
async def rescan_hooks(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    hook_manager = _get_hook_manager(request)
    reloaded = hook_manager.rescan()
    if reloaded and hook_manager.get_auto_restart_on_change():
        schedule_process_restart(f"Requested after manual hooks rescan: {', '.join(reloaded)}")
    return {
        "reloaded": reloaded,
        "items": hook_manager.get_all_states(),
    }


@router.post("/{hook_id}/reload", status_code=200)
async def reload_hook(
    hook_id: str,
    request: Request,
    _admin: dict = Depends(require_admin),
):
    hook_manager = _get_hook_manager(request)
    state = hook_manager.reload_file(hook_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail={"status": 404, "message": "Hook not found.", "data": {}},
        )
    if hook_manager.get_auto_restart_on_change():
        schedule_process_restart(f"Requested after manual hook reload: {hook_id}")
    return hook_manager.get_state(hook_id)
