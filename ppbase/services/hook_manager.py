"""File-based hook manager for pb_hooks/ directory.

Discovers Python hook files, loads them, tracks ownership of registered
hook bindings, and supports hot-reload of changed files at runtime.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from ppbase.ext.hooks import Hook
from ppbase.ext.registry import HOOK_NAMES, ExtensionRegistry

logger = logging.getLogger(__name__)


@dataclass
class HookFunction:
    """Metadata about a single registered hook handler."""

    hook_type: str
    binding_id: str
    handler_name: str


@dataclass
class HookFileState:
    """Tracks the state of a single hook file."""

    filename: str
    filepath: str
    hook_id: str
    name: str
    description: str
    status: str  # "loaded", "error", "disabled", "unsupported_for_hot_reload"
    enabled: bool = True
    functions: list[HookFunction] = field(default_factory=list)
    error: str | None = None
    error_traceback: str | None = None
    last_loaded: str | None = None
    file_hash: str | None = None
    file_mtime: float = 0.0
    has_routes: bool = False
    has_http_middleware: bool = False
    restart_required: bool = False


def _compute_file_hash(filepath: str) -> str:
    try:
        content = Path(filepath).read_bytes()
        return hashlib.md5(content).hexdigest()
    except OSError:
        return ""


def _hook_id_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    return stem.replace(" ", "_").replace("-", "_").lower()


class _BindingTracker:
    """Wraps an ExtensionRegistry to intercept hook bind calls and track ownership."""

    def __init__(self, registry: ExtensionRegistry, owner_id: str) -> None:
        self._registry = registry
        self._owner_id = owner_id
        self.bindings: list[HookFunction] = []
        self._route_count_before = len(registry._routes)
        self._middleware_count_before = len(registry._global_middlewares)
        self._http_middleware_count_before = len(registry._http_middlewares)
        self.route_attempted = False
        self.route_middleware_attempted = False
        self.http_middleware_attempted = False

    @property
    def has_new_routes(self) -> bool:
        return self.route_attempted or len(self._registry._routes) > self._route_count_before

    @property
    def has_new_http_middleware(self) -> bool:
        return (
            self.http_middleware_attempted
            or len(self._registry._http_middlewares) > self._http_middleware_count_before
        )

    @property
    def requires_restart(self) -> bool:
        return (
            self._registry.frozen
            and (self.route_attempted or self.route_middleware_attempted or self.http_middleware_attempted)
        )

    def record_binding(self, hook_type: str, binding_id: str, handler_name: str) -> None:
        self.bindings.append(HookFunction(
            hook_type=hook_type,
            binding_id=binding_id,
            handler_name=handler_name,
        ))


class _TrackingPB:
    """A thin proxy around the real pb facade that intercepts hook registration
    to record which bindings belong to which hook file."""

    def __init__(self, real_pb: Any, tracker: _BindingTracker) -> None:
        self._real_pb = real_pb
        self._tracker = tracker

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real_pb, name)
        if name.startswith("on_") and callable(attr):
            return self._wrap_hook_decorator(name, attr)
        if name in {"route", "get", "post", "put", "patch", "delete", "options", "head"}:
            return self._wrap_route_registration(name)
        if name in {"middleware", "use"}:
            return self._wrap_route_middleware(name)
        if name == "http_middleware":
            return self._wrap_http_middleware(name)
        if name == "group":
            return self._wrap_group(name)
        return attr

    def _wrap_hook_decorator(self, method_name: str, original_method: Any) -> Any:
        tracker = self._tracker
        hook_type = method_name[3:]  # strip "on_"

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            decorator = original_method(*args, **kwargs)

            def tracking_decorator(func: Any) -> Any:
                result = decorator(func)
                handler_name = getattr(func, "__name__", str(func))
                for hname in HOOK_NAMES:
                    hook: Hook = tracker._registry.hooks.get(hname)
                    for bid, binding in hook._bindings.items():
                        already_tracked = any(
                            hf.binding_id == bid for hf in tracker.bindings
                        )
                        if not already_tracked and binding.handler is func:
                            tracker.record_binding(hname, bid, handler_name)
                return result

            return tracking_decorator

        return wrapper

    def _wrap_route_registration(self, method_name: str) -> Any:
        original_method = getattr(self._real_pb, method_name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not self._tracker._registry.frozen:
                return original_method(*args, **kwargs)

            self._tracker.route_attempted = True

            def _noop_decorator(func: Any) -> Any:
                return func

            return _noop_decorator

        return wrapper

    def _wrap_route_middleware(self, method_name: str) -> Any:
        original_method = getattr(self._real_pb, method_name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not self._tracker._registry.frozen:
                return original_method(*args, **kwargs)

            self._tracker.route_middleware_attempted = True

            if args and callable(args[0]):
                return args[0]

            def _noop_decorator(func: Any) -> Any:
                return func

            return _noop_decorator

        return wrapper

    def _wrap_http_middleware(self, method_name: str) -> Any:
        original_method = getattr(self._real_pb, method_name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not self._tracker._registry.frozen:
                return original_method(*args, **kwargs)

            self._tracker.http_middleware_attempted = True

            if args and callable(args[0]):
                return args[0]

            def _noop_decorator(func: Any) -> Any:
                return func

            return _noop_decorator

        return wrapper

    def _wrap_group(self, method_name: str) -> Any:
        original_method = getattr(self._real_pb, method_name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not self._tracker._registry.frozen:
                return original_method(*args, **kwargs)
            self._tracker.route_attempted = True
            return _NoopRouteGroup(self._tracker)

        return wrapper


class _NoopRouteGroup:
    """Route-group stand in used during hot reload after app materialization."""

    def __init__(self, tracker: _BindingTracker) -> None:
        self._tracker = tracker

    def route(self, *args: Any, **kwargs: Any):
        self._tracker.route_attempted = True

        def _noop_decorator(func: Any) -> Any:
            return func

        return _noop_decorator

    def get(self, *args: Any, **kwargs: Any):
        return self.route(*args, **kwargs)

    def post(self, *args: Any, **kwargs: Any):
        return self.route(*args, **kwargs)

    def put(self, *args: Any, **kwargs: Any):
        return self.route(*args, **kwargs)

    def patch(self, *args: Any, **kwargs: Any):
        return self.route(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any):
        return self.route(*args, **kwargs)

    def options(self, *args: Any, **kwargs: Any):
        return self.route(*args, **kwargs)

    def head(self, *args: Any, **kwargs: Any):
        return self.route(*args, **kwargs)

    def middleware(self, func: Any | None = None, **kwargs: Any):
        self._tracker.route_middleware_attempted = True
        if func is not None and callable(func):
            return func

        def _noop_decorator(handler: Any) -> Any:
            return handler

        return _noop_decorator

    def use(self, func: Any | None = None, **kwargs: Any):
        return self.middleware(func, **kwargs)

    def group(self, *args: Any, **kwargs: Any):
        self._tracker.route_attempted = True
        return self

    def unbind(self, *ids: str):
        return self


class HookManager:
    """Manages file-based hooks from a directory."""

    def __init__(
        self,
        hooks_dir: str | Path,
        extensions: ExtensionRegistry,
        pb_facade: Any,
    ) -> None:
        self._hooks_dir = Path(hooks_dir)
        self._extensions = extensions
        self._pb = pb_facade
        self._files: dict[str, HookFileState] = {}
        self._disabled_hooks: set[str] = set()
        self._auto_restart_on_change = False

    @property
    def hooks_dir(self) -> Path:
        return self._hooks_dir

    @property
    def files(self) -> dict[str, HookFileState]:
        return dict(self._files)

    def set_disabled_hooks(self, disabled: set[str]) -> None:
        self._disabled_hooks = set(disabled)

    def get_disabled_hooks(self) -> set[str]:
        return set(self._disabled_hooks)

    def set_auto_restart_on_change(self, enabled: bool) -> None:
        self._auto_restart_on_change = bool(enabled)

    def get_auto_restart_on_change(self) -> bool:
        return self._auto_restart_on_change

    def discover_files(self) -> list[str]:
        if not self._hooks_dir.is_dir():
            return []
        files = sorted(
            f.name
            for f in self._hooks_dir.iterdir()
            if f.is_file()
            and f.suffix == ".py"
            and f.name != "__init__.py"
            and not f.name.startswith("_")
        )
        return files

    def initial_load(self) -> None:
        files = self.discover_files()
        for filename in files:
            hook_id = _hook_id_from_filename(filename)
            filepath = str(self._hooks_dir / filename)
            state = HookFileState(
                filename=filename,
                filepath=filepath,
                hook_id=hook_id,
                name=hook_id,
                description="",
                status="loaded",
                enabled=hook_id not in self._disabled_hooks,
            )
            self._files[hook_id] = state

            if not state.enabled:
                state.status = "disabled"
                logger.info("Hook file %s is disabled, skipping load", filename)
                continue

            self._load_file(state)

    def _load_file(self, state: HookFileState) -> None:
        filepath = state.filepath
        filename = state.filename

        state.error = None
        state.error_traceback = None
        state.functions = []
        state.has_routes = False
        state.has_http_middleware = False
        state.restart_required = False

        file_hash = _compute_file_hash(filepath)
        try:
            file_mtime = os.path.getmtime(filepath)
        except OSError:
            file_mtime = 0.0

        state.file_hash = file_hash
        state.file_mtime = file_mtime

        tracker = _BindingTracker(self._extensions, state.hook_id)

        tracking_pb = _TrackingPB(self._pb, tracker)

        try:
            module = self._import_file(filepath, filename, tracking_pb)
        except Exception as exc:
            state.status = "error"
            state.error = str(exc)
            state.error_traceback = traceback.format_exc()
            logger.error("Failed to import hook file %s: %s", filename, exc)
            return

        state.name = getattr(module, "HOOK_NAME", state.hook_id)
        state.description = getattr(module, "HOOK_DESCRIPTION", "")

        register_fn = getattr(module, "register", None)
        if register_fn is not None and callable(register_fn):
            try:
                register_fn(tracking_pb)
            except Exception as exc:
                state.status = "error"
                state.error = f"register() failed: {exc}"
                state.error_traceback = traceback.format_exc()
                logger.error("Hook file %s register() failed: %s", filename, exc)
                return

        if not tracker.bindings:
            self._scan_bindings_from_module(module, tracker)

        state.functions = tracker.bindings
        state.has_routes = tracker.has_new_routes
        state.has_http_middleware = tracker.has_new_http_middleware
        state.restart_required = tracker.requires_restart
        state.status = (
            "unsupported_for_hot_reload"
            if tracker.requires_restart
            else "loaded"
        )
        if tracker.requires_restart:
            state.error = (
                "Route or middleware changes were detected in this hook file. "
                "They were not hot-reloaded and require a PPBase restart to apply."
            )
            state.error_traceback = None
        state.last_loaded = datetime.now(timezone.utc).isoformat()

        logger.info(
            "Loaded hook file %s: %d binding(s)%s%s",
            filename,
            len(state.functions),
            " (has routes — requires restart for changes)" if state.has_routes else "",
            " (hot reload skipped for route/middleware changes)" if tracker.requires_restart else "",
        )

    def _scan_bindings_from_module(self, module: ModuleType, tracker: _BindingTracker) -> None:
        """Fallback: scan hook registry for bindings whose handler is defined in this module."""
        for hook_name in HOOK_NAMES:
            hook: Hook = self._extensions.hooks.get(hook_name)
            for bid, binding in hook._bindings.items():
                handler = binding.handler
                handler_module = getattr(handler, "__module__", None)
                if handler_module and handler_module == module.__name__:
                    already = any(hf.binding_id == bid for hf in tracker.bindings)
                    if not already:
                        tracker.record_binding(
                            hook_name,
                            bid,
                            getattr(handler, "__name__", str(handler)),
                        )

    def _import_file(self, filepath: str, filename: str, tracking_pb: _TrackingPB) -> ModuleType:
        module_name = f"pb_hooks.{Path(filename).stem}"

        if module_name in sys.modules:
            del sys.modules[module_name]

        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for {filepath}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        import ppbase as _ppbase_module

        original_pb = getattr(_ppbase_module, "pb", None)
        setattr(_ppbase_module, "pb", tracking_pb)
        try:
            spec.loader.exec_module(module)
        finally:
            if original_pb is not None:
                setattr(_ppbase_module, "pb", original_pb)
        return module

    def reload_file(self, hook_id: str) -> HookFileState | None:
        state = self._files.get(hook_id)
        if state is None:
            return None

        if not state.enabled:
            state.status = "disabled"
            return state

        self._unbind_file(state)
        self._load_file(state)
        return state

    def _unbind_file(self, state: HookFileState) -> None:
        for func_info in state.functions:
            try:
                hook: Hook = self._extensions.hooks.get(func_info.hook_type)
                hook.unbind(func_info.binding_id)
            except (ValueError, KeyError):
                pass
        state.functions = []

    def disable_hook(self, hook_id: str) -> HookFileState | None:
        state = self._files.get(hook_id)
        if state is None:
            return None

        self._unbind_file(state)
        state.enabled = False
        state.status = "disabled"
        self._disabled_hooks.add(hook_id)
        logger.info("Disabled hook: %s", hook_id)
        return state

    def enable_hook(self, hook_id: str) -> HookFileState | None:
        state = self._files.get(hook_id)
        if state is None:
            return None

        state.enabled = True
        self._disabled_hooks.discard(hook_id)
        self._load_file(state)
        logger.info("Enabled hook: %s", hook_id)
        return state

    def rescan(self) -> list[str]:
        """Rescan the hooks directory for new/removed/changed files.
        Returns list of hook_ids that were reloaded."""
        reloaded: list[str] = []
        current_files = self.discover_files()
        current_ids = {_hook_id_from_filename(f) for f in current_files}
        existing_ids = set(self._files.keys())

        removed = existing_ids - current_ids
        for hook_id in removed:
            state = self._files.pop(hook_id, None)
            if state:
                self._unbind_file(state)
                self._disabled_hooks.discard(hook_id)
                logger.info("Removed hook file: %s", state.filename)

        for filename in current_files:
            hook_id = _hook_id_from_filename(filename)
            filepath = str(self._hooks_dir / filename)

            if hook_id not in self._files:
                state = HookFileState(
                    filename=filename,
                    filepath=filepath,
                    hook_id=hook_id,
                    name=hook_id,
                    description="",
                    status="loaded",
                    enabled=hook_id not in self._disabled_hooks,
                )
                self._files[hook_id] = state
                if state.enabled:
                    self._load_file(state)
                else:
                    state.status = "disabled"
                reloaded.append(hook_id)
                continue

            state = self._files[hook_id]
            if not state.enabled:
                continue

            new_hash = _compute_file_hash(filepath)
            try:
                new_mtime = os.path.getmtime(filepath)
            except OSError:
                new_mtime = 0.0

            if new_hash != state.file_hash or new_mtime != state.file_mtime:
                self._unbind_file(state)
                self._load_file(state)
                reloaded.append(hook_id)

        return reloaded

    def get_all_states(self) -> list[dict[str, Any]]:
        result = []
        for state in self._files.values():
            result.append({
                "id": state.hook_id,
                "filename": state.filename,
                "filepath": state.filepath,
                "name": state.name,
                "description": state.description,
                "status": state.status,
                "enabled": state.enabled,
                "functions": [
                    {
                        "hookType": f.hook_type,
                        "bindingId": f.binding_id,
                        "handlerName": f.handler_name,
                    }
                    for f in state.functions
                ],
                "error": state.error,
                "errorTraceback": state.error_traceback,
                "lastLoaded": state.last_loaded,
                "hasRoutes": state.has_routes,
                "hasHttpMiddleware": state.has_http_middleware,
                "restartRequired": state.restart_required,
            })
        return result

    def get_state(self, hook_id: str) -> dict[str, Any] | None:
        state = self._files.get(hook_id)
        if state is None:
            return None
        states = self.get_all_states()
        for s in states:
            if s["id"] == hook_id:
                return s
        return None
