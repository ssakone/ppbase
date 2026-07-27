"""Integration tests for the file-based hook manager (pb_hooks/)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ppbase import PPBase
from ppbase.ext.registry import ExtensionRegistry, HOOK_NAMES
from ppbase.services.hook_manager import (
    HookManager,
    _hook_id_from_filename,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_hook(hooks_dir: Path, filename: str, code: str) -> Path:
    p = hooks_dir / filename
    p.write_text(code, encoding="utf-8")
    return p


def _make_manager(hooks_dir: Path) -> tuple[HookManager, PPBase]:
    pb = PPBase()
    mgr = HookManager(hooks_dir, pb._extensions, pb)
    return mgr, pb


def _load_startup_extensions(app: object) -> None:
    loader = app.state.deferred_extension_loader  # type: ignore[attr-defined]
    assert callable(loader)
    loader()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_empty_dir(tmp_path: Path) -> None:
    mgr, _ = _make_manager(tmp_path)
    assert mgr.discover_files() == []


def test_discover_nonexistent_dir(tmp_path: Path) -> None:
    mgr, _ = _make_manager(tmp_path / "does_not_exist")
    assert mgr.discover_files() == []


def test_discover_ignores_underscored_and_init(tmp_path: Path) -> None:
    _write_hook(tmp_path, "__init__.py", "")
    _write_hook(tmp_path, "_private.py", "")
    _write_hook(tmp_path, "valid.py", "")
    _write_hook(tmp_path, "readme.txt", "")
    mgr, _ = _make_manager(tmp_path)
    assert mgr.discover_files() == ["valid.py"]


def test_discover_multiple_sorted(tmp_path: Path) -> None:
    _write_hook(tmp_path, "beta.py", "")
    _write_hook(tmp_path, "alpha.py", "")
    _write_hook(tmp_path, "gamma.py", "")
    mgr, _ = _make_manager(tmp_path)
    assert mgr.discover_files() == ["alpha.py", "beta.py", "gamma.py"]


# ---------------------------------------------------------------------------
# Hook ID derivation
# ---------------------------------------------------------------------------


def test_hook_id_from_filename() -> None:
    assert _hook_id_from_filename("my-hook.py") == "my_hook"
    assert _hook_id_from_filename("My Hook.py") == "my_hook"
    assert _hook_id_from_filename("simple.py") == "simple"


# ---------------------------------------------------------------------------
# Metadata parsing
# ---------------------------------------------------------------------------


def test_metadata_from_module_globals(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "audit.py",
        'HOOK_NAME = "Audit Logger"\n'
        'HOOK_DESCRIPTION = "Logs all record mutations"\n'
        "def register(pb):\n"
        "    pass\n",
    )
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()
    states = mgr.get_all_states()
    assert len(states) == 1
    assert states[0]["name"] == "Audit Logger"
    assert states[0]["description"] == "Logs all record mutations"
    assert states[0]["status"] == "loaded"


def test_metadata_fallback_to_filename(tmp_path: Path) -> None:
    _write_hook(tmp_path, "simple_hook.py", "def register(pb):\n    pass\n")
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()
    states = mgr.get_all_states()
    assert states[0]["name"] == "simple_hook"
    assert states[0]["description"] == ""


# ---------------------------------------------------------------------------
# Hook binding tracking
# ---------------------------------------------------------------------------


def test_register_records_bindings(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "hooks_a.py",
        "def register(pb):\n"
        "    @pb.on_record_create_request('posts')\n"
        "    async def on_create(event):\n"
        "        await event.next()\n"
        "    @pb.on_record_delete_request('posts')\n"
        "    async def on_delete(event):\n"
        "        await event.next()\n",
    )
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()

    states = mgr.get_all_states()
    assert len(states) == 1
    s = states[0]
    assert s["status"] == "loaded"
    assert len(s["functions"]) == 2
    hook_types = {f["hookType"] for f in s["functions"]}
    assert "record_create_request" in hook_types
    assert "record_delete_request" in hook_types
    handler_names = {f["handlerName"] for f in s["functions"]}
    assert "on_create" in handler_names
    assert "on_delete" in handler_names


# ---------------------------------------------------------------------------
# Reload without duplicate bindings
# ---------------------------------------------------------------------------


def test_reload_does_not_duplicate_bindings(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "counter.py",
        "def register(pb):\n"
        "    @pb.on_record_create_request('items')\n"
        "    async def on_create(event):\n"
        "        await event.next()\n",
    )
    mgr, pb = _make_manager(tmp_path)
    mgr.initial_load()

    hook = pb._extensions.hooks.get("record_create_request")
    count_before = len(hook._bindings)

    mgr.reload_file("counter")

    count_after = len(hook._bindings)
    assert count_after == count_before, (
        f"Expected {count_before} binding(s), got {count_after} after reload"
    )

    states = mgr.get_all_states()
    assert len(states[0]["functions"]) == 1


# ---------------------------------------------------------------------------
# Disable / enable
# ---------------------------------------------------------------------------


def test_disable_removes_bindings(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "toggle.py",
        "def register(pb):\n"
        "    @pb.on_record_view_request()\n"
        "    async def on_view(event):\n"
        "        await event.next()\n",
    )
    mgr, pb = _make_manager(tmp_path)
    mgr.initial_load()

    hook = pb._extensions.hooks.get("record_view_request")
    assert len(hook._bindings) == 1

    mgr.disable_hook("toggle")
    assert len(hook._bindings) == 0

    state = mgr.get_state("toggle")
    assert state is not None
    assert state["status"] == "disabled"
    assert state["enabled"] is False
    assert "toggle" in mgr.get_disabled_hooks()


def test_enable_reloads_hook(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "reenable.py",
        "def register(pb):\n"
        "    @pb.on_record_view_request()\n"
        "    async def on_view(event):\n"
        "        await event.next()\n",
    )
    mgr, pb = _make_manager(tmp_path)
    mgr.initial_load()

    mgr.disable_hook("reenable")
    hook = pb._extensions.hooks.get("record_view_request")
    assert len(hook._bindings) == 0

    mgr.enable_hook("reenable")
    assert len(hook._bindings) == 1
    state = mgr.get_state("reenable")
    assert state is not None
    assert state["status"] == "loaded"
    assert state["enabled"] is True


def test_initial_load_respects_disabled_set(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "skipped.py",
        "def register(pb):\n"
        "    @pb.on_record_create_request()\n"
        "    async def hook(e):\n"
        "        await e.next()\n",
    )
    mgr, pb = _make_manager(tmp_path)
    mgr.set_disabled_hooks({"skipped"})
    mgr.initial_load()

    hook = pb._extensions.hooks.get("record_create_request")
    assert len(hook._bindings) == 0

    state = mgr.get_state("skipped")
    assert state is not None
    assert state["status"] == "disabled"


# ---------------------------------------------------------------------------
# Import / register errors
# ---------------------------------------------------------------------------


def test_import_error_captures_traceback(tmp_path: Path) -> None:
    _write_hook(tmp_path, "broken_import.py", "import nonexistent_module_xyz\n")
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()

    state = mgr.get_state("broken_import")
    assert state is not None
    assert state["status"] == "error"
    assert "nonexistent_module_xyz" in (state["error"] or "")
    assert state["errorTraceback"] is not None
    assert len(state["errorTraceback"]) > 0


def test_register_error_captures_traceback(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "broken_register.py",
        "def register(pb):\n    raise RuntimeError('intentional failure')\n",
    )
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()

    state = mgr.get_state("broken_register")
    assert state is not None
    assert state["status"] == "error"
    assert "intentional failure" in (state["error"] or "")
    assert state["errorTraceback"] is not None


# ---------------------------------------------------------------------------
# Rescan: detect new/removed/changed files
# ---------------------------------------------------------------------------


def test_rescan_detects_new_file(tmp_path: Path) -> None:
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()
    assert len(mgr.get_all_states()) == 0

    _write_hook(tmp_path, "new.py", "def register(pb):\n    pass\n")
    reloaded = mgr.rescan()
    assert "new" in reloaded
    assert len(mgr.get_all_states()) == 1


def test_rescan_detects_removed_file(tmp_path: Path) -> None:
    hook_file = _write_hook(
        tmp_path,
        "removable.py",
        "def register(pb):\n"
        "    @pb.on_record_create_request()\n"
        "    async def h(e):\n"
        "        await e.next()\n",
    )
    mgr, pb = _make_manager(tmp_path)
    mgr.initial_load()
    assert len(mgr.get_all_states()) == 1

    hook = pb._extensions.hooks.get("record_create_request")
    assert len(hook._bindings) == 1

    hook_file.unlink()
    mgr.rescan()
    assert len(mgr.get_all_states()) == 0
    assert len(hook._bindings) == 0


def test_rescan_detects_changed_file(tmp_path: Path) -> None:
    import time

    _write_hook(
        tmp_path,
        "changeable.py",
        "HOOK_NAME = 'Version 1'\n"
        "def register(pb):\n    pass\n",
    )
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()
    assert mgr.get_state("changeable")["name"] == "Version 1"

    # Ensure filesystem timestamp differs (some FS have 1s granularity)
    time.sleep(0.05)
    _write_hook(
        tmp_path,
        "changeable.py",
        "HOOK_NAME = 'Version 2'\n"
        "def register(pb):\n    pass\n# changed\n",
    )
    reloaded = mgr.rescan()
    assert "changeable" in reloaded
    assert mgr.get_state("changeable")["name"] == "Version 2"


def test_rescan_skips_disabled_files(tmp_path: Path) -> None:
    _write_hook(tmp_path, "kept.py", "def register(pb):\n    pass\n")
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()
    mgr.disable_hook("kept")

    _write_hook(
        tmp_path,
        "kept.py",
        "HOOK_NAME = 'Updated'\ndef register(pb):\n    pass\n",
    )
    reloaded = mgr.rescan()
    assert "kept" not in reloaded
    assert mgr.get_state("kept")["status"] == "disabled"


# ---------------------------------------------------------------------------
# Route / middleware detection
# ---------------------------------------------------------------------------


def test_route_registration_detected(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "with_route.py",
        "def register(pb):\n"
        "    @pb.get('/custom/status')\n"
        "    async def status():\n"
        "        return {'ok': True}\n",
    )
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()

    state = mgr.get_state("with_route")
    assert state is not None
    assert state["hasRoutes"] is True
    assert state["status"] == "loaded"


def test_route_file_reload_after_materialization_is_restart_required(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "files.py",
        "from ppbase import pb\n"
        "@pb.get('/api/my-storage')\n"
        "async def my_storage():\n"
        "    return {'ok': True}\n",
    )
    pb = PPBase(hooks_dir=str(tmp_path))
    app = pb.get_app()
    _load_startup_extensions(app)
    hook_manager = getattr(app.state, "hook_manager", None)
    assert hook_manager is not None

    _write_hook(
        tmp_path,
        "files.py",
        "from ppbase import pb\n"
        "@pb.get('/api/my-storage')\n"
        "async def my_storage():\n"
        "    return {'ok': 'updated'}\n",
    )

    state = hook_manager.reload_file("files")
    assert state is not None
    serialized = hook_manager.get_state("files")
    assert serialized is not None
    assert serialized["status"] == "unsupported_for_hot_reload"
    assert serialized["hasRoutes"] is True
    assert serialized["restartRequired"] is True
    assert "require a PPBase restart" in (serialized["error"] or "")


# ---------------------------------------------------------------------------
# get_state / get_all_states
# ---------------------------------------------------------------------------


def test_get_state_returns_none_for_missing(tmp_path: Path) -> None:
    mgr, _ = _make_manager(tmp_path)
    assert mgr.get_state("nonexistent") is None


def test_get_all_states_shape(tmp_path: Path) -> None:
    _write_hook(
        tmp_path,
        "shaped.py",
        'HOOK_NAME = "Test Hook"\nHOOK_DESCRIPTION = "desc"\ndef register(pb):\n    pass\n',
    )
    mgr, _ = _make_manager(tmp_path)
    mgr.initial_load()
    states = mgr.get_all_states()
    assert len(states) == 1
    s = states[0]
    required_keys = {
        "id", "filename", "filepath", "name", "description", "status",
        "enabled", "functions", "error", "errorTraceback", "lastLoaded",
        "hasRoutes", "hasHttpMiddleware", "restartRequired",
    }
    assert required_keys.issubset(set(s.keys()))
