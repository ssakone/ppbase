"""PocketBase file-serving rules (``ppbase.storage.serving``).

Expected values come from PocketBase v0.22.34 ``tools/filesystem/
filesystem.go`` (``Serve`` l.367-430) and ``apis/file.go`` (l.26-27,
l.162-193) and from v0.40.6 (``filesystem.go`` l.521-599, l.700-721,
``apis/file.go`` l.22-23, ``internal/fileblob/attrs.go``).  Rules both
versions share are checked once; where they differ the default follows
v0.40.6 and ``compat="0.22"`` follows v0.22.34.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from ppbase.storage.serving import (
    CACHE_CONTROL,
    CONTENT_SECURITY_POLICY,
    POCKETBASE_022,
    InvalidFileAttributes,
    content_disposition,
    disposition_type,
    encode_attrs,
    file_security_headers,
    file_token_to_verify,
    force_attachment,
    go_parse_bool,
    go_quote,
    go_trim_space,
    latin1_header_value,
    parse_attrs,
    pocketbase_compat,
    response_content_type,
    thumb_image_content_types,
    thumb_names,
    thumb_output,
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
    ("real", "name", "default", "compat_022"),
    [
        ("text/plain; charset=utf-8", "logo_abc.svg", "image/svg+xml", "image/svg+xml"),
        ("text/plain; charset=utf-8", "style_abc.css", "text/css", "text/css"),
        ("image/svg+xml", "logo_abc.svg", "image/svg+xml", "image/svg+xml"),
        # Go's filepath.Ext is case-sensitive.
        (
            "text/plain; charset=utf-8",
            "logo_abc.SVG",
            "text/plain; charset=utf-8",
            "text/plain; charset=utf-8",
        ),
        # v0.40.6 adds .js/.mjs and the Office types (filesystem.go l.534-549).
        (
            "text/plain; charset=utf-8",
            "app_abc.js",
            "text/javascript",
            "text/plain; charset=utf-8",
        ),
        ("text/plain", "mod_abc.mjs", "text/javascript", "text/plain"),
        (
            "application/zip",
            "sheet_abc.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/zip",
        ),
        (
            "application/zip",
            "doc_abc.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/zip",
        ),
        (
            "application/zip",
            "deck_abc.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/zip",
        ),
        # BUMA's spreadsheets are .xls: no override in either version.
        ("application/vnd.ms-excel", "a_abc.xls", "application/vnd.ms-excel", None),
        ("image/png", "100x100_photo_abc.png", "image/png", "image/png"),
    ],
)
def test_extension_overrides(
    real: str, name: str, default: str, compat_022: str | None
) -> None:
    assert response_content_type(real, name) == default
    assert response_content_type(real, name, compat=POCKETBASE_022) == (
        compat_022 or default
    )


def test_content_disposition_quotes_every_name_by_default() -> None:
    """v0.40.6 ``filesystem.go`` l.587: ``strconv.Quote`` for every name."""
    assert (
        content_disposition("inline", "photo_k3j2h1g0f9.png")
        == 'inline; filename="photo_k3j2h1g0f9.png"'
    )
    assert (
        content_disposition("attachment", "100x100t_photo_k3j2h1g0f9.png")
        == 'attachment; filename="100x100t_photo_k3j2h1g0f9.png"'
    )
    # PocketBase keeps "=" in a stored extension (v0.22.34 file.go l.169).
    assert (
        content_disposition("attachment", "report_k3j2h1g0f9.v=1")
        == 'attachment; filename="report_k3j2h1g0f9.v=1"'
    )
    assert (
        content_disposition("attachment", 'my "report".pdf')
        == 'attachment; filename="my \\"report\\".pdf"'
    )


def test_content_disposition_writes_every_name_as_is_under_0_22() -> None:
    """v0.22.34 ``filesystem.go`` l.418 (also v0.23.0 to v0.39.x)."""
    for name in (
        "photo_k3j2h1g0f9.png",
        "100x100t_photo_k3j2h1g0f9.png",
        "report_k3j2h1g0f9.v=1",
        'my "report".pdf',
        "rapport été.pdf",
    ):
        assert (
            content_disposition("inline", name, compat=POCKETBASE_022)
            == f"inline; filename={name}"
        )
    # A raw name still cannot split the header: Go's writer turns CR/LF into
    # spaces, and so does latin1_header_value.
    raw = content_disposition("attachment", "x\r\nSet-Cookie: a=b", compat="0.22")
    assert latin1_header_value(raw) == "attachment; filename=x  Set-Cookie: a=b"


def test_go_quote_matches_strconv_quote() -> None:
    assert go_quote("a\\b") == '"a\\\\b"'
    # A control character can never split the header.
    assert go_quote("x\r\nSet-Cookie: a=b") == '"x\\r\\nSet-Cookie: a=b"'
    assert go_quote("x\x00\x7f") == '"x\\x00\\x7f"'
    assert go_quote("rapport été.pdf") == '"rapport été.pdf"'
    assert go_quote("a\u00a0b") == '"a\\u00a0b"'
    # surrogateescape bytes and lone surrogates print their bytes, as Go
    # prints invalid UTF-8.
    assert go_quote("a\udcff") == '"a\\xff"'
    assert go_quote("a\ud800") == '"a\\xed\\xa0\\x80"'
    # Lone surrogates outside the surrogateescape bytes become U+FFFD.
    assert latin1_header_value("x\udfff\udc7f") == "x" + "\xef\xbf\xbd" * 2
    # Starlette writes the UTF-8 bytes of the value, as Go does.
    wire = latin1_header_value(go_quote("été")).encode("latin-1")
    assert wire == '"été"'.encode()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Go: CR and LF become spaces, then the value is trimmed.
        ("image/png\r\nX-Injected: 1", "image/png  X-Injected: 1"),
        ("\r\n image/png \n", "image/png"),
        # Other control characters, refused by uvicorn and h11: spaces.
        ("text/plain\x00\x1f\x7f; charset=utf-8", "text/plain   ; charset=utf-8"),
        # A horizontal tab is a legal header character, kept inside.
        ("a\tb", "a\tb"),
        ("image/jpeg", "image/jpeg"),
        # A lone surrogate (only a hook can produce one) never raises.
        ("image/png\ud800", "image/png\xef\xbf\xbd"),
        # A surrogateescape byte is sent as that byte.
        ("a\udce9", "a\xe9"),
    ],
)
def test_header_values_never_carry_control_characters(
    value: str, expected: str
) -> None:
    """A crafted ``.attrs`` content type cannot split nor break the response."""
    sent = latin1_header_value(value)
    assert sent == expected
    assert not any(ord(char) < 0x20 and char != "\t" for char in sent)
    assert "\x7f" not in sent
    sent.encode("latin-1")


def test_compat_mode_and_version_specific_headers() -> None:
    class _State:
        pass

    class _App:
        state = _State()

    app = _App()
    assert pocketbase_compat(app) == ""
    assert pocketbase_compat(object()) == ""
    app.state.error_compat = "0.22"
    assert pocketbase_compat(app) == POCKETBASE_022
    app.state.error_compat = "0.23"
    assert pocketbase_compat(app) == ""
    # v0.40.6 apis/middlewares.go l.293-296; v0.22.34 sends no COOP.
    assert file_security_headers() == {
        "X-XSS-Protection": "1; mode=block",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    assert file_security_headers(POCKETBASE_022) == {
        "X-XSS-Protection": "1; mode=block",
        "X-Content-Type-Options": "nosniff",
    }


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


def test_thumbnail_types_and_formats_per_version() -> None:
    # v0.40.6 apis/file.go l.22 thumbnails webp; v0.22.34 l.26 does not.
    assert "image/webp" in thumb_image_content_types()
    assert "image/webp" not in thumb_image_content_types(POCKETBASE_022)
    for content_type in ("image/png", "image/jpg", "image/jpeg", "image/gif"):
        assert content_type in thumb_image_content_types()
        assert content_type in thumb_image_content_types(POCKETBASE_022)
    # v0.40.6 filesystem.go l.700-721: the format follows the original type,
    # anything else (webp, image/jpg) becomes a PNG recorded as image/png.
    assert thumb_output("image/jpeg", "100x100_a.png") == ("JPEG", "image/jpeg")
    assert thumb_output("image/gif", "100x100_a.gif") == ("GIF", "image/gif")
    assert thumb_output("image/png", "100x100_a.jpg") == ("PNG", "image/png")
    assert thumb_output("image/jpg", "100x100_a.jpg") == ("PNG", "image/png")
    assert thumb_output("image/webp", "100x100_a.webp") == ("PNG", "image/png")
    # v0.22.34 filesystem.go l.501-516: the format follows the thumbnail
    # name (imaging FormatFromFilename, case-insensitive, PNG otherwise)
    # and the original's type is recorded.
    compat = POCKETBASE_022
    assert thumb_output("image/png", "100x100_a.jpg", compat=compat) == (
        "JPEG",
        "image/png",
    )
    assert thumb_output("image/jpeg", "100x100_a.JPEG", compat=compat) == (
        "JPEG",
        "image/jpeg",
    )
    assert thumb_output("image/jpg", "100x100_a.jpg", compat=compat) == (
        "JPEG",
        "image/jpg",
    )
    assert thumb_output("image/png", "100x100_a.tif", compat=compat) == (
        "TIFF",
        "image/png",
    )
    assert thumb_output("image/png", "100x100_a.bmp", compat=compat) == (
        "BMP",
        "image/png",
    )
    assert thumb_output("image/gif", "100x100_a.v=1", compat=compat) == (
        "PNG",
        "image/gif",
    )


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


@pytest.mark.parametrize("compat", ["", POCKETBASE_022])
def test_parse_attrs_reads_the_fileblob_side_car(compat: str) -> None:
    attrs = parse_attrs(_pocketbase_attrs("image/png"), compat=compat)
    assert attrs.content_type == "image/png"
    assert attrs.metadata == {"original-filename": "photo.png"}
    # The v0.40.6 form: no newline, {} metadata.
    assert parse_attrs(
        b'{"user.content_type":"image/gif","user.metadata":{}}', compat=compat
    ) == parse_attrs(b'  {"user.content_type":"image/gif"}\r\n', compat=compat)
    assert parse_attrs(b"null", compat=compat).content_type == ""
    assert (
        parse_attrs(b'{"unknown": [1, {"a": null}]}', compat=compat).content_type == ""
    )
    # null stores nothing; a null metadata value is "" (both decoders).
    assert parse_attrs(b'{"user.content_type":null}', compat=compat).content_type == ""
    assert parse_attrs(
        b'{"user.content_type":"image/png","user.metadata":{"a":null},"md5":null}',
        compat=compat,
    ).metadata == {"a": ""}
    assert (
        parse_attrs(
            b'{"user.content_type":"a\\u00e9\\ud83d\\ude00"}', compat=compat
        ).content_type
        == "a\u00e9\U0001f600"
    )


@pytest.mark.parametrize(
    ("payload", "v1", "v2"),
    [
        # json.Decoder (v1) reads the first value only; json/v2
        # UnmarshalRead consumes the whole input.
        (b'{"user.content_type":"image/gif"} trailing', "image/gif", None),
        (b'{"user.content_type":"image/gif"}{}', "image/gif", None),
        # v1 keeps the last duplicate; v2 rejects duplicate names anywhere.
        (
            b'{"user.content_type":"image/png","user.content_type":"text/html"}',
            "text/html",
            None,
        ),
        (b'{"user.content_type":"image/png","x":{"a":1,"a":2}}', "image/png", None),
        (b'{"user.content_type":"a","user.content_type":null}', "a", None),
        # v1 matches names case-insensitively (Go foldName: U+017F is "s");
        # v2 matches them exactly and ignores the unknown member.
        (b'{"USER.CONTENT_TYPE":"image/gif"}', "image/gif", ""),
        (b'{"u\xc5\xbfer.content_type":"image/bmp"}', "image/bmp", ""),
        # ...but Python's full case mapping ("\u0131".upper() == "I") is not Go's.
        (b'{"user.content_d\xc4\xb1sposition":1,"user.content_type":"a"}', "a", "a"),
        # v1 replaces invalid UTF-8 and lone surrogate escapes with U+FFFD.
        (b'{"user.content_type":"image/png\xff"}', "image/png\ufffd", None),
        (b'{"user.content_type":"x\xe2\x82y"}', "x\ufffd\ufffdy", None),
        (b'{"user.content_type":"image/png\\ud800"}', "image/png\ufffd", None),
        (b'{"user.content_type":"a\\udc80"}', "a\ufffd", None),
        (b'{"user.content_type":"a","x":"\\ud800"}', "a", None),
        # md5: base64; v1 ignores CR/LF and also takes an array of bytes.
        (b'{"user.content_type":"a","md5":"kAFQmDzST7DWlj99\\nKOF/cg=="}', "a", None),
        (b'{"user.content_type":"a","md5":[1,2,255,null]}', "a", None),
    ],
)
def test_parse_attrs_json_v1_and_v2(payload: bytes, v1: str, v2: str | None) -> None:
    assert parse_attrs(payload, compat=POCKETBASE_022).content_type == v1
    if v2 is None:
        with pytest.raises(InvalidFileAttributes):
            parse_attrs(payload)
    else:
        assert parse_attrs(payload).content_type == v2


@pytest.mark.parametrize("compat", ["", POCKETBASE_022])
@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"   ",
        b"{",
        b"[]",
        b'"image/png"',
        b"12",
        b'{"user.content_type": 12}',
        b'{"user.content_type": true}',
        b'{"user.metadata": {"a": 1}}',
        b'{"user.metadata": []}',
        b'{"user.cache_control": false}',
        b'{"md5": 5}',
        b'{"md5": "%%%"}',
        b'{"md5": "YQ"}',
        b'{"md5": [256]}',
        b'{"md5": [-1]}',
        b'{"md5": [1.0]}',
        # json v1 ParseUint range error, beyond Python's int() digit limit.
        b'{"md5": [1, ' + b"9" * 5000 + b"]}",
        b'{"md5": [1000]}',
        b'{"md5": {}}',
        b'{"x": NaN, "user.content_type": "a"}',
        b'{"x": Infinity}',
        b'\xc2\xa0{"user.content_type": "a"}',
        b'\xef\xbb\xbf{"user.content_type": "a"}',
        b"\xff\xfe",
        b"[" * 3000 + b"]" * 3000,
    ],
)
def test_parse_attrs_rejects_what_go_cannot_decode(payload: bytes, compat: str) -> None:
    with pytest.raises(InvalidFileAttributes):
        parse_attrs(payload, compat=compat)


def test_encode_attrs_writes_the_fileblob_layout() -> None:
    content = b"thumbnail bytes"
    md5 = base64.b64encode(hashlib.md5(content).digest()).decode()
    members = (
        '{"user.cache_control":"","user.content_disposition":"",'
        '"user.content_encoding":"","user.content_language":"",'
        '"user.content_type":"image/png","user.metadata":'
    )
    # v0.22.34 (gocloud v0.40.0 json.Encoder): null metadata, final newline.
    encoded = encode_attrs(content, "image/png", compat=POCKETBASE_022)
    assert encoded == (members + f'null,"md5":"{md5}"}}\n').encode()
    # v0.40.6 (json.MarshalWrite): {} metadata, no newline.
    assert (
        encode_attrs(content, "image/png")
        == (members + f'{{}},"md5":"{md5}"}}').encode()
    )
    for compat in ("", POCKETBASE_022):
        decoded = parse_attrs(encode_attrs(content, "image/png", compat=compat))
        assert decoded.content_type == "image/png"
    with_metadata = encode_attrs(
        content, "text/html", {"original-filename": "<a&b>"}, compat=POCKETBASE_022
    )
    assert b'"original-filename":"\\u003ca\\u0026b\\u003e"' in with_metadata
    assert b'"original-filename":"<a&b>"' in encode_attrs(
        content, "text/html", {"original-filename": "<a&b>"}
    )


def test_constant_headers() -> None:
    assert CONTENT_SECURITY_POLICY == (
        "default-src 'none'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
    )
    assert CACHE_CONTROL == "max-age=2592000, stale-while-revalidate=86400"


def test_go_trim_space_removes_the_go_unicode_spaces_only() -> None:
    """Go ``strings.TrimSpace`` trims ``unicode.IsSpace`` (White_Space).

    Python's ``str.isspace`` adds U+001C-U+001F (bidirectional class B/S),
    which Go keeps: everything else matches, for every code point.
    """
    python_only = {0x1C, 0x1D, 0x1E, 0x1F}
    for code in range(0x110000):
        char = chr(code)
        expected = char.isspace() and code not in python_only
        assert (go_trim_space(f"{char}a{char}") == "a") is expected, hex(code)
    assert go_trim_space("\u3000\xa0 a b\t\n\u2029") == "a b"
    assert go_trim_space("\x1ca\u200b") == "\x1ca\u200b"


@pytest.mark.parametrize(
    ("raw", "compat", "expected"),
    [
        (None, "", ""),
        (None, POCKETBASE_022, ""),
        ("aa.bb.cc", "", "aa.bb.cc"),
        # v0.22.34 trims (apis/file.go l.222), v0.40.6 does not (l.115).
        (" aa.bb.cc\t", "", ""),
        (" aa.bb.cc\t", POCKETBASE_022, "aa.bb.cc"),
        ("\u3000aa.bb.cc\x85", POCKETBASE_022, "aa.bb.cc"),
        ("  \u2000 ", POCKETBASE_022, ""),
        ("aa.bb.cc\x1c", POCKETBASE_022, ""),
        # RawURLEncoding: the URL-safe alphabet without padding.
        ("aa.bb.cc=", "", ""),
        ("aa.b+b.cc", "", ""),
        ("aa.b/b.cc", "", ""),
        ("aa.bb.c\u200bc", POCKETBASE_022, ""),
        ("aa.bb", "", ""),
        ("aa.bb.cc.dd", "", ""),
        ("", "", ""),
        # Go's base64 skips CR and LF: harmless in the signature segment.
        ("aa.bb.c\nc\r", "", "aa.bb.cc"),
        ("aa.bb.cc\n", "", "aa.bb.cc"),
        # In the signed segments they stay (the signature then fails).
        ("a\na.bb.cc", "", "a\na.bb.cc"),
    ],
)
def test_file_token_to_verify(raw: str | None, compat: str, expected: str) -> None:
    assert file_token_to_verify(raw, compat=compat) == expected
