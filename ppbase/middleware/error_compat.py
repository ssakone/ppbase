"""PocketBase 0.22 error compatibility (opt-in, disabled by default).

PPBase answers errors with the envelope of current PocketBase versions,
``{"status", "message", "data"}`` (v0.40.6 ``tools/router/error.go``
l.36-42), and current messages such as "Only superusers can perform this
action.".  PocketBase 0.22.34 answers ``{"code", "message", "data"}``
(``apis/api_error.go`` l.11-15) with its own texts ("Only admins can perform
this action.", ``apis/record_crud.go`` l.57).  A host application whose
clients were written against PocketBase 0.22 enables this mode once, at
startup, from its hooks registration or from the environment::

    def register(pb):
        pb.set_error_compat("0.22")

    PPBASE_ERROR_COMPAT=0.22 python -m ppbase serve ...

When the mode is off (the default) responses are never touched.  When it is
``"0.22"``, one outermost ASGI middleware post-processes every JSON error
response (status >= 400): a body that is exactly a PPBase envelope becomes
``{"code", "message", "data"}`` with the PocketBase 0.22.34 message
(``_MESSAGE_RULES`` below, each rule citing its source) normalized like
``NewApiError`` (``strings.TrimSpace(inflector.Sentenize(message))``,
api_error.go l.67-75).  Any other JSON body, for example a host route that
already answers ``{"code", ...}`` or a custom ``{"message"}``, is left as is.
Status codes are never changed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The only compatibility mode; ``""`` is PPBase's own behavior.
POCKETBASE_022 = "0.22"
_OFF_VALUES = frozenset({"", "off", "none", "false", "0", "default", "current"})
_ON_VALUES = frozenset({"0.22", "v0.22", "pocketbase-0.22", "pb-0.22"})

_ENVELOPE_KEYS = frozenset({"status", "message", "data"})

# NewNotFoundError / NewBadRequestError / NewForbiddenError /
# NewUnauthorizedError defaults (apis/api_error.go l.31-65).
_DEFAULT_MESSAGES = {
    400: "Something went wrong while processing your request.",
    401: "Missing or invalid authentication token.",
    403: "You are not allowed to perform this request.",
    404: "The requested resource wasn't found.",
}


def normalize_error_compat(mode: Any) -> str:
    """Return ``""`` (off) or ``"0.22"``; reject any other value."""
    value = "" if mode is None else str(mode).strip().lower()
    if value in _OFF_VALUES:
        return ""
    if value in _ON_VALUES:
        return POCKETBASE_022
    raise ValueError(
        f"Unsupported error compatibility mode {mode!r}: use '' (PPBase) or '0.22'."
    )


def set_error_compat(app: Any, mode: Any) -> str:
    """Enable (``"0.22"``) or disable (``""``) the mode on a PPBase app."""
    normalized = normalize_error_compat(mode)
    app.state.error_compat = normalized
    settings = getattr(app.state, "settings", None)
    if settings is not None:
        try:
            settings.error_compat = normalized
        except (AttributeError, TypeError, ValueError):
            pass
    return normalized


def get_error_compat(app: Any) -> str:
    """The active mode of a PPBase app (``""`` when unset)."""
    state = getattr(app, "state", None)
    return str(getattr(state, "error_compat", "") or "")


def sentenize(message: str) -> str:
    """PocketBase ``inflector.Sentenize`` + ``TrimSpace`` (v0.22.34)."""
    text = message.strip()
    if not text:
        return ""
    first = text[0].upper()
    if len(first) == 1:
        text = first + text[1:]
    if text[-1] not in ".?!":
        text += "."
    return text


@dataclass(frozen=True)
class _MessageRule:
    """One PPBase message that PocketBase 0.22.34 words differently."""

    status: int
    messages: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None
    replacement: str = ""
    path: re.Pattern[str] | None = None
    methods: frozenset[str] | None = None
    data: dict[str, Any] | None = None
    # Apply only when the PPBase data is exactly {field: {"code": code}}.
    data_codes: dict[str, str] | None = None
    source: str = ""

    def apply(
        self, status: int, message: str, method: str, path: str, data: Any = None
    ) -> str | None:
        if status != self.status:
            return None
        if self.methods is not None and method not in self.methods:
            return None
        if self.path is not None and not self.path.match(path):
            return None
        if self.data_codes is not None and _data_codes(data) != self.data_codes:
            return None
        if self.pattern is not None:
            match = self.pattern.match(message)
            return match.expand(self.replacement) if match else None
        return self.replacement if message in self.messages else None


def _data_codes(data: Any) -> dict[str, str] | None:
    if not isinstance(data, dict):
        return None
    codes = {}
    for key, value in data.items():
        if not isinstance(value, dict) or not isinstance(value.get("code"), str):
            return None
        codes[str(key)] = value["code"]
    return codes


_COLLECTION_ROUTE = re.compile(r"^/api/collections/[^/]+/")
_RECORD_LIST_ROUTE = re.compile(r"^/api/collections/[^/]+/records/?$")
_PASSWORD_AUTH_ROUTE = re.compile(
    r"^/api/(?:collections/[^/]+|admins)/auth-with-password/?$"
)
_RECORD_WRITE_ROUTE = re.compile(r"^/api/collections/[^/]+/records(?:/[^/]+)?/?$")
_AUTH_FORM_ROUTE = re.compile(
    r"^/api/collections/[^/]+/(?:auth-with-password|auth-with-oauth2"
    r"|request-password-reset|confirm-password-reset|request-verification"
    r"|confirm-verification|request-email-change|confirm-email-change)/?$"
)
_AUTH_REFRESH_ROUTE = re.compile(r"^/api/collections/[^/]+/auth-refresh/?$")
_REQUEST_FORM_ROUTE = re.compile(
    r"^/api/collections/[^/]+/(?:request-password-reset|request-verification)/?$"
)
_CONFIRM_FORM_ROUTE = re.compile(
    r"^/api/collections/[^/]+/(?:confirm-password-reset|confirm-verification)/?$"
)
_REALTIME_ROUTE = re.compile(r"^/api/realtime/?$")
_COLLECTION_ADMIN_ROUTE = re.compile(r"^/api/collections/[^/]+(?:/truncate)?/?$")
_LOGS_ROUTE = re.compile(r"^/api/logs/")
_SETTINGS_ROUTE = re.compile(r"^/api/settings/?$")
_BODY_ERRORS = ("Invalid JSON body.", "Request body must be a JSON object.")

#: PocketBase 0.22.34 wording of the PPBase messages that differ
#: (``git show v0.22.34:<file>``).  First match wins.
_MESSAGE_RULES: tuple[_MessageRule, ...] = (
    _MessageRule(
        403,
        messages=("Only superusers can perform this action.",),
        replacement="Only admins can perform this action.",
        source="apis/record_crud.go l.57 (list), l.155 (view), l.209 (create), "
        "l.325 (update), l.417 (delete)",
    ),
    _MessageRule(
        401,
        messages=("The request requires admin authorization token to be set.",),
        replacement="The request requires valid admin authorization token to be set.",
        source="apis/middlewares.go l.119 (RequireAdminAuth), "
        "l.147 (RequireAdminAuthOnlyIfAny)",
    ),
    _MessageRule(
        404,
        messages=("Missing collection context.",),
        replacement="The requested resource wasn't found.",
        path=_COLLECTION_ROUTE,
        source="apis/middlewares.go l.263-266 (LoadCollectionContext answers "
        "an unknown collection before the handler; record_crud.go l.45 passes "
        "'Missing collection context.' as data, not as message)",
    ),
    _MessageRule(
        401,
        messages=("The request requires valid auth token to be set.",),
        replacement="The request requires valid record authorization token to be set.",
        path=_AUTH_REFRESH_ROUTE,
        source="apis/middlewares.go l.94-100 (RequireSameContextRecordAuth, "
        "bound to auth-refresh by apis/record_auth.go l.46)",
    ),
    _MessageRule(
        400,
        messages=_BODY_ERRORS,
        replacement="Failed to load the submitted data due to invalid formatting.",
        path=_RECORD_WRITE_ROUTE,
        methods=frozenset({"POST", "PATCH"}),
        source="apis/record_crud.go l.227 and l.270 (create), l.362 (update)",
    ),
    _MessageRule(
        400,
        messages=_BODY_ERRORS,
        replacement="An error occurred while loading the submitted data.",
        path=_AUTH_FORM_ROUTE,
        source="apis/record_auth.go l.202, l.302, l.379, l.433, l.471, l.525, "
        "l.568, l.603 (c.Bind failures)",
    ),
    _MessageRule(
        400,
        messages=("Something went wrong while processing your request.",),
        replacement="An error occurred while validating the form.",
        path=_REQUEST_FORM_ROUTE,
        source="apis/record_auth.go l.383 (request-password-reset), "
        "l.475 (request-verification)",
    ),
    _MessageRule(
        400,
        messages=(
            "Failed to confirm password reset.",
            "Failed to confirm verification.",
        ),
        replacement="Something went wrong while processing your request.",
        path=_CONFIRM_FORM_ROUTE,
        source="apis/record_auth.go l.440-460 and l.532-552: a form validation "
        "error (invalid token, password) is returned as is and becomes "
        "NewBadRequestError('', err) in apis/base.go l.75-80",
    ),
    _MessageRule(
        400,
        messages=("Invalid JSON body.",),
        replacement="Something went wrong while processing your request.",
        path=_REALTIME_ROUTE,
        source="apis/realtime.go l.172-174",
    ),
    _MessageRule(
        400,
        messages=("Missing clientId.",),
        replacement="Something went wrong while processing your request.",
        path=_REALTIME_ROUTE,
        data={
            "clientId": {"code": "validation_required", "message": "Cannot be blank."}
        },
        source="apis/realtime.go l.177-179 (forms.RealtimeSubscribe validation)",
    ),
    _MessageRule(
        400,
        pattern=re.compile(
            r"^(Failed to (?:create|update) record\.) Validation failed for: .*$"
        ),
        replacement=r"\1",
        path=_RECORD_WRITE_ROUTE,
        source="apis/record_crud.go l.286 (create), l.378 (update): no field list",
    ),
    _MessageRule(
        400,
        pattern=re.compile(r"^(?!Query string is too large)(?s:.*)$"),
        replacement="Something went wrong while processing your request.",
        path=_RECORD_LIST_ROUTE,
        methods=frozenset({"GET"}),
        source="apis/record_crud.go l.82-86: a filter/sort parse error is "
        "NewBadRequestError('', err), without the parser text",
    ),
    _MessageRule(
        404,
        pattern=re.compile(r"^Collection '.*' not found\.$"),
        replacement="The requested resource wasn't found.",
        path=_COLLECTION_ADMIN_ROUTE,
        source="apis/collection.go l.62 (view), l.117 (update), l.156 (delete): "
        "NewNotFoundError('', err)",
    ),
    _MessageRule(
        404,
        messages=("Log not found.",),
        replacement="The requested resource wasn't found.",
        path=_LOGS_ROUTE,
        source="apis/logs.go l.72, l.77: NewNotFoundError('', ...)",
    ),
    _MessageRule(
        400,
        messages=("Invalid settings payload.",),
        replacement="An error occurred while loading the submitted data.",
        path=_SETTINGS_ROUTE,
        methods=frozenset({"PATCH"}),
        source="apis/settings.go l.53 (c.Bind failure)",
    ),
    _MessageRule(
        400,
        messages=("Failed to authenticate.",),
        replacement="Failed to authenticate.",
        path=_PASSWORD_AUTH_ROUTE,
        data={},
        data_codes={"identity": "validation_invalid_credentials"},
        source="apis/record_auth.go l.316-317 and apis/admin.go l.97-98: the "
        "'Invalid login credentials.' error is not a validation error, so "
        "safeErrorsData gives {} (apis/api_error.go l.77-90)",
    ),
)


def message_rules() -> tuple[_MessageRule, ...]:
    """The translation table (for documentation and tests)."""
    return _MESSAGE_RULES


def _ppbase_error(body: Any) -> tuple[str, Any] | None:
    """``(message, data)`` of a PPBase error body, else ``None``.

    A PPBase envelope ``{"status": int, "message": str[, "data"]}`` and the
    bare ``{"detail": str}`` that Starlette answers for an unknown route or
    method (PocketBase 0.22.34 answers those through echo's ``HTTPError``,
    apis/base.go l.72-74: ``NewApiError(code, "Not Found", ...)``).
    """
    if not isinstance(body, dict):
        return None
    if set(body) == {"detail"} and isinstance(body["detail"], str):
        return body["detail"], {}
    status = body.get("status")
    if (
        "message" in body
        and set(body) <= _ENVELOPE_KEYS
        and isinstance(status, int)
        and not isinstance(status, bool)
        and isinstance(body.get("message"), str)
    ):
        return body["message"], body.get("data")
    return None


def pocketbase_022_error(
    status: int,
    body: Any,
    *,
    method: str = "GET",
    path: str = "/",
) -> dict[str, Any] | None:
    """The PocketBase 0.22.34 error body of a PPBase error body, else ``None``."""
    parsed = _ppbase_error(body)
    if parsed is None:
        return None
    message, data = parsed
    for rule in _MESSAGE_RULES:
        replacement = rule.apply(status, message, method.upper(), path, data)
        if replacement is not None:
            message = replacement
            if rule.data is not None:
                data = json.loads(json.dumps(rule.data))
            break
    message = sentenize(message) or _DEFAULT_MESSAGES.get(status, "")
    # safeErrorsData: always a JSON object (api_error.go l.77-90).
    return {
        "code": status,
        "message": message,
        "data": data if isinstance(data, dict) else {},
    }


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


class ErrorCompatMiddleware:
    """Rewrite PPBase JSON error envelopes when the app enables a mode."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        owner = scope.get("app")
        if owner is None or get_error_compat(owner) != POCKETBASE_022:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", "/"))
        start: Message | None = None
        chunks: list[bytes] = []

        async def wrapped_send(message: Message) -> None:
            nonlocal start
            kind = message.get("type")
            if kind == "http.response.start":
                status = int(message.get("status", 200))
                headers = list(message.get("headers", []))
                content_type = (_header(headers, b"content-type") or b"").lower()
                if status >= 400 and content_type.startswith(b"application/json"):
                    start = message
                    return
                await send(message)
                return
            if kind == "http.response.body" and start is not None:
                chunks.append(message.get("body", b"") or b"")
                if message.get("more_body", False):
                    return
                await _flush(start, b"".join(chunks))
                return
            await send(message)

        async def _flush(start_message: Message, raw: bytes) -> None:
            status = int(start_message.get("status", 500))
            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, ValueError):
                decoded = None
            rewritten = pocketbase_022_error(status, decoded, method=method, path=path)
            if rewritten is None:
                await send(start_message)
                await send(
                    {"type": "http.response.body", "body": raw, "more_body": False}
                )
                return
            body = json.dumps(
                rewritten, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers = [
                (key, value)
                for key, value in start_message.get("headers", [])
                if key.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(body)).encode("latin-1")))
            await send({**start_message, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, wrapped_send)
