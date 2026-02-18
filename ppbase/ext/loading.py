"""Hook module loading helpers."""

from __future__ import annotations

import importlib
from typing import Any, Callable


def resolve_hook_target(target: str) -> Callable[[Any], Any]:
    """Resolve a CLI hook target in ``module:function`` format."""
    module_name, sep, attr_name = target.partition(":")
    if not sep or not module_name.strip() or not attr_name.strip():
        raise ValueError(
            f"Invalid hook target '{target}'. Expected format: module:function"
        )

    try:
        module = importlib.import_module(module_name.strip())
    except Exception as exc:
        raise ImportError(
            f"Failed to import hook module '{module_name.strip()}': {exc}"
        ) from exc

    try:
        target_callable = getattr(module, attr_name.strip())
    except AttributeError as exc:
        raise AttributeError(
            f"Hook callable '{attr_name.strip()}' not found in module '{module_name.strip()}'."
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
