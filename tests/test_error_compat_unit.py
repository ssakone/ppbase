"""Unit tests of the opt-in PocketBase 0.22 error compatibility layer."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from ppbase.config import Settings
from ppbase.ext.flask_like_pb import FlaskLikePB
from ppbase.middleware.error_compat import (
    ERROR_KIND_HEADER,
    ErrorCompatMiddleware,
    encode_like_pocketbase_022,
    error_kind_headers,
    error_kinds,
    mark_error,
    message_rules,
    normalize_error_compat,
    pocketbase_022_error,
    sentenize,
    set_error_compat,
)

INVALID_FORMATTING = "Failed to load the submitted data due to invalid formatting."
LOAD_ERROR = "An error occurred while loading the submitted data."


def test_modes_are_explicit_and_off_by_default() -> None:
    assert Settings().error_compat == ""
    assert normalize_error_compat(None) == ""
    assert normalize_error_compat("") == ""
    assert normalize_error_compat("off") == ""
    assert normalize_error_compat("0.22") == "0.22"
    assert normalize_error_compat(" PocketBase-0.22 ") == "0.22"
    with pytest.raises(ValueError):
        normalize_error_compat("0.23")


def test_environment_variable_selects_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PPBASE_ERROR_COMPAT", "0.22")
    assert Settings().error_compat == "0.22"


def test_pb_facade_sets_the_mode_before_and_after_materialization() -> None:
    pb = FlaskLikePB(database_url="postgresql+asyncpg://u:p@127.0.0.1:1/none")
    assert pb.error_compat == ""
    pb.set_error_compat("0.22")
    assert pb.error_compat == "0.22"
    app = pb.get_app()
    assert app.state.error_compat == "0.22"
    pb.set_error_compat(None)
    assert app.state.error_compat == "" and pb.error_compat == ""
    with pytest.raises(ValueError):
        pb.set_error_compat("1.0")


def test_sentenize_matches_pocketbase_inflector() -> None:
    assert sentenize("  not Found ") == "Not Found."
    assert sentenize("query string is too large") == "Query string is too large."
    assert sentenize("Done!") == "Done!"
    assert sentenize("Only admins can filter by @collection.") == (
        "Only admins can filter by @collection."
    )
    assert sentenize("   ") == ""


@pytest.mark.parametrize(
    ("status", "message", "method", "path", "expected"),
    [
        (
            403,
            "Only superusers can perform this action.",
            "GET",
            "/api/collections/x/records",
            "Only admins can perform this action.",
        ),
        (
            404,
            "Missing collection context.",
            "GET",
            "/api/collections/x/records",
            "The requested resource wasn't found.",
        ),
        (
            401,
            "The request requires valid auth token to be set.",
            "POST",
            "/api/collections/users/auth-refresh",
            "The request requires valid record authorization token to be set.",
        ),
        (
            401,
            "The request requires admin authorization token to be set.",
            "GET",
            "/api/collections",
            "The request requires valid admin authorization token to be set.",
        ),
        (
            400,
            "Invalid JSON body.",
            "POST",
            "/api/collections/x/records",
            "Failed to load the submitted data due to invalid formatting.",
        ),
        (
            400,
            "Request body must be a JSON object.",
            "PATCH",
            "/api/collections/x/records/abc",
            "Failed to load the submitted data due to invalid formatting.",
        ),
        (
            400,
            "Invalid JSON body.",
            "POST",
            "/api/collections/users/auth-with-password",
            "An error occurred while loading the submitted data.",
        ),
        (
            400,
            "Something went wrong while processing your request.",
            "POST",
            "/api/collections/users/request-verification",
            "An error occurred while validating the form.",
        ),
        (
            400,
            "Failed to confirm password reset.",
            "POST",
            "/api/collections/users/confirm-password-reset",
            "Something went wrong while processing your request.",
        ),
        (
            400,
            "Failed to create record. Validation failed for: title, n.",
            "POST",
            "/api/collections/x/records",
            "Failed to create record.",
        ),
        # 0.22.34 returns an update form validation error unwrapped.
        (
            400,
            "Failed to update record. Validation failed for: title.",
            "PATCH",
            "/api/collections/x/records/abc",
            "Something went wrong while processing your request.",
        ),
        # A persistence error keeps its 0.22.34 wording.
        (
            400,
            "Failed to update record.",
            "PATCH",
            "/api/collections/x/records/abc",
            "Failed to update record.",
        ),
        (
            404,
            "Collection 'posts' not found.",
            "GET",
            "/api/collections/posts",
            "The requested resource wasn't found.",
        ),
        (
            404,
            "Log not found.",
            "GET",
            "/api/logs/abc",
            "The requested resource wasn't found.",
        ),
        (
            400,
            "Invalid settings payload.",
            "PATCH",
            "/api/settings",
            "An error occurred while loading the submitted data.",
        ),
        # Same text, other route: unchanged.
        (
            400,
            "Invalid JSON body.",
            "POST",
            "/api/hooks/settings",
            "Invalid JSON body.",
        ),
        (
            404,
            "Not Found",
            "GET",
            "/api/nothing",
            "Not Found.",
        ),
    ],
)
def test_message_translation(
    status: int, message: str, method: str, path: str, expected: str
) -> None:
    body = {"status": status, "message": message, "data": {}}
    rewritten = pocketbase_022_error(status, body, method=method, path=path)
    assert rewritten == {"code": status, "message": expected, "data": {}}
    assert list(rewritten) == ["code", "message", "data"]


def test_list_parse_errors_and_bad_credentials() -> None:
    parse_error = {
        "status": 400,
        "message": "Invalid filter syntax: Unexpected end.",
        "data": {},
    }
    list_path = "/api/collections/posts/records"
    rewritten = pocketbase_022_error(
        400, parse_error, method="GET", path=list_path, kind="query"
    )
    assert rewritten == {
        "code": 400,
        "message": "Something went wrong while processing your request.",
        "data": {},
    }
    # Untagged (for example a host list-request hook's own 400): kept.
    untagged = pocketbase_022_error(400, parse_error, method="GET", path=list_path)
    assert untagged["message"] == "Invalid filter syntax: Unexpected end."
    hook_error = {"status": 400, "message": "Période invalide", "data": {}}
    assert (
        pocketbase_022_error(400, hook_error, method="GET", path=list_path)["message"]
        == "Période invalide."
    )
    credentials = {
        "status": 400,
        "message": "Failed to authenticate.",
        "data": {
            "identity": {
                "code": "validation_invalid_credentials",
                "message": "Invalid login credentials.",
            }
        },
    }
    for path in (
        "/api/collections/users/auth-with-password",
        "/api/admins/auth-with-password",
    ):
        assert pocketbase_022_error(400, credentials, method="POST", path=path) == {
            "code": 400,
            "message": "Failed to authenticate.",
            "data": {},
        }
    other = {**credentials, "data": {"identity": {"code": "validation_invalid"}}}
    rewritten = pocketbase_022_error(
        400, other, method="POST", path="/api/collections/users/auth-with-password"
    )
    assert rewritten["data"] == other["data"]


def test_password_login_form_errors_are_unwrapped() -> None:
    blank = {"code": "validation_required", "message": "Cannot be blank."}
    path = "/api/collections/users/auth-with-password"
    for data, expected in (
        ({"identity": {}, "password": blank}, {"password": blank}),
        (
            {"identity": blank, "password": blank},
            {"identity": blank, "password": blank},
        ),
    ):
        body = {"status": 400, "message": "Failed to authenticate.", "data": data}
        assert pocketbase_022_error(400, body, method="POST", path=path) == {
            "code": 400,
            "message": "Something went wrong while processing your request.",
            "data": expected,
        }
    # No failing field: not a form error.
    body = {"status": 400, "message": "Failed to authenticate.", "data": {"a": {}}}
    rewritten = pocketbase_022_error(400, body, method="POST", path=path)
    assert rewritten["message"] == "Failed to authenticate."


def test_tagged_errors_take_the_0_22_message_of_their_kind() -> None:
    body = {
        "status": 400,
        "message": "Failed to create record. Validation failed for: title.",
        "data": {
            "title": {"code": "validation_required", "message": "Cannot be blank."},
            "identity": {"code": "validation_required", "message": "Field required"},
        },
    }
    rewritten = pocketbase_022_error(
        400, body, method="POST", path="/api/collections/x/records", kind="form"
    )
    assert rewritten == {
        "code": 400,
        "message": "Something went wrong while processing your request.",
        "data": {
            "title": {"code": "validation_required", "message": "Cannot be blank."},
            "identity": {"code": "validation_required", "message": "Cannot be blank."},
        },
    }
    guards = {
        "guest_only": (403, "The request can be accessed only by guests."),
        "record_auth_required": (
            401,
            "The request requires valid record authorization token to be set.",
        ),
        "record_collection_forbidden": (
            403,
            "The authorized record model is not allowed to perform this action.",
        ),
        "admin_auth_required": (
            401,
            "The request requires valid admin authorization token to be set.",
        ),
        "admin_or_record_auth_required": (
            401,
            "The request requires admin or record authorization token to be set.",
        ),
        "owner_forbidden": (403, "You are not allowed to perform this request."),
    }
    for kind, (status, message) in guards.items():
        guard_body = {"status": status, "message": "x", "data": {}}
        assert pocketbase_022_error(status, guard_body, kind=kind) == {
            "code": status,
            "message": message,
            "data": {},
        }
    # An unknown tag is ignored (the table applies).
    plain = {"status": 403, "message": "Only superusers can perform this action."}
    assert pocketbase_022_error(403, plain, kind="nope")["message"] == (
        "Only admins can perform this action."
    )


def test_marking_is_a_no_op_while_the_mode_is_off() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    request = SimpleNamespace(app=app)
    response = JSONResponse({"status": 400, "message": "x", "data": {}}, 400)
    mark_error(request, response, "form")
    assert ERROR_KIND_HEADER not in response.headers
    assert error_kind_headers(app, "form") is None
    set_error_compat(app, "0.22")
    mark_error(request, response, "form")
    assert response.headers[ERROR_KIND_HEADER] == "form"
    assert error_kind_headers(app, "query") == {ERROR_KIND_HEADER: "query"}
    with pytest.raises(ValueError):
        mark_error(request, response, "nope")


def _run_middleware(mode: str, status: int, headers: dict[str, str]) -> tuple:
    app = SimpleNamespace(state=SimpleNamespace(error_compat=mode))

    async def inner(scope, receive, send) -> None:
        response = JSONResponse(
            {"status": status, "message": "Failed to update record.", "data": {}},
            status_code=status,
            headers=headers,
        )
        await response(scope, receive, send)

    sent: list[dict] = []

    async def send(message) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "PATCH",
        "path": "/api/collections/x/records/abc",
        "headers": [],
        "app": app,
    }
    asyncio.run(ErrorCompatMiddleware(inner)(scope, receive, send))
    start_headers = dict(sent[0]["headers"])
    return start_headers, json.loads(sent[1]["body"])


def test_middleware_consumes_the_kind_header() -> None:
    headers, body = _run_middleware("0.22", 400, {ERROR_KIND_HEADER: "form"})
    assert ERROR_KIND_HEADER.encode() not in headers
    # The bytes of PocketBase 0.22.34 (echo's encoder adds a final newline).
    assert int(headers[b"content-length"]) == len(encode_like_pocketbase_022(body))
    assert body == {
        "code": 400,
        "message": "Something went wrong while processing your request.",
        "data": {},
    }
    # A tag on a success is removed too.
    headers, body = _run_middleware("0.22", 200, {ERROR_KIND_HEADER: "form"})
    assert ERROR_KIND_HEADER.encode() not in headers
    assert body["status"] == 200
    # Untagged update persistence error: kept.
    _headers, body = _run_middleware("0.22", 400, {})
    assert body["message"] == "Failed to update record."
    # Mode off: untouched.
    _headers, body = _run_middleware("", 400, {})
    assert body == {"status": 400, "message": "Failed to update record.", "data": {}}


def test_starlette_detail_bodies_are_translated() -> None:
    assert pocketbase_022_error(
        405, {"detail": "Method Not Allowed"}, method="DELETE", path="/api/health"
    ) == {"code": 405, "message": "Method Not Allowed.", "data": {}}
    assert pocketbase_022_error(422, {"detail": [{"loc": ["x"]}]}) is None


def test_realtime_missing_client_id_gets_the_form_error_data() -> None:
    rewritten = pocketbase_022_error(
        400,
        {"status": 400, "message": "Missing clientId.", "data": {}},
        method="POST",
        path="/api/realtime",
    )
    assert rewritten == {
        "code": 400,
        "message": "Something went wrong while processing your request.",
        "data": {
            "clientId": {"code": "validation_required", "message": "Cannot be blank."}
        },
    }


@pytest.mark.parametrize(
    "body",
    [
        {"code": 401, "message": "Already 0.22.", "data": {}},
        {"message": "Montant non valide"},
        {"status": 400, "message": "x", "data": {}, "extra": 1},
        {"status": "400", "message": "x", "data": {}},
        {"status": True, "message": "x"},
        ["status", "message"],
        None,
    ],
)
def test_other_bodies_are_left_untouched(body: object) -> None:
    assert pocketbase_022_error(400, body, method="GET", path="/api/x") is None


def test_every_rule_cites_its_pocketbase_source() -> None:
    for rule in message_rules():
        assert rule.source and "l." in rule.source, rule
        assert rule.replacement, rule
    for kind, spec in error_kinds().items():
        assert spec.source and "l." in spec.source, kind


@pytest.mark.parametrize(
    ("method", "path", "message"),
    [
        # apis/admin.go l.247-249 (create), l.293-295 (update)
        ("POST", "/api/admins", INVALID_FORMATTING),
        ("PATCH", "/api/admins/aaaaaaaaaaaaaaa", INVALID_FORMATTING),
        # apis/admin.go l.83-85, apis/backup.go l.76-77, PPBase-only routes
        ("POST", "/api/admins/auth-with-password", LOAD_ERROR),
        ("POST", "/api/admins/aaaaaaaaaaaaaaa/change-password", LOAD_ERROR),
        ("POST", "/api/backups", LOAD_ERROR),
    ],
)
def test_an_unbound_body_takes_the_message_of_its_route(
    method: str, path: str, message: str
) -> None:
    body = {
        "status": 400,
        "message": "Something went wrong while processing your request.",
        "data": {"1": {"code": "validation_required", "message": "JSON decode error"}},
    }
    assert pocketbase_022_error(
        400, body, method=method, path=path, kind="body_load"
    ) == {"code": 400, "message": message, "data": {}}


def test_settings_and_backup_load_errors_take_the_022_message() -> None:
    test_email = {
        "status": 400,
        "message": "Failed to send the test email.",
        "data": {
            "body": {
                "code": "validation_invalid_value",
                "message": "Invalid request body.",
            }
        },
    }
    assert pocketbase_022_error(
        400, test_email, method="POST", path="/api/settings/test/email"
    ) == {"code": 400, "message": LOAD_ERROR, "data": {}}
    # A form error of the same route keeps its message (apis/settings.go l.121).
    form_error = {
        **test_email,
        "data": {
            "email": {"code": "validation_required", "message": "Cannot be blank."}
        },
    }
    assert (
        pocketbase_022_error(
            400, form_error, method="POST", path="/api/settings/test/email"
        )["message"]
        == "Failed to send the test email."
    )
    backup = {"status": 400, "message": "Invalid UTF-8 JSON body.", "data": {}}
    assert pocketbase_022_error(400, backup, method="POST", path="/api/backups") == {
        "code": 400,
        "message": LOAD_ERROR,
        "data": {},
    }


def test_022_error_bytes_are_those_of_pocketbase_022() -> None:
    # Measured on the 0.22.34 binary (echo's json Encoder, apis/base.go l.104):
    # GET /api/files/nope/nope/nope.png and a pb_hooks route throwing
    # BadRequestError("a<b>c&d\u2028e\u00e9").
    assert (
        encode_like_pocketbase_022(
            {"code": 404, "message": "The requested resource wasn't found.", "data": {}}
        )
        == b'{"code":404,"message":"The requested resource wasn\'t found.","data":{}}\n'
    )
    assert encode_like_pocketbase_022(
        {"code": 400, "message": "A<b>c&d\u2028e\u00e9.", "data": {}}
    ) == (
        '{"code":400,"message":"A\\u003cb\\u003ec\\u0026d\\u2028e\u00e9.","data":{}}\n'
    ).encode("utf-8")
    # data is a Go map: its keys are sorted, the struct fields are not.
    assert encode_like_pocketbase_022(
        {
            "code": 400,
            "message": "x",
            "data": {
                "password": {
                    "message": "Cannot be blank.",
                    "code": "validation_required",
                },
                "identity": {
                    "message": "Cannot be blank.",
                    "code": "validation_required",
                },
            },
        }
    ) == (
        b'{"code":400,"message":"x","data":{"identity":{"code":"validation_required",'
        b'"message":"Cannot be blank."},"password":{"code":"validation_required",'
        b'"message":"Cannot be blank."}}}\n'
    )
