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

The body alone does not always tell which 0.22.34 error a response stands
for (an admin's and a user's record create validation errors, or two route
guards, share a PPBase body but not a 0.22.34 message).  The handler that
knows it then tags its error response with :func:`mark_error` (or a guard
raises with :func:`error_kind_headers`): a private ``x-ppbase-error-kind``
header naming one of :func:`error_kinds`, set only while the mode is on and
always removed by the middleware.  A tagged error skips the message table.
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

#: Private response header naming the PocketBase 0.22.34 error a handler
#: reproduces (a key of ``_ERROR_KINDS``).  Only set while the 0.22 mode is
#: on, and always stripped by :class:`ErrorCompatMiddleware`.
ERROR_KIND_HEADER = "x-ppbase-error-kind"
_ERROR_KIND_HEADER_BYTES = ERROR_KIND_HEADER.encode("latin-1")


@dataclass(frozen=True)
class _ErrorKind:
    """A 0.22.34 error that the response body alone does not identify."""

    #: ``""``: the default message of the status (``NewXxxError("", err)``).
    message: str = ""
    #: ``True``: 0.22.34 passes a non-validation error, so ``data`` is ``{}``.
    clear_data: bool = False
    source: str = ""
    #: ``(path, methods, message)``: routes whose 0.22.34 handler words this
    #: error differently (first match wins, else ``message``).
    routes: tuple[tuple[re.Pattern[str], frozenset[str], str], ...] = ()

    def message_for(self, method: str, path: str) -> str:
        for pattern, methods, message in self.routes:
            if method in methods and pattern.match(path):
                return message
        return self.message


_ERROR_KINDS: dict[str, _ErrorKind] = {
    "form": _ErrorKind(
        source="apis/base.go l.75-80: a form validation error returned "
        "unwrapped becomes NewBadRequestError('', err) with its fields as data "
        "(record update, any caller: apis/record_crud.go l.372 form.Submit -> "
        "forms/record_upsert.go l.744-747; record create by an admin: "
        "record_crud.go l.280, the createRule DrySubmit l.215-261 being "
        "skipped; admin auth-with-password: apis/admin.go l.92, "
        "forms/admin_login.go l.53-56)"
    ),
    "create_rule_failed": _ErrorKind(
        "Failed to create record.",
        clear_data=True,
        source="apis/record_crud.go l.250-261: a createRule that does not match "
        "fails the DrySubmit lookup (l.251-253), a plain error wrapped as "
        "NewBadRequestError('Failed to create record.', testErr): no "
        "validation data (apis/api_error.go l.77-90)",
    ),
    "body_load": _ErrorKind(
        "An error occurred while loading the submitted data.",
        clear_data=True,
        routes=(
            (
                re.compile(r"^/api/admins/?$"),
                frozenset({"POST"}),
                "Failed to load the submitted data due to invalid formatting.",
            ),
            (
                re.compile(r"^/api/admins/[^/]+/?$"),
                frozenset({"PATCH"}),
                "Failed to load the submitted data due to invalid formatting.",
            ),
        ),
        source="c.Bind failure of a body that is not a JSON object of the form "
        "(PPBase: a pydantic body model rejects the JSON itself): "
        "apis/admin.go l.83-85 (auth-with-password), apis/backup.go l.76-77, "
        "apis/settings.go l.52-54; admin create l.247-249 and update "
        "l.293-295 say 'Failed to load the submitted data due to invalid "
        "formatting.'; not a validation error, so data is {} (apis/api_error.go "
        "l.77-90)",
    ),
    "query": _ErrorKind(
        clear_data=True,
        source="apis/record_crud.go l.83-86: a search provider parse error "
        "(filter, sort, page, perPage, skipTotal) is NewBadRequestError('', "
        "err); err is not a validation error, so safeErrorsData gives {} "
        "(apis/api_error.go l.77-90)",
    ),
    "guest_only": _ErrorKind(
        "The request can be accessed only by guests.",
        source="apis/middlewares.go l.37 (RequireGuestOnly)",
    ),
    "record_auth_required": _ErrorKind(
        "The request requires valid record authorization token to be set.",
        source="apis/middlewares.go l.77 (RequireRecordAuth), "
        "l.99 (RequireSameContextRecordAuth)",
    ),
    "record_collection_forbidden": _ErrorKind(
        "The authorized record model is not allowed to perform this action.",
        source="apis/middlewares.go l.82 (RequireRecordAuth), "
        "l.169 (RequireAdminOrRecordAuth)",
    ),
    "admin_auth_required": _ErrorKind(
        "The request requires valid admin authorization token to be set.",
        source="apis/middlewares.go l.119 (RequireAdminAuth), "
        "l.147 (RequireAdminAuthOnlyIfAny)",
    ),
    "admin_or_record_auth_required": _ErrorKind(
        "The request requires admin or record authorization token to be set.",
        source="apis/middlewares.go l.165 (RequireAdminOrRecordAuth), "
        "l.193 (RequireAdminOrOwnerAuth)",
    ),
    "owner_forbidden": _ErrorKind(
        "You are not allowed to perform this request.",
        source="apis/middlewares.go l.204 (RequireAdminOrOwnerAuth)",
    ),
}

# forms/validators/record_data.go l.21 and l.79-83: every required schema
# field of a record form fails with this error.
_RECORD_REQUIRED = "Missing required value."

# ozzo-validation ``validation.Required`` ("cannot be blank", sentenized by
# resolveSafeErrorItem, apis/api_error.go l.117-130), the 0.22.34 form
# counterpart of pydantic's missing-field message.
_PYDANTIC_MISSING = "Field required"
_OZZO_REQUIRED = "Cannot be blank."


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


def error_kinds() -> dict[str, _ErrorKind]:
    """The error kinds a handler can tag (for documentation and tests)."""
    return dict(_ERROR_KINDS)


def _checked_kind(kind: str) -> str:
    if kind not in _ERROR_KINDS:
        raise ValueError(f"Unknown error compatibility kind {kind!r}.")
    return kind


def error_kind_headers(app: Any, kind: str) -> dict[str, str] | None:
    """Headers tagging an error with ``kind`` when the 0.22 mode is on.

    For errors raised as ``HTTPException(..., headers=...)``; ``None`` (no
    header at all) while the mode is off, so PPBase's own responses are
    unchanged.
    """
    _checked_kind(kind)
    if get_error_compat(app) != POCKETBASE_022:
        return None
    return {ERROR_KIND_HEADER: kind}


def mark_error(request: Any, response: Any, kind: str) -> Any:
    """Tag an error response with the 0.22.34 error it reproduces.

    A no-op while the mode is off.  ``request`` is the Starlette request (or
    anything with an ``app``); returns ``response`` for chaining.
    """
    _checked_kind(kind)
    app = getattr(request, "app", None)
    if app is not None and get_error_compat(app) == POCKETBASE_022:
        response.headers[ERROR_KIND_HEADER] = kind
    return response


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
    # Apply only when this predicate accepts the PPBase data.
    data_match: Callable[[Any], bool] | None = None
    # The 0.22.34 data, computed from the PPBase data.
    data_transform: Callable[[Any], Any] | None = None
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
        if self.data_match is not None and not self.data_match(data):
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


def _is_required_fields_data(data: Any) -> bool:
    """PPBase's password-login form errors: ``{field: {} | required}``."""
    if not isinstance(data, dict) or not data:
        return False
    failing = 0
    for value in data.values():
        if value == {}:
            continue
        if not isinstance(value, dict) or value.get("code") != "validation_required":
            return False
        failing += 1
    return failing > 0


def _failing_fields(data: Any) -> dict[str, Any]:
    """Only the failing fields, like ozzo ``ValidateStruct``."""
    return {key: value for key, value in data.items() if value != {}}


def _ozzo_required_messages(data: Any) -> Any:
    """Pydantic's missing-field text in ozzo ``Required`` words."""
    if not isinstance(data, dict):
        return data
    result: dict[str, Any] = {}
    for key, value in data.items():
        if (
            isinstance(value, dict)
            and value.get("code") == "validation_required"
            and value.get("message") == _PYDANTIC_MISSING
        ):
            value = {**value, "message": _OZZO_REQUIRED}
        result[key] = value
    return result


_COLLECTION_ROUTE = re.compile(r"^/api/collections/[^/]+/")
_PASSWORD_AUTH_ROUTE = re.compile(
    r"^/api/(?:collections/[^/]+|admins)/auth-with-password/?$"
)
_RECORD_PASSWORD_AUTH_ROUTE = re.compile(
    r"^/api/collections/[^/]+/auth-with-password/?$"
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
_SETTINGS_TEST_EMAIL_ROUTE = re.compile(r"^/api/settings/test/email/?$")
_BACKUPS_ROUTE = re.compile(r"^/api/backups/?$")
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
        pattern=re.compile(r"^Failed to update record\. Validation failed for: .*$"),
        replacement="Something went wrong while processing your request.",
        path=_RECORD_WRITE_ROUTE,
        methods=frozenset({"PATCH"}),
        source="apis/record_crud.go l.372 form.Submit: an update validation "
        "error is returned unwrapped for every caller (forms/record_upsert.go "
        "l.744-747; only a persistence error is 'Failed to update record.', "
        "l.377-379) and becomes NewBadRequestError('', err) in apis/base.go "
        "l.75-80",
    ),
    _MessageRule(
        400,
        pattern=re.compile(r"^Failed to create record\. Validation failed for: .*$"),
        replacement="Failed to create record.",
        path=_RECORD_WRITE_ROUTE,
        methods=frozenset({"POST"}),
        source="apis/record_crud.go l.250-261: the createRule DrySubmit of a "
        "non-admin validates the form first (forms/record_upsert.go l.702) and "
        "wraps the error, without a field list (an admin's create validation "
        "error is tagged 'form' by the handler: l.280, apis/base.go l.75-80)",
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
        messages=("Invalid UTF-8 JSON body.",),
        replacement="An error occurred while loading the submitted data.",
        path=_BACKUPS_ROUTE,
        methods=frozenset({"POST"}),
        source="apis/backup.go l.76-77 (c.Bind failure of the backup create form)",
    ),
    _MessageRule(
        400,
        messages=("Failed to send the test email.",),
        replacement="An error occurred while loading the submitted data.",
        path=_SETTINGS_TEST_EMAIL_ROUTE,
        methods=frozenset({"POST"}),
        data={},
        data_codes={"body": "validation_invalid_value"},
        source="apis/settings.go l.112-115 (c.Bind failure of the test email form)",
    ),
    _MessageRule(
        400,
        messages=("Failed to authenticate.",),
        replacement="Something went wrong while processing your request.",
        path=_RECORD_PASSWORD_AUTH_ROUTE,
        methods=frozenset({"POST"}),
        data_match=_is_required_fields_data,
        data_transform=_failing_fields,
        source="apis/record_auth.go l.311 form.Submit: a missing identity or "
        "password fails forms/record_password_login.go l.43-46 before the "
        "'Failed to authenticate.' interceptor (l.56-57, record_auth.go "
        "l.316-317), so it is NewBadRequestError('', err) (apis/base.go "
        "l.75-80) with only the failing fields",
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


def compat_record_errors(
    request: Any, collection: Any, errors: dict[str, Any]
) -> dict[str, Any]:
    """The 0.22.34 ``data`` of a record form error (unchanged while off).

    PocketBase 0.22.34 reports every missing required *schema* field as
    "Missing required value." (``validation_required``,
    forms/validators/record_data.go l.21, l.79-83); auth system fields
    (``email``, ``password``, ...) keep ozzo's "Cannot be blank."
    (forms/record_upsert.go l.472-563).  PPBase words both "Cannot be
    blank." (bool: "Must be true.").  Only the handler knows the schema, so
    it passes the collection.
    """
    app = getattr(request, "app", None)
    if app is None or get_error_compat(app) != POCKETBASE_022:
        return errors
    if not isinstance(errors, dict):
        return errors
    schema = getattr(collection, "schema", None) or []
    fields = {
        str(field.get("name", ""))
        for field in schema
        if isinstance(field, dict) and not field.get("system", False)
    }
    if str(getattr(collection, "type", "") or "") == "auth":
        fields -= {"email", "emailVisibility", "verified", "username", "password"}
    result: dict[str, Any] = {}
    for key, value in errors.items():
        if (
            key in fields
            and isinstance(value, dict)
            and value.get("code") == "validation_required"
        ):
            value = {**value, "message": _RECORD_REQUIRED}
        result[key] = value
    return result


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
    kind: str | None = None,
) -> dict[str, Any] | None:
    """The PocketBase 0.22.34 error body of a PPBase error body, else ``None``.

    ``kind`` is the tag of :func:`mark_error`; an unknown tag is ignored.
    """
    parsed = _ppbase_error(body)
    if parsed is None:
        return None
    message, data = parsed
    spec = _ERROR_KINDS.get(kind) if kind else None
    if spec is not None:
        message = spec.message_for(method.upper(), path)
        data = {} if spec.clear_data else _ozzo_required_messages(data)
    else:
        for rule in _MESSAGE_RULES:
            replacement = rule.apply(status, message, method.upper(), path, data)
            if replacement is not None:
                message = replacement
                if rule.data is not None:
                    data = json.loads(json.dumps(rule.data))
                elif rule.data_transform is not None:
                    data = rule.data_transform(data)
                break
    message = sentenize(message) or _DEFAULT_MESSAGES.get(status, "")
    # safeErrorsData: always a JSON object (api_error.go l.77-90).
    return {
        "code": status,
        "message": message,
        "data": data if isinstance(data, dict) else {},
    }


# Go's encoding/json escapes these for HTML safety (HTMLEscape); echo's
# default JSON serializer keeps that behavior.
_GO_HTML_ESCAPES = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    ("\u2028", "\\u2028"),
    ("\u2029", "\\u2029"),
)


def _sorted_maps(value: Any) -> Any:
    """Go encodes a ``map[string]any`` with its keys sorted."""
    if isinstance(value, dict):
        return {key: _sorted_maps(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted_maps(item) for item in value]
    return value


def encode_like_pocketbase_022(error: dict[str, Any]) -> bytes:
    """The bytes PocketBase 0.22.34 writes for an ``ApiError``.

    ``e.HttpContext.JSON(apiErr.Code, apiErr)`` (apis/base.go l.104) goes
    through echo's ``json.NewEncoder(...).Encode``: the struct fields in
    order (``code``, ``message``, ``data``, apis/api_error.go l.12-15), the
    ``data`` map with sorted keys, ``<``, ``>``, ``&``, U+2028 and U+2029
    escaped, non-ASCII text as UTF-8 and a final newline.  Measured on the
    0.22.34 binary: ``{"code":404,"message":"The requested resource wasn't
    found.","data":{}}\\n`` (72 bytes) and ``"A\\u003cb\\u003ec\\u0026d\\u2028e"``
    for a ``"a<b>c&d\\u2028e"`` message.
    """
    ordered = {
        "code": error["code"],
        "message": error["message"],
        "data": _sorted_maps(error["data"]),
    }
    text = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    for char, escape in _GO_HTML_ESCAPES:
        text = text.replace(char, escape)
    return (text + "\n").encode("utf-8")


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
        error_kind: str | None = None
        chunks: list[bytes] = []

        async def wrapped_send(message: Message) -> None:
            nonlocal start, error_kind
            message_type = message.get("type")
            if message_type == "http.response.start":
                status = int(message.get("status", 200))
                headers = list(message.get("headers", []))
                marker = _header(headers, _ERROR_KIND_HEADER_BYTES)
                if marker is not None:
                    error_kind = marker.decode("latin-1")
                    headers = [
                        (key, value)
                        for key, value in headers
                        if key.lower() != _ERROR_KIND_HEADER_BYTES
                    ]
                    message = {**message, "headers": headers}
                content_type = (_header(headers, b"content-type") or b"").lower()
                if status >= 400 and content_type.startswith(b"application/json"):
                    start = message
                    return
                await send(message)
                return
            if message_type == "http.response.body" and start is not None:
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
            rewritten = pocketbase_022_error(
                status, decoded, method=method, path=path, kind=error_kind
            )
            if rewritten is None:
                await send(start_message)
                await send(
                    {"type": "http.response.body", "body": raw, "more_body": False}
                )
                return
            body = encode_like_pocketbase_022(rewritten)
            headers = [
                (key, value)
                for key, value in start_message.get("headers", [])
                if key.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(body)).encode("latin-1")))
            await send({**start_message, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, receive, wrapped_send)
