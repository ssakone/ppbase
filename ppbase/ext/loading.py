"""Hook module loading helpers."""

from __future__ import annotations

import importlib
from typing import Any, Callable


def validate_hook_target(target: str) -> str:
    """Validate and normalize a hook target without importing user code."""
    module_name, sep, attr_name = target.partition(":")
    if not sep or not module_name.strip() or not attr_name.strip():
        raise ValueError(
            f"Invalid hook target '{target}'. Expected format: module:function"
        )
    return f"{module_name.strip()}:{attr_name.strip()}"


def resolve_hook_target(target: str) -> Callable[[Any], Any]:
    """Resolve a CLI hook target in ``module:function`` format."""
    normalized = validate_hook_target(target)
    module_name, attr_name = normalized.split(":", 1)

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ImportError(
            f"Failed to import hook module '{module_name}': {exc}"
        ) from exc

    try:
        target_callable = getattr(module, attr_name)
    except AttributeError as exc:
        raise AttributeError(
            f"Hook callable '{attr_name}' not found in module '{module_name}'."
        ) from exc

    if not callable(target_callable):
        raise TypeError(
            f"Hook target '{target}' is not callable."
        )

    return target_callable


def load_hook_target(target: str, pb: Any) -> None:
    """Import and execute a hook target callable with the provided ``pb`` facade."""
    target_callable = resolve_hook_target(target)
    try:
        target_callable(pb)
    except TypeError as exc:
        raise TypeError(
            f"Hook callable '{target}' must accept the pb facade argument."
        ) from exc
