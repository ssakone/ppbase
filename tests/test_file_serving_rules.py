"""PocketBase file-serving rules (``ppbase.storage.serving``).

Expected values come from PocketBase v0.22.34 ``tools/filesystem/
filesystem.go`` (``Serve`` l.367-430) and ``apis/file.go`` (l.26-27,
l.162-193), which v0.40.6 applies identically for every case below except
the quoting of non-token names (``strconv.Quote``, v0.40.6 l.587).
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from ppbase.storage.serving import (
    CACHE_CONTROL,
    CONTENT_SECURITY_POLICY,
    InvalidFileAttributes,
    content_disposition,
    disposition_type,
    encode_attrs,
    force_attachment,
    go_parse_bool,
    go_quote,
    latin1_header_value,
    parse_attrs,
    response_content_type,
    thumb_names,
    thumb_size_allowed,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("t", True),
        ("T", True),
        ("TRUE", True),
        ("true", True),
        ("True", True),
        ("0", False),
        ("f", False),
        ("F", False),
        ("FALSE", False),
        ("false", False),
        ("False", False),
        ("tRuE", None),
        ("yes", None),
        ("on", None),
        (" 1", None),
        ("", None),
    ],
)
def test_go_parse_bool_matches_strconv(raw: str, expected: bool | None) -> None:
    assert go_parse_bool(raw) is expected


@pytest.mark.parametrize(
    ("raw", "forced"),
    [
        (None, False),
        ("", False),
        ("1", True),
        ("true", True),
        ("T", True),
        ("0", False),
        ("yes", False),
        ("on", False),
        ("custom-name.pdf", False),
    ],
)
def test_download_param_is_a_go_boolean(raw: str | None, forced: bool) -> None:
    assert force_attachment(raw) is forced


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("image/png", "inline"),
        ("image/jpeg", "inline"),
        ("image/webp", "inline"),
        ("video/mp4", "inline"),
        ("audio/mpeg", "inline"),
        ("application/pdf", "inline"),
        ("image/svg+xml", "attachment"),
        ("text/plain; charset=utf-8", "attachment"),
        ("text/html; charset=utf-8", "attachment"),
        ("application/octet-stream", "attachment"),
        ("", "attachment"),
    ],
)
def test_inline_only_for_the_safe_types(content_type: str, expected: str) -> None:
    assert disposition_type(content_type, forced=False) == expected
    assert disposition_type(content_type, forced=True) == "attachment"


@pytest.mark.parametrize(
    ("real", "name", "expected"),
    [
        ("text/plain; charset=utf-8", "logo_abc.svg", "image/svg+xml"),
        ("text/plain; charset=utf-8", "style_abc.css", "text/css"),
        ("image/svg+xml", "logo_abc.svg", "image/svg+xml"),
        # Go's filepath.Ext is case-sensitive.
        ("text/plain; charset=utf-8", "logo_abc.SVG", "text/plain; charset=utf-8"),
        # v0.40.6-only overrides are not applied.
        ("text/plain; charset=utf-8", "app_abc.js", "text/plain; charset=utf-8"),
        ("application/zip", "sheet_abc.xlsx", "application/zip"),
        ("image/png", "100x100_photo_abc.png", "image/png"),
    ],
)
def test_extension_overrides(real: str, name: str, expected: str) -> None:
    assert response_content_type(real, name) == expected


def test_content_disposition_of_generated_names_is_the_0_22_header() -> None:
    assert (
        content_disposition("inline", "photo_k3j2h1g0f9.png")
        == "inline; filename=photo_k3j2h1g0f9.png"
    )
    assert (
        content_disposition("attachment", "100x100t_photo_k3j2h1g0f9.png")
        == "attachment; filename=100x100t_photo_k3j2h1g0f9.png"
    )


def test_content_disposition_quotes_other_names_like_strconv_quote() -> None:
    assert (
        content_disposition("attachment", 'my "report".pdf')
        == 'attachment; filename="my \\"report\\".pdf"'
    )
    assert go_quote("a\\b") == '"a\\\\b"'
    # A control character can never split the header.
    assert go_quote("x\r\nSet-Cookie: a=b") == '"x\\r\\nSet-Cookie: a=b"'
    assert go_quote("x\x00\x7f") == '"x\\x00\\x7f"'
    assert go_quote("rapport été.pdf") == '"rapport été.pdf"'
    assert go_quote("a b") == '"a\\u00a0b"'
    # Starlette writes the UTF-8 bytes of the value, as Go does.
    wire = latin1_header_value(go_quote("été")).encode("latin-1")
    assert wire == '"été"'.encode()


def test_thumb_sizes_are_matched_exactly() -> None:
    assert thumb_size_allowed("100x100", None)
    assert thumb_size_allowed("100x100", [])
    assert not thumb_size_allowed("100X100", None)
    assert not thumb_size_allowed(" 100x100", None)
    # Exact membership only: an invalid configured size is "allowed" and then
    # fails in CreateThumb, which serves the original.
    assert thumb_size_allowed("100x100 ", ["100x100 "])
    assert not thumb_size_allowed("", ["100x100"])
    assert thumb_size_allowed("50x50", ["20x0", "50x50"])
    assert not thumb_size_allowed("50x50", ["20x0"])
    assert thumb_size_allowed("50x50f", "50x50f")
    assert not thumb_size_allowed("../50x50", ["50x50"])
    assert thumb_names("100x100", "a_b.png") == ("thumbs_a_b.png", "100x100_a_b.png")


def _pocketbase_attrs(content_type: str, **extra: object) -> bytes:
    payload = {
        "user.cache_control": "",
        "user.content_disposition": "",
        "user.content_encoding": "",
        "user.content_language": "",
        "user.content_type": content_type,
        "user.metadata": {"original-filename": "photo.png"},
        "md5": "kAFQmDzST7DWlj99KOF/cg==",
    }
    payload.update(extra)
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def test_parse_attrs_reads_the_fileblob_side_car() -> None:
    attrs = parse_attrs(_pocketbase_attrs("image/png"))
    assert attrs.content_type == "image/png"
    assert attrs.metadata == {"original-filename": "photo.png"}
    # json.Decoder reads the first value only, after leading whitespace.
    assert parse_attrs(
        b'  {"user.content_type":"image/gif"} trailing'
    ).content_type == ("image/gif")
    assert parse_attrs(b"null").content_type == ""
    assert parse_attrs(b'{"unknown": 1}').content_type == ""


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"   ",
        b"{",
        b"[]",
        b'"image/png"',
        b'{"user.content_type": 12}',
        b'{"user.metadata": {"a": 1}}',
        b'{"user.metadata": []}',
        b'{"user.cache_control": false}',
        b'{"md5": 5}',
        b"\xff\xfe",
    ],
)
def test_parse_attrs_rejects_what_go_cannot_decode(payload: bytes) -> None:
    with pytest.raises(InvalidFileAttributes):
        parse_attrs(payload)


def test_encode_attrs_writes_the_fileblob_layout() -> None:
    content = b"thumbnail bytes"
    encoded = encode_attrs(content, "image/png")
    md5 = base64.b64encode(hashlib.md5(content).digest()).decode()
    assert (
        encoded
        == (
            '{"user.cache_control":"","user.content_disposition":"",'
            '"user.content_encoding":"","user.content_language":"",'
            '"user.content_type":"image/png","user.metadata":null,'
            f'"md5":"{md5}"}}\n'
        ).encode()
    )
    assert parse_attrs(encoded).content_type == "image/png"
    with_metadata = encode_attrs(content, "text/html", {"original-filename": "<a&b>"})
    assert b'"original-filename":"\\u003ca\\u0026b\\u003e"' in with_metadata


def test_constant_headers() -> None:
    assert CONTENT_SECURITY_POLICY == (
        "default-src 'none'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
    )
    assert CACHE_CONTROL == "max-age=2592000, stale-while-revalidate=86400"
