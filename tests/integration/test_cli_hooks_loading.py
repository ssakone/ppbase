from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase
from ppbase import __main__ as cli


def _load_deferred_extensions(app: object) -> None:
    state = app.state  # type: ignore[attr-defined]
    loader = state.deferred_extension_loader
    assert callable(loader)
    loader()
    state.extension_registry.mount_routes(
        app,
        start_index=state.extension_routes_mounted,
    )
    state.extension_routes_mounted = state.extension_registry.route_count
    state.deferred_extension_loader = None


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
    _load_deferred_extensions(app)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ext/cli-loaded")

    assert response.status_code == 200
    assert response.json() == {"loaded": True}


def test_load_hooks_errors_are_explicit() -> None:
    app_pb = PPBase()

    with pytest.raises(ValueError, match="module:function"):
        app_pb.load_hooks("invalid-target")

    app_pb = PPBase()
    app_pb.load_hooks("missing_module_xyz:register")
    app = app_pb.get_app()
    with pytest.raises(ImportError, match="Failed to import hook module"):
        _load_deferred_extensions(app)

    app_pb = PPBase()
    app_pb.load_hooks("ppbase.config:missing_register")
    app = app_pb.get_app()
    with pytest.raises(AttributeError, match="not found"):
        _load_deferred_extensions(app)


def test_load_hooks_queues_user_import_until_app_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "imported.txt"
    module_name = "tmp_cli_hooks_deferred"
    (tmp_path / f"{module_name}.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')",
                "def register(app_pb):",
                "    return None",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    app_pb = PPBase()
    app_pb.load_hooks(f"{module_name}:register")

    assert not marker.exists()
    app = app_pb.get_app()
    assert not marker.exists()
    _load_deferred_extensions(app)
    assert marker.read_text(encoding="utf-8") == "imported"


def test_file_hook_import_is_deferred_until_startup_loader(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "file-hook-imported.txt"
    hooks_dir = tmp_path / "pb_hooks"
    hooks_dir.mkdir()
    (hooks_dir / "early.py").write_text(
        "\n".join(
            [
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')",
                "def register(app_pb):",
                "    @app_pb.get('/ext/deferred-file-hook')",
                "    async def deferred_file_hook():",
                "        return {'loaded': True}",
            ]
        ),
        encoding="utf-8",
    )
    app_pb = PPBase(hooks_dir=str(hooks_dir))
    app = app_pb.get_app()

    assert not marker.exists()
    _load_deferred_extensions(app)

    assert marker.read_text(encoding="utf-8") == "imported"


def test_daemon_relay_includes_all_hooks_and_dirs(monkeypatch, tmp_path: Path) -> None:
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
        data_dir="/tmp/pb_data_test",
        public_dir="/tmp/pb_public_test",
        migrations_dir="/tmp/pb_migrations_test",
        hooks=["pkg.mod:register_one", "pkg.mod:register_two"],
        automigrate=False,
    )

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    assert cmd.count("--hooks") == 2
    assert "pkg.mod:register_one" in cmd
    assert "pkg.mod:register_two" in cmd
    assert "--dir" in cmd
    assert "/tmp/pb_data_test" in cmd
    assert "--publicDir" in cmd
    assert "/tmp/pb_public_test" in cmd
    assert "--migrationsDir" in cmd
    assert "/tmp/pb_migrations_test" in cmd
    assert "--no-automigrate" in cmd
