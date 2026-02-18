from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase
from ppbase import __main__ as cli


@pytest.mark.asyncio
async def test_load_hooks_module_function_target(tmp_path: Path, monkeypatch) -> None:
    module_name = "tmp_cli_hooks_ok"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(
        "\n".join(
            [
                "def register(app_pb):",
                "    @app_pb.get('/ext/cli-loaded')",
                "    async def _route():",
                "        return {'loaded': True}",
            ]
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    app_pb = PPBase()
    app_pb.load_hooks(f"{module_name}:register")
    app = app_pb.get_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/cli-loaded")

    assert response.status_code == 200
    assert response.json() == {"loaded": True}


def test_load_hooks_errors_are_explicit() -> None:
    app_pb = PPBase()

    with pytest.raises(ValueError, match="module:function"):
        app_pb.load_hooks("invalid-target")

    with pytest.raises(ImportError, match="Failed to import hook module"):
        app_pb.load_hooks("missing_module_xyz:register")

    with pytest.raises(AttributeError, match="not found"):
        app_pb.load_hooks("ppbase.config:missing_register")


def test_daemon_relay_includes_all_hooks(monkeypatch, tmp_path: Path) -> None:
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
        hooks=["pkg.mod:register_one", "pkg.mod:register_two"],
        automigrate=False,
    )

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd.count("--hooks") == 2
    assert "pkg.mod:register_one" in cmd
    assert "pkg.mod:register_two" in cmd
    assert "--no-automigrate" in cmd
