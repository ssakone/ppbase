"""Unit tests of the opt-in PocketBase 0.22 error compatibility layer."""

from __future__ import annotations

import pytest

from ppbase.config import Settings
from ppbase.ext.flask_like_pb import FlaskLikePB
from ppbase.middleware.error_compat import (
    message_rules,
    normalize_error_compat,
    pocketbase_022_error,
    sentenize,
)


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
    rewritten = pocketbase_022_error(
        400,
        {
            "status": 400,
            "message": "Invalid filter syntax: Unexpected end.",
            "data": {},
        },
        method="GET",
        path="/api/collections/posts/records",
    )
    assert rewritten["message"] == "Something went wrong while processing your request."
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
    other = {**credentials, "data": {"identity": {"code": "validation_required"}}}
    rewritten = pocketbase_022_error(
        400, other, method="POST", path="/api/collections/users/auth-with-password"
    )
    assert rewritten["data"] == other["data"]


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
