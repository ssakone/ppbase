"""Request binding of the PocketBase 0.22 forms (admin password reset routes).

PocketBase 0.22.34 reads these forms with ``c.Bind`` and ``rest.MultiBinder``
(``v0.22.34 tools/rest/multi_binder.go:29-146``), after the
``eagerRequestInfoCache`` middleware of every ``/api`` route has parsed the
body once and ignored its errors (``apis/middlewares.go:405-421``,
``apis/record_helpers.go:64-65``, ``apis/base.go:120``).  The resulting rules,
each measured on the official 0.22.34 binary with synthetic data:

- ``Content-Length: 0`` (or no body) binds nothing, even with a query string;
  a chunked body is read even when it is empty.
- ``Content-Type`` is compared by case-sensitive prefix:
  ``application/json`` (``application/json; charset=utf-8`` too),
  ``application/x-www-form-urlencoded`` and ``multipart/form-data``.  No
  type, XML or any other type is a loading error.
- JSON (``json.NewDecoder(...).Decode``): only the first value is read and
  trailing bytes are ignored; ``null`` binds nothing and any other
  non-object is a loading error.  A key binds a field when it is the field
  name or, failing that, equal to it under Unicode simple case folding (the
  Kelvin sign and the long s fold to ``k`` and ``s``, the dotless and dotted
  i do not); the last such key wins and a ``null`` value keeps the previous
  one.  A field given another type than a string is a loading error.  Invalid
  UTF-8 and lone surrogate escapes become U+FFFD; ``NaN``, a byte order mark,
  UTF-16 or a blank body are loading errors.
- Forms: the values of the urlencoded body come before those of the query
  string, and after them for ``multipart/form-data`` (``Request.ParseForm``
  and ``ParseMultipartForm``).  Pairs with a bad ``%`` escape or a ``;`` are
  dropped without error (the eager parse already swallowed the error), ``+``
  is a space, a key without ``=`` has an empty value.  A field takes the first
  value of its exact key, else of a key equal under case folding.  The
  ``@jsonPayload`` values are decoded first like ``json.Unmarshal`` (invalid
  JSON is ignored, a wrongly typed field skipped) and the form fields then
  override them.
- Multipart (``mime/multipart``): a part is a value when its
  ``Content-Disposition`` is ``form-data`` with a ``name`` (RFC 2231
  ``name*`` included) and no non-empty ``filename``: ``filename=""`` is a
  value, a part without a name or of another disposition is skipped, a file
  part ignored.  A preamble before the first boundary is skipped.  The body
  may end inside the headers of a part (Go's reader stops there without
  error); a body ending anywhere else before the closing boundary, a missing
  or empty boundary, or a malformed ``Content-Type`` is a loading error.

Known limits, outside the cases measured identical: invalid UTF-8 in a form
value counts as U+FFFD, three bytes, where Go keeps the byte (the e-mail check
gives the same answer); Python's JSON decoder stops a few levels before Go's
10000-level nesting limit (9997 here); multipart parts are split by
python-multipart, which refuses LF-only line endings and blanks after a
boundary, both accepted by Go.
"""

from __future__ import annotations

import codecs
import json
import re
from typing import Any

from python_multipart.exceptions import FormParserError
from python_multipart.multipart import MultipartParser
from starlette.requests import Request

from ppbase.services.legacy_validation import go_string

LOAD_ERROR_MESSAGE = "An error occurred while loading the submitted data."
JSON_PAYLOAD_KEY = "@jsonPayload"  # tools/rest/multi_binder.go:18

_JSON_SPACE = " \t\n\r"
# Go's unicode case orbits add one rune to two ASCII letters (KELVIN SIGN and
# LATIN SMALL LETTER LONG S); the field names are ASCII.
_EXTRA_FOLDS = {"k": "K", "s": "ſ"}


class FormLoadError(Exception):
    """The body cannot be bound to the form (``c.Bind`` error in PocketBase)."""


def _go_utf8_replace(exc: UnicodeError) -> tuple[str, int]:
    if not isinstance(exc, UnicodeDecodeError):
        raise exc
    # Go's utf8.DecodeRune reports each byte of an invalid sequence as one
    # U+FFFD; Python reports the bytes of the longest invalid prefix at once.
    return "�" * (exc.end - exc.start), exc.end


codecs.register_error("ppbase.go-utf8", _go_utf8_replace)


def go_decode(data: bytes) -> str:
    """Bytes read as a Go JSON string: one U+FFFD per invalid UTF-8 byte."""
    return data.decode("utf-8", "ppbase.go-utf8")


# ---------------------------------------------------------------------------
# Field matching
# ---------------------------------------------------------------------------


def _folds_to(key: str, name: str) -> bool:
    """Go ``strings.EqualFold(key, name)`` for an ASCII ``name``."""
    if len(key) != len(name):
        return False
    for got, want in zip(key, name, strict=True):
        lower = want.lower()
        if got != lower and got != want.upper() and got != _EXTRA_FOLDS.get(lower):
            return False
    return True


def _field_for(key: str, names: tuple[str, ...]) -> str | None:
    if key in names:
        return key
    for name in names:
        if _folds_to(key, name):
            return name
    return None


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


class _Pairs(list):
    """A JSON object as its ordered ``(key, value)`` pairs, duplicates kept."""


def _reject_constant(name: str) -> Any:
    raise ValueError(f"invalid JSON literal {name}")


# Numbers are never bound to these string fields: they are not converted.
_DECODER = json.JSONDecoder(
    object_pairs_hook=_Pairs,
    parse_constant=_reject_constant,
    parse_float=lambda raw: 0.0,
    parse_int=lambda raw: 0,
)


def _assign_json(value: Any, names: tuple[str, ...], out: dict[str, str]) -> bool:
    """Bind a decoded JSON value like Go's ``json``; False on a type error."""
    if value is None:
        return True
    if not isinstance(value, _Pairs):
        return False
    typed = True
    for key, item in value:
        name = _field_for(go_string(key), names)
        if name is None or item is None:
            continue
        if isinstance(item, str):
            out[name] = go_string(item)
        else:
            typed = False
    return typed


def _bind_json(raw: bytes, names: tuple[str, ...]) -> dict[str, str]:
    text = go_decode(raw)
    start = len(text) - len(text.lstrip(_JSON_SPACE))
    try:
        value, _end = _DECODER.raw_decode(text, start)
    except (ValueError, RecursionError):
        raise FormLoadError from None
    out = dict.fromkeys(names, "")
    if not _assign_json(value, names, out):
        raise FormLoadError
    return out


# ---------------------------------------------------------------------------
# Media types (Go mime.ParseMediaType)
# ---------------------------------------------------------------------------

_TSPECIALS = frozenset('()<>@,;:\\"/[]?=')
_GO_SPACE = " \t\n\v\f\r"


def _is_token_char(char: str) -> bool:
    return " " < char < "\x7f" and char not in _TSPECIALS


def _consume_token(value: str) -> tuple[str, str]:
    for index, char in enumerate(value):
        if not _is_token_char(char):
            return value[:index], value[index:]
    return value, ""


def _consume_value(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    if value[0] != '"':
        return _consume_token(value)
    out: list[str] = []
    index = 1
    while index < len(value):
        char = value[index]
        if char == '"':
            return "".join(out), value[index + 1 :]
        if char == "\\" and index + 1 < len(value) and value[index + 1] in _TSPECIALS:
            out.append(value[index + 1])
            index += 2
            continue
        if char in "\r\n":
            return "", value
        out.append(char)
        index += 1
    return "", value  # no closing quote


def _consume_param(value: str) -> tuple[str, str, str]:
    rest = value.lstrip(_GO_SPACE)
    if not rest.startswith(";"):
        return "", "", value
    rest = rest[1:].lstrip(_GO_SPACE)
    param, rest = _consume_token(rest)
    param = param.lower()
    if not param:
        return "", "", value
    rest = rest.lstrip(_GO_SPACE)
    if not rest.startswith("="):
        return "", "", value
    rest = rest[1:].lstrip(_GO_SPACE)
    parsed, after = _consume_value(rest)
    if not parsed and after == rest:
        return "", "", value
    return param, parsed, after


_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


def _percent_unescape(value: str) -> str | None:
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return None
    return _PERCENT.sub(lambda match: chr(int(match.group(1), 16)), value)


def _decode_2231(value: str) -> str | None:
    parts = value.split("'", 2)
    if len(parts) != 3 or parts[0].lower() not in ("us-ascii", "utf-8"):
        return None
    return _percent_unescape(parts[2])


def go_parse_media_type(header: str) -> tuple[str, dict[str, str] | None]:
    """Go ``mime.ParseMediaType`` on a Latin-1 decoded header value.

    Returns the lower-cased media type and its parameters, ``None`` for the
    parameters when Go answers an error (the media type is then still given
    when only a parameter is malformed, as in Go).  Parameter values are
    Latin-1 strings holding the raw bytes.
    """
    base = header.split(";", 1)[0]
    media_type = base.lower().strip(_GO_SPACE)
    kind, rest = _consume_token(media_type)
    if not kind:
        return "", None
    if rest:
        subtype, rest = _consume_token(rest[1:]) if rest.startswith("/") else ("", rest)
        if not subtype or rest:
            return "", None
    params: dict[str, str] = {}
    continuations: dict[str, dict[str, str]] = {}
    remaining = header[len(base) :]
    while remaining:
        remaining = remaining.lstrip(_GO_SPACE)
        if not remaining:
            break
        key, value, rest = _consume_param(remaining)
        if not key:
            if rest.strip(_GO_SPACE) == ";":
                break  # a trailing semicolon is not an error
            return media_type, None
        target = params
        if "*" in key:
            target = continuations.setdefault(key.split("*", 1)[0], {})
        if key in target and target[key] != value:
            return "", None  # duplicate parameter
        target[key] = value
        remaining = rest
    for name, pieces in continuations.items():
        if name + "*" in pieces:
            decoded = _decode_2231(pieces[name + "*"])
            if decoded is not None:
                params[name] = decoded
            continue
        joined: list[str] = []
        valid = False
        index = 0
        while True:
            simple = f"{name}*{index}"
            if simple in pieces:
                joined.append(pieces[simple])
            elif simple + "*" in pieces:
                encoded = pieces[simple + "*"]
                part = _decode_2231(encoded) if index == 0 else _percent_unescape(encoded)
                joined.append(part or "")
            else:
                break
            valid = True
            index += 1
        if valid:
            params[name] = "".join(joined)
    return media_type, params


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------


def _go_unescape(data: bytes) -> bytes | None:
    """Go ``url.QueryUnescape``: ``+`` is a space, a bad ``%`` escape fails."""
    if b"%" not in data and b"+" not in data:
        return data
    out = bytearray()
    index = 0
    size = len(data)
    while index < size:
        byte = data[index]
        if byte == 0x25:  # %
            pair = data[index + 1 : index + 3]
            if index + 2 >= size or not all(chr(c) in "0123456789abcdefABCDEF" for c in pair):
                return None
            out.append(int(pair, 16))
            index += 3
        else:
            out.append(0x20 if byte == 0x2B else byte)
            index += 1
    return bytes(out)


def _go_parse_query(raw: bytes) -> list[tuple[str, str]]:
    """Go ``url.ParseQuery`` keeping the valid pairs (its error is ignored)."""
    pairs: list[tuple[str, str]] = []
    for part in raw.split(b"&"):
        if not part or b";" in part:
            continue
        key, _sep, value = part.partition(b"=")
        key_bytes = _go_unescape(key)
        value_bytes = _go_unescape(value) if key_bytes is not None else None
        if key_bytes is None or value_bytes is None:
            continue
        pairs.append((go_decode(key_bytes), go_decode(value_bytes)))
    return pairs


def _merge(*sources: list[tuple[str, str]]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for pairs in sources:
        for key, value in pairs:
            values.setdefault(key, []).append(value)
    return values


def _bind_values(values: dict[str, list[str]], names: tuple[str, ...]) -> dict[str, str]:
    out = dict.fromkeys(names, "")
    if not values:
        return out
    for payload in values.get(JSON_PAYLOAD_KEY, ()):
        try:
            decoded = _DECODER.decode(payload)
        except (ValueError, RecursionError):
            continue
        _assign_json(decoded, names, out)
    for name in names:
        if name in values:
            out[name] = values[name][0]
            continue
        for key, items in values.items():
            if _folds_to(key, name):
                out[name] = items[0]
                break
    return out


# ---------------------------------------------------------------------------
# Multipart
# ---------------------------------------------------------------------------

# Go's ReadForm keeps at most 32 MiB of values in memory, plus 10 MiB
# (net/http defaultMaxMemory, mime/multipart): beyond, the body is refused.
_MAX_MULTIPART_VALUE_BYTES = (32 << 20) + (10 << 20)
_MAX_PREAMBLE_BYTES = 1 << 20


class _MultipartCollector:
    """Text parts of a ``multipart/form-data`` body, kept like Go's ``ReadForm``."""

    def __init__(self) -> None:
        self.pairs: list[tuple[str, str]] = []
        self.finished = False
        self.in_headers = False
        self._size = 0
        self._header_name = b""
        self._header_value = b""
        self._disposition: bytes | None = None
        self._name: str | None = None
        self._data = bytearray()

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value_data,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
            "on_end": self._end,
        }

    def _part_begin(self) -> None:
        self.in_headers = True
        self._disposition = None
        self._name = None
        self._data = bytearray()

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def _header_value_data(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _header_end(self) -> None:
        if self._header_name.lower() == b"content-disposition" and self._disposition is None:
            self._disposition = self._header_value
        self._header_name = b""
        self._header_value = b""

    def _headers_finished(self) -> None:
        self.in_headers = False
        # multipart.Part.FormName and FileName: an unparsable disposition has
        # no parameters, hence no name; a non-empty filename makes a file.
        disposition, params = go_parse_media_type((self._disposition or b"").decode("latin-1"))
        params = params or {}
        name = params.get("name", "")
        if disposition == "form-data" and name and not params.get("filename"):
            self._name = name

    def _part_data(self, data: bytes, start: int, end: int) -> None:
        if self._name is None:
            return
        self._size += end - start
        if self._size > _MAX_MULTIPART_VALUE_BYTES:
            raise FormLoadError
        self._data += data[start:end]

    def _part_end(self) -> None:
        if self._name is not None:
            self.pairs.append(
                (go_decode(self._name.encode("latin-1")), go_decode(bytes(self._data)))
            )

    def _end(self) -> None:
        self.finished = True


def _first_boundary(data: bytes, boundary: bytes) -> int | None:
    """Offset of the first boundary line of ``data``; Go skips the lines before."""
    dash = b"--" + boundary
    offset = 0
    while offset < len(data):
        newline = data.find(b"\n", offset)
        line_end = len(data) if newline == -1 else newline + 1
        line = data[offset:line_end]
        if line.startswith(dash):
            rest = line[len(dash) :]
            if rest.startswith(b"--"):
                rest = rest[2:]
            if rest.lstrip(b" \t") in (b"", b"\r\n", b"\n"):
                return offset
        offset = line_end
    return None


async def _multipart_pairs(request: Request, params: dict[str, str]) -> list[tuple[str, str]]:
    boundary = params.get("boundary", "").encode("latin-1")
    if not boundary:
        raise FormLoadError
    collector = _MultipartCollector()
    parser = MultipartParser(boundary, collector.callbacks(), max_header_count=1000)
    preamble = bytearray()
    started = False
    try:
        async for chunk in request.stream():
            if started:
                parser.write(chunk)
                continue
            preamble += chunk
            complete = preamble.rfind(b"\n") + 1
            offset = _first_boundary(bytes(preamble[:complete]), boundary)
            if offset is not None:
                parser.write(bytes(preamble[offset:]))
                started = True
                continue
            del preamble[:complete]
            if len(preamble) > _MAX_PREAMBLE_BYTES:
                raise FormLoadError
        if not started:
            offset = _first_boundary(bytes(preamble), boundary)
            if offset is None:
                raise FormLoadError
            parser.write(bytes(preamble[offset:]))
        parser.finalize()
    except FormParserError:
        raise FormLoadError from None
    if not collector.finished and not collector.in_headers:
        raise FormLoadError
    return collector.pairs


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_MAX_JSON_BYTES = 32 << 20  # rest.DefaultMaxMemory
_MAX_FORM_BYTES = 10 << 20  # net/http parsePostForm maxFormSize


async def _read_at_most(request: Request, limit: int) -> bytes:
    """At most ``limit + 1`` bytes of the body (more would not be read by Go)."""
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            break
    return bytes(data[: limit + 1])


def _content_length_is_zero(request: Request) -> bool:
    """Go ``Request.ContentLength == 0``: no body, chunked bodies excluded."""
    if "chunked" in request.headers.get("transfer-encoding", "").lower():
        return False
    length = request.headers.get("content-length")
    if length is None:
        return True
    try:
        return int(length) == 0
    except ValueError:
        return False


async def bind_legacy_form(request: Request, names: tuple[str, ...]) -> dict[str, str]:
    """String fields ``names`` of a PocketBase 0.22 form, bound like ``c.Bind``.

    Raises :class:`FormLoadError` where PocketBase answers ``400 An error
    occurred while loading the submitted data.``
    """
    if _content_length_is_zero(request):
        return dict.fromkeys(names, "")
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        # CopyJsonBody decodes from the first 32 MiB (tools/rest/multi_binder.go:81).
        raw = await _read_at_most(request, _MAX_JSON_BYTES)
        return _bind_json(raw[:_MAX_JSON_BYTES], names)
    query = _go_parse_query(request.scope.get("query_string", b""))
    media_type, params = go_parse_media_type(content_type)
    if content_type.startswith("application/x-www-form-urlencoded"):
        # Request.ParseForm reads the body only for this exact media type, and
        # drops a body over 10 MiB (the eager parse swallowed that error too).
        body = b""
        if media_type == "application/x-www-form-urlencoded":
            body = await _read_at_most(request, _MAX_FORM_BYTES)
            if len(body) > _MAX_FORM_BYTES:
                body = b""
        return _bind_values(_merge(_go_parse_query(body), query), names)
    if content_type.startswith("multipart/form-data"):
        if media_type != "multipart/form-data" or params is None:
            raise FormLoadError  # Request.multipartReader: ErrNotMultipart
        return _bind_values(_merge(query, await _multipart_pairs(request, params)), names)
    raise FormLoadError
