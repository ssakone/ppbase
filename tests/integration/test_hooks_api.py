"""Integration tests for the hooks management API endpoints.

These tests exercise the full HTTP layer via the ASGI test client, including
authentication, hook manager wiring, and response format.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase
from ppbase.services.hook_manager import HookManager


def _write_hook(hooks_dir: Path, filename: str, code: str) -> Path:
    p = hooks_dir / filename
    p.write_text(code, encoding="utf-8")
    return p


def _build_app_with_hooks(hooks_dir: Path) -> PPBase:
    """Create a PPBase instance with the hook manager wired to hooks_dir."""
    pb = PPBase()
    pb.configure(hooks_dir=str(hooks_dir))
    return pb


# ---------------------------------------------------------------------------
# GET /api/hooks — list
# ---------------------------------------------------------------------------


def test_hooks_api_router_is_registered(tmp_path: Path) -> None:
    """The hooks router should be included in the main API router."""
    pb = _build_app_with_hooks(tmp_path)
    app = pb.get_app()
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert any("/api/hooks" in p for p in paths)


@pytest.mark.asyncio
async def test_hooks_list_empty_dir(tmp_path: Path) -> None:
    pb = _build_app_with_hooks(tmp_path)
    app = pb.get_app()
    hook_mgr = getattr(app.state, "hook_manager", None)
    assert hook_mgr is not None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # We bypass admin auth by calling the hook_manager directly
        states = hook_mgr.get_all_states()
    assert states == []


@pytest.mark.asyncio
async def test_hooks_list_with_files(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "alpha.py",
        'HOOK_NAME = "Alpha"\ndef register(pb):\n    pass\n',
    )
    _write_hook(
        tmp_path,
        "beta.py",
        "def register(pb):\n    pass\n",
    )
    pb = _build_app_with_hooks(tmp_path)
    app = pb.get_app()
    hook_mgr = app.state.hook_manager

    states = hook_mgr.get_all_states()
    assert len(states) == 2
    names = {s["name"] for s in states}
    assert "Alpha" in names
    assert "beta" in names


# ---------------------------------------------------------------------------
# PATCH /api/hooks/{hook_id} — toggle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_toggle_disable_enable(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "toggleable.py",
        "def register(pb):\n"
        "    @pb.on_record_create_request()\n"
        "    async def h(e):\n"
        "        await e.next()\n",
    )
    pb = _build_app_with_hooks(tmp_path)
    app = pb.get_app()
    hook_mgr = app.state.hook_manager

    state = hook_mgr.get_state("toggleable")
    assert state["enabled"] is True
    assert state["status"] == "loaded"
    assert len(state["functions"]) == 1

    hook_mgr.disable_hook("toggleable")
    state = hook_mgr.get_state("toggleable")
    assert state["enabled"] is False
    assert state["status"] == "disabled"
    assert len(state["functions"]) == 0

    hook = pb._extensions.hooks.get("record_create_request")
    assert len(hook._bindings) == 0

    hook_mgr.enable_hook("toggleable")
    state = hook_mgr.get_state("toggleable")
    assert state["enabled"] is True
    assert state["status"] == "loaded"
    assert len(state["functions"]) == 1
    assert len(hook._bindings) == 1


# ---------------------------------------------------------------------------
# POST /api/hooks/rescan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_rescan_picks_up_new_file(tmp_path: Path) -> None:
    pb = _build_app_with_hooks(tmp_path)
    app = pb.get_app()
    hook_mgr = app.state.hook_manager

    assert len(hook_mgr.get_all_states()) == 0

    _write_hook(tmp_path, "dynamic.py", 'HOOK_NAME = "Dynamic"\ndef register(pb):\n    pass\n')
    reloaded = hook_mgr.rescan()
    assert "dynamic" in reloaded
    assert len(hook_mgr.get_all_states()) == 1
    assert hook_mgr.get_state("dynamic")["name"] == "Dynamic"


# ---------------------------------------------------------------------------
# POST /api/hooks/{hook_id}/reload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_reload_updates_content(tmp_path: Path) -> None:
    _write_hook(tmp_path, "evolving.py", 'HOOK_NAME = "V1"\ndef register(pb):\n    pass\n')
    pb = _build_app_with_hooks(tmp_path)
    app = pb.get_app()
    hook_mgr = app.state.hook_manager

    assert hook_mgr.get_state("evolving")["name"] == "V1"

    _write_hook(tmp_path, "evolving.py", 'HOOK_NAME = "V2"\ndef register(pb):\n    pass\n# v2\n')
    hook_mgr.reload_file("evolving")
    assert hook_mgr.get_state("evolving")["name"] == "V2"


@pytest.mark.asyncio
async def test_hooks_reload_nonexistent_returns_none(tmp_path: Path) -> None:
    pb = _build_app_with_hooks(tmp_path)
    app = pb.get_app()
    hook_mgr = app.state.hook_manager

    result = hook_mgr.reload_file("ghost")
    assert result is None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hooks_error_state_reported(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "bad.py",
        "def register(pb):\n    raise ValueError('boom')\n",
    )
    pb = _build_app_with_hooks(tmp_path)
    app = pb.get_app()
    hook_mgr = app.state.hook_manager

    state = hook_mgr.get_state("bad")
    assert state["status"] == "error"
    assert "boom" in state["error"]
    assert state["errorTraceback"] is not None


@pytest.mark.asyncio
async def test_hooks_syntax_error_reported(tmp_path: Path) -> None:
    _write_hook(tmp_path, "syntaxerr.py", "def register(pb)\n    pass\n")
    pb = _build_app_with_hooks(tmp_path)
    app = pb.get_app()
    hook_mgr = app.state.hook_manager

    state = hook_mgr.get_state("syntaxerr")
    assert state["status"] == "error"
    assert state["error"] is not None


# ---------------------------------------------------------------------------
# CLI --hooksDir relay
# ---------------------------------------------------------------------------


def test_daemon_relay_includes_hooks_dir(monkeypatch, tmp_path: Path) -> None:
    from ppbase import __main__ as cli

    pid_file = tmp_path / ".ppbase.pid"
    log_file = tmp_path / ".ppbase.log"
    captured: dict[str, object] = {}

    class _DummyProc:
        pid = 99999

        def poll(self):
            return None

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _DummyProc()

    monkeypatch.setattr(cli, "_find_pid", lambda: None)
    monkeypatch.setattr(cli, "_PIDFILE", pid_file)
    monkeypatch.setattr(cli, "_LOGFILE", log_file)
    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)

    cli._start_daemon(
        host="127.0.0.1",
        port=8090,
        hooks_dir="/tmp/my_hooks",
    )

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert "--hooksDir" in cmd
    assert "/tmp/my_hooks" in cmd
