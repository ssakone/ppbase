"""Binding of the PocketBase 0.22 forms (ppbase/api/legacy_forms.py).

Each expected outcome was measured on the official PocketBase 0.22.34 binary
with the same request sent to ``POST /api/admins/request-password-reset``
(synthetic addresses only, 2026-10-24):

- ``ok``: 204, the bound e-mail is the valid one;
- ``required``, ``is_email``: 400 ``An error occurred while validating the
  form.`` with that error on ``email``;
- ``loading``: 400 ``An error occurred while loading the submitted data.``
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from starlette.requests import Request

from ppbase.api.legacy_forms import FormLoadError, bind_legacy_form, go_parse_media_type
from ppbase.services.legacy_validation import byte_length, is_email_format

GOOD = "nobody@c11.test"
FORM = "application/x-www-form-urlencoded"
JSON = "application/json"


def _request(
    body: bytes,
    content_type: str | None,
    query: bytes = b"",
    *,
    chunked: bool = False,
) -> Request:
    chunks = [body]

    async def receive() -> dict:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(), "more_body": False}
        return {"type": "http.disconnect"}

    headers = []
    if content_type is not None:
        headers.append((b"content-type", content_type.encode("latin-1")))
    if chunked:
        headers.append((b"transfer-encoding", b"chunked"))
    else:
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers,
        "query_string": query,
    }
    return Request(scope, receive)


def _built(**kwargs) -> tuple[bytes, str]:
    """Body and type built by httpx, like the PocketBase JS SDK sends them."""
    built = httpx.Request("POST", "http://c11.test/", **kwargs)
    return built.read(), built.headers["content-type"]


def _multipart(*parts: tuple[bytes, bytes], tail: bytes = b"--b--\r\n") -> bytes:
    body = b""
    for disposition, value in parts:
        body += b"--b\r\nContent-Disposition: " + disposition + b"\r\n\r\n" + value + b"\r\n"
    return body + tail


async def _outcome(request: Request) -> str:
    """The admin reset request answer: Required, Length(1, 255), EmailFormat."""
    try:
        email = (await bind_legacy_form(request, ("email",)))["email"]
    except FormLoadError:
        return "loading"
    if not email:
        return "required"
    if byte_length(email) > 255:
        return "length"
    return "ok" if is_email_format(email) else "is_email"


def _case(spec: dict) -> Request:
    query = spec.get("query", b"")
    if "json" in spec:
        body, ctype = _built(json=spec["json"])
    elif "files" in spec:
        body, ctype = _built(files=spec["files"])
    else:
        body, ctype = spec["raw"], spec["ctype"]
    return _request(body, ctype, query, chunked=spec.get("chunked", False))


MP = "multipart/form-data; boundary=b"
DEEP = '{"x": ' + "[" * 1500 + "]" * 1500 + ', "email": "nobody@c11.test"}'

CASES: dict[str, tuple[dict, str]] = {
    # Content types.
    "form_basic": ({"raw": b"email=nobody%40c11.test", "ctype": FORM}, "ok"),
    "form_charset": ({"raw": b"email=nobody%40c11.test", "ctype": FORM + "; charset=utf-8"}, "ok"),
    "multipart_basic": ({"files": {"email": (None, GOOD)}}, "ok"),
    "no_content_type_json": ({"raw": b'{"email": "nobody@c11.test"}', "ctype": None}, "loading"),
    "text_plain_json": ({"raw": b'{"email": "nobody@c11.test"}', "ctype": "text/plain"}, "loading"),
    "upper_application_json": (
        {"raw": b'{"email": "nobody@c11.test"}', "ctype": "Application/JSON"},
        "loading",
    ),
    "json_charset": (
        {"raw": b'{"email": "nobody@c11.test"}', "ctype": JSON + "; charset=utf-8"},
        "ok",
    ),
    "json_suffix_ctype": (
        {"raw": b'{"email": "nobody@c11.test"}', "ctype": "application/jsonx"},
        "ok",
    ),
    "xml_body": (
        {
            "raw": b"<AdminPasswordResetRequest><Email>nobody@c11.test</Email></AdminPasswordResetRequest>",
            "ctype": "application/xml",
        },
        "loading",
    ),
    "text_xml": ({"raw": b"<x><Email>nobody@c11.test</Email></x>", "ctype": "text/xml"}, "loading"),
    # Empty bodies: Content-Length 0 binds nothing, even the query string.
    "form_empty_body_cl0": ({"raw": b"", "ctype": FORM}, "required"),
    "form_cl0_query_good": (
        {"raw": b"", "ctype": FORM, "query": b"email=nobody%40c11.test"},
        "required",
    ),
    "json_cl0_query_good": (
        {"raw": b"", "ctype": JSON, "query": b"email=nobody%40c11.test"},
        "required",
    ),
    "cl0_text_plain": ({"raw": b"", "ctype": "text/plain"}, "required"),
    "chunked_empty_json": ({"raw": b"", "ctype": JSON, "chunked": True}, "loading"),
    "chunked_json": (
        {"raw": b'{"email": "nobody@c11.test"}', "ctype": JSON, "chunked": True},
        "ok",
    ),
    # Keys: exact, else Unicode simple folding; the last JSON key wins.
    "form_upper_key": ({"raw": b"EMAIL=nobody%40c11.test", "ctype": FORM}, "ok"),
    "form_title_key": ({"raw": b"Email=nobody%40c11.test", "ctype": FORM}, "ok"),
    "form_exact_and_folded": ({"raw": b"EMAIL=bad&email=nobody%40c11.test", "ctype": FORM}, "ok"),
    "form_dotless_i_key": ({"raw": b"ema%C4%B1l=nobody%40c11.test", "ctype": FORM}, "required"),
    "json_upper_key": ({"json": {"EMAIL": GOOD}}, "ok"),
    "json_mixed_key": ({"json": {"eMaIl": GOOD}}, "ok"),
    "json_escaped_key": ({"raw": b'{"\\u0065mail": "nobody@c11.test"}', "ctype": JSON}, "ok"),
    "json_dotless_i_key": (
        {"raw": '{"emaıl": "nobody@c11.test"}'.encode(), "ctype": JSON},
        "required",
    ),
    "json_dotted_capital_i_key": (
        {"raw": '{"emaİl": "nobody@c11.test"}'.encode(), "ctype": JSON},
        "required",
    ),
    "json_kelvin_key_email_absent": (
        {"raw": '{"Key": 1, "email": "nobody@c11.test"}'.encode(), "ctype": JSON},
        "ok",
    ),
    "json_exact_then_folded": (
        {"raw": b'{"email": "nobody@c11.test", "EMAIL": "bad"}', "ctype": JSON},
        "is_email",
    ),
    "json_folded_then_exact": (
        {"raw": b'{"EMAIL": "bad", "email": "nobody@c11.test"}', "ctype": JSON},
        "ok",
    ),
    "json_dup_interleaved": (
        {"raw": b'{"email": "bad", "EMAIL": "bad2", "email": "nobody@c11.test"}', "ctype": JSON},
        "ok",
    ),
    "json_null_after_value": (
        {"raw": b'{"email": "nobody@c11.test", "EMAIL": null}', "ctype": JSON},
        "ok",
    ),
    "json_value_after_null": (
        {"raw": b'{"email": null, "EMAIL": "nobody@c11.test"}', "ctype": JSON},
        "ok",
    ),
    # JSON documents.
    "json_null": ({"raw": b"null", "ctype": JSON}, "required"),
    "json_null_trailing_space_garbage": ({"raw": b"null x", "ctype": JSON}, "required"),
    "json_null_glued_garbage": ({"raw": b"nullx", "ctype": JSON}, "required"),
    "json_whitespace": ({"raw": b"   ", "ctype": JSON}, "loading"),
    "json_trailing_garbage": (
        {"raw": b'{"email": "nobody@c11.test"} trailing', "ctype": JSON},
        "ok",
    ),
    "json_two_objects": (
        {"raw": b'{"email": "nobody@c11.test"}{"email": "bad"}', "ctype": JSON},
        "ok",
    ),
    "json_truncated": ({"raw": b'{"email": "nobody@c11.test"', "ctype": JSON}, "loading"),
    "json_nan": ({"raw": b'{"x": NaN, "email": "nobody@c11.test"}', "ctype": JSON}, "loading"),
    "json_array": ({"raw": b"[]", "ctype": JSON}, "loading"),
    "json_string": ({"raw": b'"x"', "ctype": JSON}, "loading"),
    "json_top_true": ({"raw": b"true", "ctype": JSON}, "loading"),
    "json_number_field": ({"raw": b'{"email": 5}', "ctype": JSON}, "loading"),
    "json_bool_field": ({"raw": b'{"email": true}', "ctype": JSON}, "loading"),
    "json_object_field": ({"raw": b'{"email": {"a": 1}}', "ctype": JSON}, "loading"),
    "json_type_error_other_field_ok": (
        {"raw": b'{"email": 5, "EMAIL": "nobody@c11.test"}', "ctype": JSON},
        "loading",
    ),
    "json_type_error_then_string": (
        {"raw": b'{"email": 5, "email": "nobody@c11.test"}', "ctype": JSON},
        "loading",
    ),
    "json_bom": ({"raw": b'\xef\xbb\xbf{"email": "nobody@c11.test"}', "ctype": JSON}, "loading"),
    "json_utf16": (
        {"raw": '{"email": "nobody@c11.test"}'.encode("utf-16"), "ctype": JSON},
        "loading",
    ),
    "json_invalid_utf8_in_string": (
        {"raw": b'{"email": "nob\xffody@c11.test"}', "ctype": JSON},
        "is_email",
    ),
    "json_big_number_unknown_key": (
        {"raw": ('{"x": ' + "9" * 5000 + ', "email": "nobody@c11.test"}').encode(), "ctype": JSON},
        "ok",
    ),
    "json_deep_unknown_key_1500": ({"raw": DEEP.encode(), "ctype": JSON}, "ok"),
    "json_lone_surrogate_unknown_key": (
        {"raw": b'{"x": "\\udc00", "email": "nobody@c11.test"}', "ctype": JSON},
        "ok",
    ),
    # Query strings: merged with form bodies only.
    "form_query_only": ({"raw": b"x=1", "ctype": FORM, "query": b"email=nobody%40c11.test"}, "ok"),
    "form_body_bad_query_good": (
        {"raw": b"email=bad", "ctype": FORM, "query": b"email=nobody%40c11.test"},
        "is_email",
    ),
    "multipart_body_bad_query_good": (
        {"files": {"email": (None, "bad")}, "query": b"email=nobody%40c11.test"},
        "ok",
    ),
    "multipart_query_only": (
        {"files": {"x": (None, "1")}, "query": b"email=nobody%40c11.test"},
        "ok",
    ),
    "json_query_good": ({"json": {}, "query": b"email=nobody%40c11.test"}, "required"),
    "form_query_bad_escape": (
        {"raw": b"email=nobody%40c11.test", "ctype": FORM, "query": b"x=%zz"},
        "ok",
    ),
    "json_query_bad_escape": ({"json": {"email": GOOD}, "query": b"x=%zz"}, "ok"),
    # urlencoded pairs.
    "form_bad_escape": ({"raw": b"email=%zz", "ctype": FORM}, "required"),
    "form_bad_escape_other_key": ({"raw": b"x=%zz&email=nobody%40c11.test", "ctype": FORM}, "ok"),
    "form_semicolon": ({"raw": b"email=nobody%40c11.test;x=1", "ctype": FORM}, "required"),
    "form_semicolon_other_pair": ({"raw": b"x=1;y=2&email=nobody%40c11.test", "ctype": FORM}, "ok"),
    "form_duplicate_first_bad": (
        {"raw": b"email=bad&email=nobody%40c11.test", "ctype": FORM},
        "is_email",
    ),
    "form_duplicate_first_good": (
        {"raw": b"email=nobody%40c11.test&email=bad", "ctype": FORM},
        "ok",
    ),
    "form_plus_is_space": ({"raw": b"email=nobody+x%40c11.test", "ctype": FORM}, "is_email"),
    "form_key_without_value": ({"raw": b"email", "ctype": FORM}, "required"),
    "form_invalid_utf8": ({"raw": b"email=nob%FFody%40c11.test", "ctype": FORM}, "is_email"),
    "form_empty_key_pair": ({"raw": b"=x&email=nobody%40c11.test", "ctype": FORM}, "ok"),
    # @jsonPayload, then the form fields.
    "multipart_jsonpayload": (
        {"files": {"@jsonPayload": (None, json.dumps({"email": GOOD}))}},
        "ok",
    ),
    "multipart_jsonpayload_and_bad_field": (
        {"files": {"@jsonPayload": (None, json.dumps({"email": GOOD})), "email": (None, "bad")}},
        "is_email",
    ),
    "multipart_jsonpayload_type_error": (
        {"files": {"@jsonPayload": (None, '{"email": 5}')}},
        "required",
    ),
    "multipart_jsonpayload_invalid": ({"files": {"@jsonPayload": (None, "{oops")}}, "required"),
    "form_jsonpayload": (
        {"raw": b"%40jsonPayload=%7B%22email%22%3A%22nobody%40c11.test%22%7D", "ctype": FORM},
        "ok",
    ),
    "jsonpayload_trailing_garbage": (
        {"files": {"@jsonPayload": (None, '{"email": "nobody@c11.test"} x')}},
        "required",
    ),
    "jsonpayload_array": ({"files": {"@jsonPayload": (None, "[]")}}, "required"),
    "jsonpayload_folded_key": (
        {"files": {"@jsonPayload": (None, '{"EMAIL": "nobody@c11.test"}')}},
        "ok",
    ),
    "jsonpayload_null_email": ({"files": {"@jsonPayload": (None, '{"email": null}')}}, "required"),
    "jsonpayload_partial_type_error": (
        {"files": {"@jsonPayload": (None, '{"email": 5, "EMAIL": "nobody@c11.test"}')}},
        "ok",
    ),
    "jsonpayload_two_payloads_last_bad": (
        {
            "raw": _multipart(
                (b'form-data; name="@jsonPayload"', b'{"email": "nobody@c11.test"}'),
                (b'form-data; name="@jsonPayload"', b'{"email": "bad"}'),
            ),
            "ctype": MP,
        },
        "is_email",
    ),
    # Multipart parts.
    "multipart_file_named_email": (
        {"files": {"email": ("e.txt", GOOD.encode(), "text/plain")}},
        "required",
    ),
    "multipart_empty_filename": (
        {"raw": _multipart((b'form-data; name="email"; filename=""', GOOD.encode())), "ctype": MP},
        "ok",
    ),
    "multipart_part_without_name": (
        {
            "raw": _multipart((b"form-data", b"x"), (b'form-data; name="email"', GOOD.encode())),
            "ctype": MP,
        },
        "ok",
    ),
    "multipart_invalid_utf8_value": (
        {"raw": _multipart((b'form-data; name="email"', b"nob\xffody@c11.test")), "ctype": MP},
        "is_email",
    ),
    "multipart_no_boundary": ({"raw": b"--x\r\n", "ctype": "multipart/form-data"}, "loading"),
    "multipart_truncated_in_headers": (
        {"raw": b"--b\r\nContent-Disposition: form-data; name", "ctype": MP},
        "required",
    ),
    "multipart_complete_no_closing": (
        {"raw": _multipart((b'form-data; name="email"', GOOD.encode()), tail=b""), "ctype": MP},
        "loading",
    ),
    "multipart_dash_boundary_only": (
        {"raw": _multipart((b'form-data; name="email"', GOOD.encode()), tail=b"--b"), "ctype": MP},
        "loading",
    ),
    "multipart_closing_without_crlf": (
        {
            "raw": _multipart((b'form-data; name="email"', GOOD.encode()), tail=b"--b--"),
            "ctype": MP,
        },
        "ok",
    ),
    "multipart_truncated_in_value": (
        {
            "raw": b'--b\r\nContent-Disposition: form-data; name="email"\r\n\r\nnobody@c1',
            "ctype": MP,
        },
        "loading",
    ),
    "multipart_preamble": (
        {"raw": b"junk\r\n" + _multipart((b'form-data; name="email"', GOOD.encode())), "ctype": MP},
        "ok",
    ),
    "multipart_only_preamble": ({"raw": b"just text\r\n", "ctype": MP}, "loading"),
    "multipart_only_closing": ({"raw": b"--b--\r\n", "ctype": MP}, "required"),
    "multipart_attachment_disposition": (
        {"raw": _multipart((b'attachment; name="email"', GOOD.encode())), "ctype": MP},
        "required",
    ),
    "multipart_mixed_case_disposition": (
        {"raw": _multipart((b'Form-Data; NAME="email"', GOOD.encode())), "ctype": MP},
        "ok",
    ),
    "multipart_no_disposition": (
        {
            "raw": b"--b\r\nContent-Type: text/plain\r\n\r\n" + GOOD.encode() + b"\r\n--b--\r\n",
            "ctype": MP,
        },
        "required",
    ),
    "multipart_two_email_parts_first_bad": (
        {
            "raw": _multipart(
                (b'form-data; name="email"', b"bad"), (b'form-data; name="email"', GOOD.encode())
            ),
            "ctype": MP,
        },
        "is_email",
    ),
    "multipart_empty_value_then_good": (
        {
            "raw": _multipart(
                (b'form-data; name="email"', b""), (b'form-data; name="email"', GOOD.encode())
            ),
            "ctype": MP,
        },
        "required",
    ),
    "multipart_unquoted_name": (
        {"raw": _multipart((b"form-data; name=email", GOOD.encode())), "ctype": MP},
        "ok",
    ),
    "multipart_filename_star": (
        {
            "raw": _multipart(
                (b"form-data; name=\"email\"; filename*=UTF-8''x.txt", GOOD.encode())
            ),
            "ctype": MP,
        },
        "required",
    ),
    "multipart_name_star_only": (
        {"raw": _multipart((b"form-data; name*=UTF-8''email", GOOD.encode())), "ctype": MP},
        "ok",
    ),
    "multipart_bad_disposition_syntax": (
        {"raw": _multipart((b'form-data; name="email', GOOD.encode())), "ctype": MP},
        "required",
    ),
    "multipart_value_with_crlf_inside": (
        {"raw": _multipart((b'form-data; name="email"', b"nobody@c11.test\r\nx")), "ctype": MP},
        "is_email",
    ),
    "multipart_jsonpayload_after_field": (
        {
            "raw": _multipart(
                (b'form-data; name="email"', b"bad"),
                (b'form-data; name="@jsonPayload"', b'{"email": "nobody@c11.test"}'),
            ),
            "ctype": MP,
        },
        "is_email",
    ),
    "multipart_quoted_boundary": (
        {
            "raw": _multipart((b'form-data; name="email"', GOOD.encode())),
            "ctype": 'multipart/form-data; boundary="b"',
        },
        "ok",
    ),
    "multipart_boundary_param_case": (
        {
            "raw": _multipart((b'form-data; name="email"', GOOD.encode())),
            "ctype": "multipart/form-data; BOUNDARY=b",
        },
        "ok",
    ),
    "multipart_ctype_upper": (
        {
            "raw": _multipart((b'form-data; name="email"', GOOD.encode())),
            "ctype": "Multipart/Form-Data; boundary=b",
        },
        "loading",
    ),
    "multipart_charset_param": (
        {
            "raw": _multipart((b'form-data; name="email"', GOOD.encode())),
            "ctype": MP + "; charset=latin-1",
        },
        "ok",
    ),
    "multipart_bad_media_params": (
        {"raw": _multipart((b'form-data; name="email"', GOOD.encode())), "ctype": MP + "; =x"},
        "loading",
    ),
}


@pytest.mark.parametrize(("spec", "expected"), CASES.values(), ids=CASES.keys())
def test_binding_matches_the_022_binary(spec: dict, expected: str) -> None:
    assert asyncio.run(_outcome(_case(spec))) == expected


def test_lf_only_multipart_is_a_documented_difference() -> None:
    """PocketBase 0.22.34 accepts LF-only multipart bodies (204, measured);
    python-multipart does not, so PPBase answers the loading error."""
    body = b'--b\nContent-Disposition: form-data; name="email"\n\n' + GOOD.encode() + b"\n--b--\n"
    assert asyncio.run(_outcome(_request(body, MP))) == "loading"


def test_confirm_fields_fold_like_go() -> None:
    names = ("token", "password", "passwordConfirm")
    body = json.dumps({"toKen": "t", "paſſword": "p", "PASSWORDCONFIRM": "c"}).encode()
    bound = asyncio.run(bind_legacy_form(_request(body, JSON), names))
    assert bound == {"token": "t", "password": "p", "passwordConfirm": "c"}
    form = "toKen=t&password=p&passwordconfirm=c".encode()
    assert asyncio.run(bind_legacy_form(_request(form, FORM), names)) == bound


def test_lone_surrogates_are_bound_as_replacement_characters() -> None:
    body = b'{"password": "' + b"\\ud800" * 3 + b'a"}'
    bound = asyncio.run(bind_legacy_form(_request(body, JSON), ("password",)))
    assert bound == {"password": "�" * 3 + "a"}
    assert byte_length(bound["password"]) == 10


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("multipart/form-data; boundary=b", ("multipart/form-data", {"boundary": "b"})),
        ('Form-Data; NAME="e\\"x"', ("form-data", {"name": 'e"x'})),
        ("form-data; name*=UTF-8''%C3%A9", ("form-data", {"name": "Ã©"})),
        ("form-data; name*0=a; name*1=b", ("form-data", {"name": "ab"})),
        ("form-data; name=a;", ("form-data", {"name": "a"})),
        ("form-data; name=a; name=b", ("", None)),
        ("form-data; name=", ("form-data", None)),
        ('form-data; name="a', ("form-data", None)),
        ("multipart/form-data; boundary=b; =x", ("multipart/form-data", None)),
        ("a b", ("", None)),
        ("", ("", None)),
    ],
)
def test_media_types_parse_like_go(header: str, expected: tuple) -> None:
    assert go_parse_media_type(header) == expected
