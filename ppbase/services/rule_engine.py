"""API rules evaluation engine.

PocketBase API rules control access to collection endpoints:

- ``None``  (null)  -> only superusers / admins can access
- ``""``   (empty)  -> public access
- ``"expression"`` -> additional SQL WHERE filter that must match
"""

from __future__ import annotations

from typing import Any


def check_rule(
    rule: str | None,
    auth_context: dict[str, Any] | None,
) -> bool | str:
    """Evaluate an API rule.

    Args:
        rule: The rule string from the collection definition.
        auth_context: Resolved auth context (from ``build_auth_context``).

    Returns:
        - ``True`` if access is unconditionally allowed (public rule).
        - ``False`` if access is denied (null rule with no admin auth).
        - A filter expression string if the rule needs to be applied as
          an additional SQL WHERE clause.
    """
    # None -> admin-only
    if rule is None:
        if auth_context and auth_context.get("is_admin"):
            return True
        return False

    # Empty string -> public
    if rule == "":
        return True

    # Expression -> return it for the caller to apply as a SQL filter
    # Admin bypasses all expression rules
    if auth_context and auth_context.get("is_admin"):
        return True

    return rule


def build_auth_context(
    token_payload: dict[str, Any] | None = None,
    admin: Any | None = None,
) -> dict[str, Any] | None:
    """Build an auth context dict for rule evaluation.

    The context provides values for ``@request.auth.*`` macros used in
    filter expressions.

    Args:
        token_payload: Decoded JWT payload (may be None for unauthenticated).
        admin: An ``AdminRecord`` if the token belongs to an admin.

    Returns:
        A dict with auth context keys, or ``None`` if unauthenticated.
    """
    if admin is not None:
        return {
            "is_admin": True,
            "@request.auth.id": admin.id if hasattr(admin, "id") else "",
            "@request.auth.email": admin.email if hasattr(admin, "email") else "",
        }

    if token_payload is None:
        return None

    ctx: dict[str, Any] = {"is_admin": False}
    ctx["@request.auth.id"] = token_payload.get("id", "")
    ctx["@request.auth.collectionId"] = token_payload.get("collectionId", "")
    ctx["@request.auth.type"] = token_payload.get("type", "")
    return ctx
