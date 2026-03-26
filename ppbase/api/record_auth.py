"""FastAPI routes for auth collection endpoints.

Provides authentication endpoints for collections of type ``auth``:
- GET    /api/collections/{collection}/auth-methods
- POST   /api/collections/{collection}/auth-with-password
- POST   /api/collections/{collection}/request-otp
- POST   /api/collections/{collection}/auth-with-otp
- POST   /api/collections/{collection}/auth-with-oauth2
- POST   /api/collections/{collection}/auth-refresh
- POST   /api/collections/{collection}/impersonate/{id}
- POST   /api/collections/{collection}/request-verification
- POST   /api/collections/{collection}/confirm-verification
- POST   /api/collections/{collection}/request-password-reset
- POST   /api/collections/{collection}/confirm-password-reset
- POST   /api/collections/{collection}/request-email-change
- POST   /api/collections/{collection}/confirm-email-change
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from ppbase.ext.events import RecordAuthRequestEvent
from ppbase.ext.registry import (
    HOOK_RECORD_AUTH_WITH_OTP_REQUEST,
    HOOK_RECORD_AUTH_REFRESH_REQUEST,
    HOOK_RECORD_AUTH_REQUEST,
    HOOK_RECORD_AUTH_WITH_OAUTH2_REQUEST,
    HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
    HOOK_RECORD_REQUEST_OTP_REQUEST,
    get_extension_registry,
)
from ppbase.models.field_types import _EMAIL_RE
from ppbase.services.expand_service import expand_records
from ppbase.services.record_service import (
    check_record_rule,
    get_all_collections,
    resolve_collection,
)
from ppbase.services.rule_engine import check_rule

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_response(status: int, message: str, data: Any = None) -> JSONResponse:
    body: dict[str, Any] = {
        "status": status,
        "message": message,
        "data": data or {},
    }
    return JSONResponse(content=body, status_code=status)


def _require_auth_collection(collection):
    """Return an error response if the collection is not type ``auth``."""
    col_type = getattr(collection, "type", "base") or "base"
    if col_type != "auth":
        return _error_response(
            404,
            f"The collection \"{collection.name}\" is not an auth collection.",
        )
    return None


def _apply_fields_filter(record: dict[str, Any], fields_param: str) -> dict[str, Any]:
    """Filter record keys to only those listed in the ``fields`` query param."""
    if not fields_param:
        return record
    allowed = {f.strip() for f in fields_param.split(",") if f.strip()}
    if not allowed or "*" in allowed:
        return record
    return {k: v for k, v in record.items() if k in allowed}


def _extract_auth_token(request: Request) -> str:
    """Extract raw auth token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header
    if token.lower().startswith("bearer "):
        token = token[7:]
    return token.strip()


def _normalize_oauth_base_url(base_url: str) -> str:
    """Normalize callback base URL for OAuth2 providers.

    Browsers and OAuth providers don't reliably accept ``0.0.0.0`` as a callback
    host. When detected, rewrite it to ``127.0.0.1``.
    """
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    if host not in {"0.0.0.0", "::"}:
        return base_url

    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"127.0.0.1{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _normalize_oauth_provider_auth_url(auth_url: str) -> str:
    """Ensure provider auth URL ends with ``redirect_uri=`` placeholder.

    PocketBase SDK concatenates the final callback URL to ``authUrl``.
    Therefore, ``redirect_uri`` must be the last query parameter and have an empty value.
    """
    base, sep, query = auth_url.partition("?")
    if not sep:
        return auth_url

    parsed = parse_qsl(query, keep_blank_values=True)
    items: list[tuple[str, str]] = []
    for key, value in parsed:
        if key != "redirect_uri":
            items.append((key, value))

    items.append(("redirect_uri", ""))
    normalized_query = urlencode(items, doseq=True)
    return f"{base}?{normalized_query}"


def _extract_first_header_value(value: str | None) -> str:
    """Return first comma-separated header value."""
    if not value:
        return ""
    return value.split(",")[0].strip()


async def _resolve_public_base_url(request: Request, engine: Any) -> str:
    """Resolve externally accessible app base URL.

    Resolution order:
    1) ``settings.meta.appURL`` from persisted settings
    2) reverse-proxy headers (``X-Forwarded-*``)
    3) request base URL fallback
    """
    configured_url = ""
    try:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from ppbase.db.system_tables import ParamRecord

        factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with factory() as session:
            stmt = select(ParamRecord.value).where(ParamRecord.key == "settings")
            raw_settings = (await session.execute(stmt)).scalar_one_or_none()

        if isinstance(raw_settings, dict):
            meta = raw_settings.get("meta")
            if isinstance(meta, dict):
                configured_url = str(meta.get("appURL", "") or "").strip().rstrip("/")
    except Exception:
        configured_url = ""

    if configured_url:
        parsed = urlsplit(configured_url)
        if parsed.scheme and parsed.netloc:
            normalized = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
            )
            return _normalize_oauth_base_url(normalized.rstrip("/"))

    scheme = _extract_first_header_value(request.headers.get("x-forwarded-proto"))
    host = _extract_first_header_value(request.headers.get("x-forwarded-host"))
    forwarded_port = _extract_first_header_value(request.headers.get("x-forwarded-port"))

    if not scheme:
        scheme = request.url.scheme
    if not host:
        host = request.headers.get("host", "").strip()

    if host and forwarded_port and ":" not in host:
        is_default_http = scheme == "http" and forwarded_port == "80"
        is_default_https = scheme == "https" and forwarded_port == "443"
        if not is_default_http and not is_default_https:
            host = f"{host}:{forwarded_port}"

    if host:
        return _normalize_oauth_base_url(f"{scheme}://{host}".rstrip("/"))

    return _normalize_oauth_base_url(str(request.base_url).rstrip("/"))


async def _extract_oauth2_redirect_payload(request: Request) -> dict[str, str]:
    """Extract OAuth2 callback payload from query/body."""
    payload: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        payload[str(key)] = str(value)

    if request.method.upper() != "POST":
        return payload

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            data = await request.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            for key, value in data.items():
                if value is None or key in payload:
                    continue
                payload[str(key)] = str(value)
        return payload

    try:
        form = await request.form()
    except Exception:
        return payload

    for key in form.keys():
        if key in payload:
            continue
        value = form.get(key)
        if value is not None:
            payload[str(key)] = str(value)
    return payload


async def _dispatch_oauth2_realtime_payload(
    subscription_manager: Any | None,
    payload: dict[str, str],
) -> bool:
    """Push OAuth2 callback payload to the matching realtime client session."""
    state = str(payload.get("state", "") or "").strip()
    if not state or subscription_manager is None:
        return False

    get_session = getattr(subscription_manager, "get_session", None)
    if not callable(get_session):
        return False

    session = get_session(state)
    if session is None:
        return False

    await session.response_queue.put(
        {"topic": "@oauth2", "data": dict(payload)}
    )
    return True


def _oauth2_redirect_html(payload: dict[str, str]) -> str:
    """Render popup relay HTML used by OAuth2 integrations."""
    payload_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OAuth2 Redirect</title>
  </head>
  <body>
    <script>
      (function () {{
        const payload = {payload_json};
        const browserPayload = {{
          query: window.location.search || '',
          hash: window.location.hash || '',
          href: window.location.href || '',
        }};
        const message = Object.assign({{}}, payload, browserPayload);

        try {{
          if (window.opener && typeof window.opener.postMessage === 'function') {{
            window.opener.postMessage(message, window.location.origin);
          }}
        }} catch (_) {{
          // Ignore postMessage delivery errors.
        }}

        try {{ window.close(); }} catch (_) {{}}
      }})();
    </script>
    <noscript>OAuth2 redirect page</noscript>
  </body>
</html>
"""


async def _trigger_record_auth_request_hook(
    request: Request,
    specific_hook_name: str,
    event: RecordAuthRequestEvent,
    default_handler,
):
    """Trigger auth request hook for a specific auth API action."""
    extensions = get_extension_registry(request.app)
    if extensions is None:
        return await default_handler(event)
    specific_hook = extensions.hooks.get(specific_hook_name)
    return await specific_hook.trigger(event, default_handler)


async def _trigger_record_auth_success_hook(
    request: Request,
    event: RecordAuthRequestEvent,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Trigger the generic auth-success hook after successful authentication."""
    extensions = get_extension_registry(request.app)
    if extensions is None:
        return data

    generic_hook = extensions.hooks.get(HOOK_RECORD_AUTH_REQUEST)

    # Keep event fields in sync with mutable auth result payload.
    if "record" in data:
        event.record = data.get("record")
    if "token" in data:
        token_value = data.get("token")
        event.token = str(token_value) if token_value is not None else None
    if "meta" in data and isinstance(data.get("meta"), dict):
        event.meta = dict(data.get("meta") or {})

    async def _default(e: RecordAuthRequestEvent) -> dict[str, Any]:
        result = dict(data)
        if e.record is not None:
            result["record"] = e.record
        if e.token is not None:
            result["token"] = e.token
        if e.meta is not None:
            result["meta"] = e.meta
        return result

    hooked_result = await generic_hook.trigger(event, _default)
    return hooked_result if isinstance(hooked_result, dict) else data


_AUTH_INVALID_CREDENTIALS_DATA = {
    "identity": {
        "code": "validation_invalid_credentials",
        "message": "Invalid login credentials.",
    }
}


def _build_rule_request_context(
    request: Request,
    *,
    context: str,
    data: dict[str, Any] | None = None,
    auth_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build request context object for authRule filter evaluation."""
    auth_info: dict[str, Any] = {}
    if auth_payload:
        auth_info = {
            "id": auth_payload.get("id", ""),
            "email": auth_payload.get("email", ""),
            "type": auth_payload.get("type", ""),
            "collectionId": auth_payload.get("collectionId", ""),
            "collectionName": auth_payload.get("collectionName", ""),
        }

    headers_info: dict[str, str] = {}
    for key, value in request.headers.items():
        lower = key.lower()
        headers_info[lower] = value
        headers_info[lower.replace("-", "_")] = value

    return {
        "context": context,
        "method": request.method.upper(),
        "headers": headers_info,
        "auth": auth_info,
        "data": data or {},
        "query": dict(request.query_params),
    }


async def _passes_auth_rule(
    engine: Any,
    collection: Any,
    record_id: str,
    request_context: dict[str, Any],
) -> bool:
    """Return whether the target record satisfies ``collection.options.authRule``."""
    options = getattr(collection, "options", None) or {}
    auth_rule = options.get("authRule")
    rule_result = check_rule(auth_rule, None)
    if rule_result is True:
        return True
    if rule_result is False:
        return False
    return await check_record_rule(
        engine,
        collection,
        record_id,
        str(rule_result),
        request_context,
    )


# ---------------------------------------------------------------------------
# GET auth-methods
# ---------------------------------------------------------------------------


@router.api_route("/api/oauth2-redirect", methods=["GET", "POST"])
async def api_oauth2_redirect(request: Request) -> HTMLResponse:
    """Handle OAuth2 callback relay for realtime and popup integrations."""
    payload = await _extract_oauth2_redirect_payload(request)
    subscription_manager = getattr(request.app.state, "subscription_manager", None)
    await _dispatch_oauth2_realtime_payload(subscription_manager, payload)
    return HTMLResponse(content=_oauth2_redirect_html(payload), status_code=200)


@router.get("/api/collections/{collectionIdOrName}/auth-methods")
async def api_auth_methods(collectionIdOrName: str, request: Request) -> JSONResponse:
    """Return available auth methods for the collection."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    # Read identity fields from collection options
    opts = collection.options or {}
    pa = opts.get("passwordAuth", {})
    identity_fields = pa.get("identityFields", ["email"])
    enabled = pa.get("enabled", True)

    # Determine legacy compatibility flags
    email_password = enabled and "email" in identity_fields
    username_password = enabled and "username" in identity_fields

    # Build OAuth2 providers list
    oauth2_config = opts.get("oauth2", {})
    oauth2_enabled = oauth2_config.get("enabled", False)
    oauth2_providers = []
    mfa_opts = opts.get("mfa", {}) or {}
    otp_opts = opts.get("otp", {}) or {}
    mfa_enabled = bool(mfa_opts.get("enabled", False))
    otp_enabled = bool(otp_opts.get("enabled", False))
    mfa_duration = int(mfa_opts.get("duration", 0) or 0) if mfa_enabled else 0
    otp_duration = int(otp_opts.get("duration", 0) or 0) if otp_enabled else 0

    if oauth2_enabled:
        from ppbase.services.oauth2_service import (
            generate_pkce_pair,
            generate_state,
            get_provider_class,
            get_provider_credentials,
        )

        settings = request.app.state.settings

        # Get configured providers from collection options or environment
        provider_names = []
        if oauth2_config.get("providers"):
            provider_names = [p.get("name") for p in oauth2_config["providers"] if p.get("name")]
        else:
            # Check environment for any configured providers
            for provider_name in ["google", "github", "gitlab", "discord", "facebook"]:
                try:
                    get_provider_credentials(provider_name, settings, opts)
                    provider_names.append(provider_name)
                except Exception:
                    pass

        # Build provider configs with auth URLs
        for provider_name in provider_names:
            try:
                client_id, client_secret = get_provider_credentials(provider_name, settings, opts)
                provider_class = get_provider_class(provider_name)
                provider = provider_class(client_id, client_secret)

                # Generate PKCE pair and state
                code_verifier, code_challenge = generate_pkce_pair()
                state = generate_state()

                # Return auth URL with ``redirect_uri=`` placeholder.
                # Clients (SDK/manual) append their callback URL.
                auth_url = provider.get_auth_url(state, code_challenge, "")
                auth_url = _normalize_oauth_provider_auth_url(auth_url)

                oauth2_providers.append({
                    "name": provider_name,
                    "displayName": provider_name.capitalize(),
                    "state": state,
                    "codeVerifier": code_verifier,
                    "codeChallenge": code_challenge,
                    "codeChallengeMethod": "S256",
                    "authURL": auth_url,
                    "authUrl": auth_url,
                })
            except Exception:
                # Skip providers that aren't configured
                pass

    return JSONResponse(
        content={
            # PocketBase SDK v0.x compatibility fields
            "usernamePassword": username_password,
            "emailPassword": email_password,
            "authProviders": oauth2_providers,  # Legacy field
            # Newer structured format
            "password": {
                "enabled": enabled,
                "identityFields": identity_fields,
            },
            "oauth2": {
                "enabled": oauth2_enabled and len(oauth2_providers) > 0,
                "providers": oauth2_providers,
            },
            "mfa": {
                "enabled": mfa_enabled,
                "duration": mfa_duration,
            },
            "otp": {
                "enabled": otp_enabled,
                "duration": otp_duration,
            },
        }
    )


# ---------------------------------------------------------------------------
# POST auth-with-password
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/auth-with-password")
async def api_auth_with_password(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Authenticate a user with identity + password.

    Supports all auth collections including _superusers.
    """
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")
    if not isinstance(body, dict):
        return _error_response(400, "Request body must be a JSON object.")

    event = RecordAuthRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        method="password",
        auth_method="password",
        body=body,
        identity=str(body.get("identity", "") or ""),
        identity_field=str(body.get("identityField", "") or "") or None,
        password=str(body.get("password", "") or ""),
    )

    async def _default_auth_with_password(e: RecordAuthRequestEvent) -> JSONResponse:
        if not isinstance(e.body, dict):
            return _error_response(400, "Request body must be a JSON object.")

        payload = dict(e.body)
        identity = str(
            e.identity if e.identity is not None else payload.get("identity", "")
        ).strip()
        password = str(
            e.password if e.password is not None else payload.get("password", "")
        )
        identity_field: str | None = e.identity_field
        if identity_field is None:
            raw_identity_field = payload.get("identityField")
            identity_field = str(raw_identity_field).strip() if raw_identity_field else None

        if not identity or not password:
            return _error_response(
                400,
                "Failed to authenticate.",
                {
                    "identity": {"code": "validation_required", "message": "Cannot be blank."}
                    if not identity
                    else {},
                    "password": {"code": "validation_required", "message": "Cannot be blank."}
                    if not password
                    else {},
                },
            )

        settings = request.app.state.settings
        from ppbase.services.record_auth_service import auth_with_password

        result = await auth_with_password(
            engine,
            collection,
            identity,
            password,
            settings,
            identity_field=identity_field,
        )

        if result is None:
            return _error_response(
                400,
                "Failed to authenticate.",
                _AUTH_INVALID_CREDENTIALS_DATA,
            )

        rule_request_context = _build_rule_request_context(
            request,
            context="password",
            data=payload,
        )
        if not await _passes_auth_rule(
            engine,
            collection,
            result["record"]["id"],
            rule_request_context,
        ):
            return _error_response(
                400,
                "Failed to authenticate.",
                _AUTH_INVALID_CREDENTIALS_DATA,
            )

        expand_str = request.query_params.get("expand", "")
        if expand_str and result.get("record"):
            all_colls = await get_all_collections(engine)
            records = await expand_records(
                engine, collection, [result["record"]], expand_str, all_colls,
            )
            result["record"] = records[0] if records else result["record"]

        fields_param = request.query_params.get("fields", "")
        if fields_param and result.get("record"):
            result["record"] = _apply_fields_filter(result["record"], fields_param)

        result = await _trigger_record_auth_success_hook(request, e, result)
        return JSONResponse(content=result)

    return await _trigger_record_auth_request_hook(
        request,
        HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
        event,
        _default_auth_with_password,
    )


# ---------------------------------------------------------------------------
# POST request-otp
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/request-otp")
async def api_request_otp(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Request a one-time password for an auth record email."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    opts = collection.options or {}
    otp_cfg = opts.get("otp", {}) or {}
    if not bool(otp_cfg.get("enabled", False)):
        return _error_response(
            400,
            f"OTP authentication is not enabled for collection \"{collection.name}\".",
        )

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")
    if not isinstance(body, dict):
        return _error_response(400, "Request body must be a JSON object.")

    event = RecordAuthRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        method="requestOtp",
        auth_method="requestOtp",
        body=body,
        email=str(body.get("email", "") or ""),
    )

    async def _default_request_otp(e: RecordAuthRequestEvent) -> JSONResponse:
        if not isinstance(e.body, dict):
            return _error_response(400, "Request body must be a JSON object.")

        payload = dict(e.body)
        email = str(e.email if e.email is not None else payload.get("email", "")).strip()
        if not email:
            return _error_response(
                400,
                "Something went wrong while processing your request.",
                {"email": {"code": "validation_required", "message": "Cannot be blank."}},
            )
        if not _EMAIL_RE.match(email):
            return _error_response(
                400,
                "Something went wrong while processing your request.",
                {
                    "email": {
                        "code": "validation_invalid_email",
                        "message": "Must be a valid email address.",
                    }
                },
            )

        settings = request.app.state.settings
        from ppbase.services.record_auth_service import request_otp as _request_otp

        otp_id, rate_limited = await _request_otp(
            engine,
            collection,
            email,
            settings,
        )
        if rate_limited:
            return _error_response(
                429,
                "You've send too many OTP requests, please try again later.",
            )

        return JSONResponse(content={"otpId": otp_id}, status_code=200)

    return await _trigger_record_auth_request_hook(
        request,
        HOOK_RECORD_REQUEST_OTP_REQUEST,
        event,
        _default_request_otp,
    )


# ---------------------------------------------------------------------------
# POST auth-with-otp
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/auth-with-otp")
async def api_auth_with_otp(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Authenticate a user with OTP request ID + OTP password."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    opts = collection.options or {}
    otp_cfg = opts.get("otp", {}) or {}
    if not bool(otp_cfg.get("enabled", False)):
        return _error_response(
            400,
            f"OTP authentication is not enabled for collection \"{collection.name}\".",
        )

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")
    if not isinstance(body, dict):
        return _error_response(400, "Request body must be a JSON object.")

    event = RecordAuthRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        method="otp",
        auth_method="otp",
        body=body,
        otp_id=str(body.get("otpId", "") or ""),
        otp=str(body.get("password", "") or ""),
        password=str(body.get("password", "") or ""),
    )

    async def _default_auth_with_otp(e: RecordAuthRequestEvent) -> JSONResponse:
        if not isinstance(e.body, dict):
            return _error_response(400, "Request body must be a JSON object.")

        payload = dict(e.body)
        otp_id = str(
            e.otp_id if e.otp_id is not None else payload.get("otpId", "")
        ).strip()
        password = str(
            e.otp if e.otp is not None else payload.get("password", "")
        )
        if not otp_id or not password:
            return _error_response(
                400,
                "Failed to authenticate.",
                {
                    "otpId": {"code": "validation_required", "message": "Cannot be blank."}
                    if not otp_id
                    else {},
                    "password": {"code": "validation_required", "message": "Cannot be blank."}
                    if not password
                    else {},
                },
            )

        settings = request.app.state.settings
        from ppbase.services.record_auth_service import auth_with_otp

        result = await auth_with_otp(
            engine,
            collection,
            otp_id,
            password,
            settings,
        )
        if result is None:
            return _error_response(
                400,
                "Failed to authenticate.",
                _AUTH_INVALID_CREDENTIALS_DATA,
            )

        rule_request_context = _build_rule_request_context(
            request,
            context="otp",
            data=payload,
        )
        if not await _passes_auth_rule(
            engine,
            collection,
            result["record"]["id"],
            rule_request_context,
        ):
            return _error_response(
                400,
                "Failed to authenticate.",
                _AUTH_INVALID_CREDENTIALS_DATA,
            )

        expand_str = request.query_params.get("expand", "")
        if expand_str and result.get("record"):
            all_colls = await get_all_collections(engine)
            records = await expand_records(
                engine, collection, [result["record"]], expand_str, all_colls,
            )
            result["record"] = records[0] if records else result["record"]

        fields_param = request.query_params.get("fields", "")
        if fields_param and result.get("record"):
            result["record"] = _apply_fields_filter(result["record"], fields_param)

        result = await _trigger_record_auth_success_hook(request, e, result)
        return JSONResponse(content=result)

    return await _trigger_record_auth_request_hook(
        request,
        HOOK_RECORD_AUTH_WITH_OTP_REQUEST,
        event,
        _default_auth_with_otp,
    )


# ---------------------------------------------------------------------------
# POST auth-with-oauth2
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/auth-with-oauth2")
async def api_auth_with_oauth2(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Authenticate a user with OAuth2 authorization code."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")
    if not isinstance(body, dict):
        return _error_response(400, "Request body must be a JSON object.")

    event = RecordAuthRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        method="oauth2",
        auth_method="oauth2",
        body=body,
    )

    async def _default_auth_with_oauth2(e: RecordAuthRequestEvent) -> JSONResponse:
        if not isinstance(e.body, dict):
            return _error_response(400, "Request body must be a JSON object.")

        payload = dict(e.body)
        provider = str(payload.get("provider", "")).strip()
        code = str(payload.get("code", "")).strip()
        code_verifier = str(payload.get("codeVerifier", "")).strip()
        redirect_url_raw = payload.get("redirectUrl")
        if redirect_url_raw in (None, ""):
            redirect_url_raw = payload.get("redirectURL")
        if redirect_url_raw in (None, ""):
            redirect_url_raw = payload.get("redirect_url")
        redirect_url = str(redirect_url_raw or "").strip()
        create_data = payload.get("createData", {})

        if not provider:
            return _error_response(
                400,
                "Failed to authenticate.",
                {"provider": {"code": "validation_required", "message": "Cannot be blank."}},
            )

        if not code:
            return _error_response(
                400,
                "Failed to authenticate.",
                {"code": {"code": "validation_required", "message": "Cannot be blank."}},
            )

        if not code_verifier:
            return _error_response(
                400,
                "Failed to authenticate.",
                {"codeVerifier": {"code": "validation_required", "message": "Cannot be blank."}},
            )

        if not redirect_url:
            return _error_response(
                400,
                "Failed to authenticate.",
                {"redirectUrl": {"code": "validation_required", "message": "Cannot be blank."}},
            )

        opts = collection.options or {}
        oauth2_config = opts.get("oauth2", {})
        if not oauth2_config.get("enabled", False):
            return _error_response(
                400,
                f"OAuth2 authentication is not enabled for collection \"{collection.name}\".",
            )

        settings = request.app.state.settings
        from ppbase.services.oauth2_service import (
            get_provider_class,
            get_provider_credentials,
            link_or_create_oauth_user,
        )

        try:
            client_id, client_secret = get_provider_credentials(provider, settings, opts)
            provider_class = get_provider_class(provider)
            provider_instance = provider_class(client_id, client_secret)

            token_data = await provider_instance.exchange_code(
                code,
                redirect_url,
                code_verifier,
            )
            user_info = await provider_instance.get_user_info(token_data.get("access_token"))

            result = await link_or_create_oauth_user(
                engine,
                collection,
                provider,
                user_info,
                token_data,
                create_data,
            )

            rule_request_context = _build_rule_request_context(
                request,
                context="oauth2",
                data=payload,
            )
            if not await _passes_auth_rule(
                engine,
                collection,
                result["record"]["id"],
                rule_request_context,
            ):
                return _error_response(
                    400,
                    "Failed to authenticate.",
                    _AUTH_INVALID_CREDENTIALS_DATA,
                )

            from ppbase.services.record_auth_service import generate_record_auth_token

            token = await generate_record_auth_token(
                engine, collection, result["record"]["id"], settings
            )

            response_data = {
                "token": token,
                "record": result["record"],
                "meta": result["meta"],
            }

            expand_str = request.query_params.get("expand", "")
            if expand_str and response_data.get("record"):
                all_colls = await get_all_collections(engine)
                records = await expand_records(
                    engine, collection, [response_data["record"]], expand_str, all_colls,
                )
                response_data["record"] = records[0] if records else response_data["record"]

            fields_param = request.query_params.get("fields", "")
            if fields_param and response_data.get("record"):
                response_data["record"] = _apply_fields_filter(
                    response_data["record"],
                    fields_param,
                )

            response_data = await _trigger_record_auth_success_hook(
                request, e, response_data
            )
            return JSONResponse(content=response_data)

        except Exception as exc:
            message = str(exc)
            return _error_response(
                400,
                f"Failed to authenticate with OAuth2: {message}",
                {"provider": {"code": "validation_oauth2_failed", "message": message}},
            )

    return await _trigger_record_auth_request_hook(
        request,
        HOOK_RECORD_AUTH_WITH_OAUTH2_REQUEST,
        event,
        _default_auth_with_oauth2,
    )


# ---------------------------------------------------------------------------
# POST auth-refresh
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/auth-refresh")
async def api_auth_refresh(
    collectionIdOrName: str,
    request: Request,
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """Refresh an existing auth token."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    event = RecordAuthRequestEvent(
        app=request.app,
        request=request,
        collection=collection,
        collection_id_or_name=collectionIdOrName,
        auth=auth,
        method="refresh",
        auth_method="refresh",
        body={},
    )

    async def _default_auth_refresh(e: RecordAuthRequestEvent) -> JSONResponse:
        auth_header = request.headers.get("Authorization", "")
        token_str = auth_header
        if token_str.lower().startswith("bearer "):
            token_str = token_str[7:]
        token_str = token_str.strip()

        if not token_str:
            return _error_response(401, "The request requires valid auth token to be set.")

        settings = request.app.state.settings
        from ppbase.services.record_auth_service import (
            auth_refresh,
            verify_record_auth_token,
        )

        payload = await verify_record_auth_token(engine, collection, token_str, settings)
        if payload is None:
            return _error_response(401, "The request requires valid auth token to be set.")

        e.payload = payload
        result = await auth_refresh(engine, collection, payload, settings)
        if result is None:
            return _error_response(401, "The request requires valid auth token to be set.")

        rule_request_context = _build_rule_request_context(
            request,
            context="default",
            auth_payload=payload,
        )
        if not await _passes_auth_rule(
            engine,
            collection,
            result["record"]["id"],
            rule_request_context,
        ):
            return _error_response(401, "The request requires valid auth token to be set.")

        expand_str = request.query_params.get("expand", "")
        if expand_str and result.get("record"):
            all_colls = await get_all_collections(engine)
            records = await expand_records(
                engine, collection, [result["record"]], expand_str, all_colls,
            )
            result["record"] = records[0] if records else result["record"]

        fields_param = request.query_params.get("fields", "")
        if fields_param and result.get("record"):
            result["record"] = _apply_fields_filter(result["record"], fields_param)

        result = await _trigger_record_auth_success_hook(request, e, result)
        return JSONResponse(content=result)

    return await _trigger_record_auth_request_hook(
        request,
        HOOK_RECORD_AUTH_REFRESH_REQUEST,
        event,
        _default_auth_refresh,
    )


# ---------------------------------------------------------------------------
# POST impersonate/{id}
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/impersonate/{recordId}")
async def api_impersonate_record(
    collectionIdOrName: str,
    recordId: str,
    request: Request,
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """Generate a non-refreshable auth token for a target auth record."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    if auth is None:
        return _error_response(
            401,
            "An error occurred while validating the submitted data.",
        )

    if auth.get("type") != "admin":
        return _error_response(
            403,
            "The authorized record model is not allowed to perform this action.",
        )

    # Optional: body duration in seconds (0 or missing -> default collection duration).
    data: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        data = {k: form.get(k) for k in form.keys()}
    else:
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}

    duration_input = data.get("duration", 0)
    duration_value: int | None = None
    if duration_input not in (None, "", 0, "0"):
        try:
            duration_value = int(duration_input)
        except Exception:
            return _error_response(
                400,
                "Something went wrong while processing your request.",
                {
                    "duration": {
                        "code": "validation_not_a_number",
                        "message": "Must be a valid number.",
                    }
                },
            )
        if duration_value < 0:
            return _error_response(
                400,
                "Something went wrong while processing your request.",
                {
                    "duration": {
                        "code": "validation_min_greater_equal_than_required",
                        "message": "Must be no less than 0.",
                    }
                },
            )
        if duration_value == 0:
            duration_value = None

    settings = request.app.state.settings

    from ppbase.services.record_auth_service import impersonate_auth_record

    result = await impersonate_auth_record(
        engine,
        collection,
        recordId,
        settings,
        duration=duration_value,
    )
    if result is None:
        return _error_response(404, "The requested resource wasn't found.")

    # Apply expand if requested
    expand_str = request.query_params.get("expand", "")
    if expand_str and result.get("record"):
        all_colls = await get_all_collections(engine)
        records = await expand_records(
            engine, collection, [result["record"]], expand_str, all_colls,
        )
        result["record"] = records[0] if records else result["record"]

    # Apply fields filter if requested
    fields_param = request.query_params.get("fields", "")
    if fields_param and result.get("record"):
        result["record"] = _apply_fields_filter(result["record"], fields_param)

    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# POST request-verification
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/request-verification")
async def api_request_verification(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Request a verification email."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    email = body.get("email", "").strip()
    if not email:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"email": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    settings = request.app.state.settings
    base_url = await _resolve_public_base_url(request, engine)

    from ppbase.services.record_auth_service import request_verification as _req_verif

    await _req_verif(engine, collection, email, settings, base_url=base_url)

    # Always 204 to avoid enumeration
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST confirm-verification
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/confirm-verification")
async def api_confirm_verification(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Confirm a verification token."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    token = body.get("token", "").strip()
    if not token:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"token": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    settings = request.app.state.settings

    from ppbase.services.record_auth_service import confirm_verification as _confirm_verif

    ok = await _confirm_verif(engine, collection, token, settings)
    if not ok:
        return _error_response(
            400,
            "Failed to confirm verification.",
            {"token": {"code": "validation_invalid_token", "message": "Invalid or expired token."}},
        )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST request-password-reset
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/request-password-reset")
async def api_request_password_reset(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Request a password reset email."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    email = body.get("email", "").strip()
    if not email:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"email": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    settings = request.app.state.settings
    base_url = await _resolve_public_base_url(request, engine)

    from ppbase.services.record_auth_service import request_password_reset as _req_reset

    await _req_reset(engine, collection, email, settings, base_url=base_url)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST confirm-password-reset
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/confirm-password-reset")
async def api_confirm_password_reset(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Confirm a password reset token and set a new password."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    token = body.get("token", "").strip()
    password = body.get("password", "")
    password_confirm = body.get("passwordConfirm", "")

    if not token:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"token": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    settings = request.app.state.settings

    from ppbase.services.record_auth_service import confirm_password_reset as _confirm_reset

    ok, errors = await _confirm_reset(
        engine, collection, token, password, password_confirm, settings
    )

    if not ok:
        return _error_response(
            400,
            "Failed to confirm password reset.",
            errors or {},
        )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST request-email-change
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/request-email-change")
async def api_request_email_change(
    collectionIdOrName: str,
    request: Request,
    auth: dict[str, Any] | None = Depends(get_optional_auth),
) -> JSONResponse:
    """Request auth record email change."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    if auth is None or auth.get("type") != "authRecord":
        return _error_response(
            401,
            "The request requires valid record authorization token to be set.",
        )

    # A record token from another auth collection is valid, but cannot be used
    # to change emails in this collection.
    if auth.get("collectionId") != collection.id:
        return _error_response(
            403,
            "The authorized record model is not allowed to perform this action.",
        )

    token_str = _extract_auth_token(request)
    if not token_str:
        return _error_response(
            401,
            "The request requires valid record authorization token to be set.",
        )

    settings = request.app.state.settings

    from ppbase.services.record_auth_service import (
        request_email_change as _request_email_change,
        verify_record_auth_token,
    )

    payload = await verify_record_auth_token(engine, collection, token_str, settings)
    if payload is None:
        return _error_response(
            401,
            "The request requires valid record authorization token to be set.",
        )

    record_id = payload.get("id", "")
    if not record_id:
        return _error_response(
            401,
            "The request requires valid record authorization token to be set.",
        )

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    new_email = str(body.get("newEmail", "")).strip()
    if not new_email:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"newEmail": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    base_url = await _resolve_public_base_url(request, engine)
    ok, errors = await _request_email_change(
        engine,
        collection,
        record_id,
        new_email,
        settings,
        base_url=base_url,
    )
    if not ok:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            errors or {},
        )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST confirm-email-change
# ---------------------------------------------------------------------------


@router.post("/api/collections/{collectionIdOrName}/confirm-email-change")
async def api_confirm_email_change(
    collectionIdOrName: str,
    request: Request,
) -> JSONResponse:
    """Confirm auth record email change token."""
    engine = get_engine()
    collection = await resolve_collection(engine, collectionIdOrName)
    if collection is None:
        return _error_response(404, "Missing collection context.")

    err = _require_auth_collection(collection)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        return _error_response(400, "Invalid JSON body.")

    token = str(body.get("token", "")).strip()
    password = body.get("password", "")

    if not token:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"token": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    if not password:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            {"password": {"code": "validation_required", "message": "Cannot be blank."}},
        )

    settings = request.app.state.settings

    from ppbase.services.record_auth_service import confirm_email_change as _confirm_email_change

    ok, errors = await _confirm_email_change(
        engine,
        collection,
        token,
        password,
        settings,
    )
    if not ok:
        return _error_response(
            400,
            "Something went wrong while processing your request.",
            errors or {},
        )

    return Response(status_code=204)
