"""PocketBase file-serving rules shared by ``GET/HEAD /api/files/...``.

PocketBase serves a stored file with ``tools/filesystem`` ``Serve``
(v0.22.34 ``filesystem.go`` l.393-430, v0.40.6 l.562-599):

- the real content type is the one recorded in the blob attributes, which a
  local (``fileblob``) storage keeps in a JSON ``<file>.attrs`` side-car
  (gocloud ``fileblob``; vendored copy v0.40.6
  ``tools/filesystem/internal/fileblob/attrs.go`` l.28-87);
- ``Content-Disposition`` is ``inline`` for a short list of safe types and
  ``attachment`` otherwise or when ``?download`` parses as a Go boolean true;
- some extensions force their content type;
- a sandboxing ``Content-Security-Policy`` and a 30-day ``Cache-Control``
  are always sent.

The rules that PocketBase v0.22.34 and the current release (v0.40.6) apply
identically hold in every mode.  Where the two versions differ, PPBase
follows v0.40.6 by default and v0.22.34 when the application runs in the
PocketBase 0.22 compatibility mode (:func:`pocketbase_compat`): the quoting
of the ``Content-Disposition`` file name, the extension overrides,
``Cross-Origin-Opener-Policy``, webp thumbnails, the thumbnail format, the
JSON decoding of side-cars and the trimming of a protected-file token.  Each
constant cites its sources; the list of differences is in
``docs/file-serving.md``.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

#: The PocketBase 0.22 compatibility mode; ``""`` is the default behavior.
POCKETBASE_022 = "0.22"


def pocketbase_compat(app: Any) -> str:
    """Return ``"0.22"`` when ``app`` runs in PocketBase 0.22 compatibility mode.

    The mode is the one of the PocketBase 0.22 error compatibility
    (``ppbase.middleware.error_compat``): ``app.state.error_compat``, set by
    ``set_error_compat("0.22")`` or ``PPBASE_ERROR_COMPAT=0.22``.  This reads
    the same attribute, so that one switch selects the 0.22 behavior.  Any
    other value, or none, is the default (current PocketBase) behavior.
    """
    state = getattr(app, "state", None)
    mode = getattr(state, "error_compat", "") if state is not None else ""
    return POCKETBASE_022 if mode == POCKETBASE_022 else ""


#: v0.22.34 ``filesystem.go`` l.367-377 == v0.40.6 ``filesystem.go`` l.521-531.
INLINE_SERVE_CONTENT_TYPES = frozenset(
    {
        # image
        "image/png",
        "image/jpg",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/x-icon",
        "image/bmp",
        # video
        "video/webm",
        "video/mp4",
        "video/3gpp",
        "video/quicktime",
        "video/x-ms-wmv",
        # audio
        "audio/basic",
        "audio/aiff",
        "audio/mpeg",
        "audio/midi",
        "audio/mp3",
        "audio/wave",
        "audio/wav",
        "audio/x-wav",
        "audio/x-mpeg",
        "audio/x-m4a",
        "audio/aac",
        # document
        "application/pdf",
        "application/x-pdf",
    }
)

#: v0.22.34 ``filesystem.go`` l.380-383.  The extension is matched
#: case-sensitively, like Go's ``filepath.Ext``.
MANUAL_EXTENSION_CONTENT_TYPES_022 = {
    ".svg": "image/svg+xml",
    ".css": "text/css",
}

#: v0.40.6 ``filesystem.go`` l.534-549 (``.js``/``.mjs`` since issue 6597,
#: the Office types since discussion 7467).
MANUAL_EXTENSION_CONTENT_TYPES = {
    **MANUAL_EXTENSION_CONTENT_TYPES_022,
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
}

#: v0.22.34 ``filesystem.go`` l.420 == v0.40.6 l.589.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
)

#: v0.22.34 ``filesystem.go`` l.425 == v0.40.6 l.594.
CACHE_CONTROL = "max-age=2592000, stale-while-revalidate=86400"

#: Global security headers that both versions send on every response, the
#: file route included: v0.22.34 ``apis/base.go`` l.53 (echo
#: ``middleware.Secure()`` defaults) and v0.40.6 ``apis/middlewares.go``
#: l.293-294.  ``X-Frame-Options`` is deliberately absent: the file route
#: deletes it (v0.22.34 ``apis/file.go`` l.206, v0.40.6 l.205).
FILE_SECURITY_HEADERS = {
    "X-XSS-Protection": "1; mode=block",
    "X-Content-Type-Options": "nosniff",
}

#: v0.40.6 only (``apis/middlewares.go`` l.296); v0.22.34 never sends it.
CROSS_ORIGIN_OPENER_POLICY = "same-origin"

#: v0.22.34 ``apis/file.go`` l.27 == v0.40.6 ``apis/file.go`` l.23.
DEFAULT_THUMB_SIZES = ("100x100",)

#: v0.22.34 ``apis/file.go`` l.26: no webp (a webp original is served as is).
THUMB_IMAGE_CONTENT_TYPES_022 = frozenset(
    {"image/png", "image/jpg", "image/jpeg", "image/gif"}
)

#: v0.40.6 ``apis/file.go`` l.22 adds ``image/webp``.
THUMB_IMAGE_CONTENT_TYPES = THUMB_IMAGE_CONTENT_TYPES_022 | {"image/webp"}

#: The query parameter forcing an attachment (v0.22.34 ``filesystem.go``
#: l.387, v0.40.6 l.553).
FORCE_ATTACHMENT_PARAM = "download"

#: Content type of a blob without ``.attrs`` side-car in ``fileblob``
#: (v0.40.6 ``internal/fileblob/attrs.go`` l.69-74).  PPBase keeps its own
#: extension-based detection for files it uploaded itself (they have no
#: side-car); see ``docs/file-serving.md``.
FILEBLOB_DEFAULT_CONTENT_TYPE = "application/octet-stream"

ATTRS_SUFFIX = ".attrs"

_GO_TRUE = frozenset({"1", "t", "T", "TRUE", "true", "True"})
_GO_FALSE = frozenset({"0", "f", "F", "FALSE", "false", "False"})


class InvalidFileAttributes(ValueError):
    """A ``.attrs`` side-car exists but cannot be decoded like ``fileblob``."""


@dataclass(frozen=True)
class FileAttributes:
    """The decoded ``fileblob`` side-car of one stored object."""

    content_type: str
    metadata: dict[str, str] = field(default_factory=dict)


def go_parse_bool(raw: str) -> bool | None:
    """Return Go's ``strconv.ParseBool`` result, ``None`` for an error."""
    if raw in _GO_TRUE:
        return True
    if raw in _GO_FALSE:
        return False
    return None


def force_attachment(raw: str | None) -> bool:
    """``?download`` handling of ``Serve``: a non-empty Go boolean true.

    ``download=yes``, ``download=on`` or ``download=name.pdf`` are parse
    errors in Go, hence false (v0.22.34 ``filesystem.go`` l.400-403, v0.40.6
    l.569-572).
    """
    if not raw:
        return False
    return go_parse_bool(raw) is True


#: Go's ``unicode.IsSpace`` (the Unicode White_Space property), the runes
#: ``strings.TrimSpace`` removes: ``\t``, ``\n``, ``\v``, ``\f``, ``\r``,
#: space, U+0085, U+00A0, U+1680, U+2000-U+200A, U+2028, U+2029, U+202F,
#: U+205F and U+3000.  Python's ``str.strip()`` also removes U+001C-U+001F.
GO_UNICODE_SPACES = "".join(
    map(
        chr,
        (
            *range(0x09, 0x0E),
            0x20,
            0x85,
            0xA0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
        ),
    )
)

# A JWT segment as golang-jwt decodes it (``base64.RawURLEncoding``): the
# URL-safe alphabet, no padding; Go's base64 decoder skips CR and LF.
_JWT_SEGMENT_RE = re.compile(r"[A-Za-z0-9_\-\r\n]*")


def go_trim_space(value: str) -> str:
    """Go's ``strings.TrimSpace``: :data:`GO_UNICODE_SPACES` at both ends."""
    return value.strip(GO_UNICODE_SPACES)


def file_token_to_verify(raw: str | None, *, compat: str = "") -> str:
    """The protected-file token to verify, ``""`` when PocketBase has none.

    ``raw`` is the first ``?token`` value.  v0.22.34 trims it with
    ``strings.TrimSpace`` and treats an empty result as no token
    (``apis/file.go`` l.119, l.221-225); v0.40.6 passes it as is
    (``URL.Query().Get``, l.115-116).  golang-jwt then splits exactly three
    segments (v4 ``strings.Split``, v5 ``splitToken``) and decodes each with
    ``base64.RawURLEncoding``: any other character than the URL-safe
    alphabet, padding included, makes the token invalid, while CR and LF
    are skipped.  PyJWT accepts padding and refuses CR/LF, so a token Go
    could not decode is no token here, and CR/LF are taken out of the
    signature segment (in the two signed segments they stay, and the
    signature fails in both).
    """
    token = raw or ""
    if compat == POCKETBASE_022:
        token = go_trim_space(token)
    parts = token.split(".")
    if len(parts) != 3 or not all(_JWT_SEGMENT_RE.fullmatch(part) for part in parts):
        return ""
    signature = parts[2].replace("\r", "").replace("\n", "")
    return f"{parts[0]}.{parts[1]}.{signature}"


def disposition_type(real_content_type: str, *, forced: bool) -> str:
    """``inline`` for the safe types, ``attachment`` otherwise.

    The check uses the stored (real) content type, before the extension
    overrides (v0.22.34 ``filesystem.go`` l.405-409, v0.40.6 l.574-578).
    """
    if not forced and real_content_type in INLINE_SERVE_CONTENT_TYPES:
        return "inline"
    return "attachment"


def response_content_type(
    real_content_type: str, override_name: str, *, compat: str = ""
) -> str:
    """Apply the extension overrides of ``Serve``.

    v0.22.34 looks up the extension of the served name (l.412-415) in its two
    overrides; v0.40.6 the one of the served key (l.583-586) in its seven.
    The caller passes the name matching the mode.
    """
    overrides = (
        MANUAL_EXTENSION_CONTENT_TYPES_022
        if compat == POCKETBASE_022
        else MANUAL_EXTENSION_CONTENT_TYPES
    )
    return overrides.get(_go_ext(override_name), real_content_type)


def content_disposition(disposition: str, served_name: str, *, compat: str = "") -> str:
    """Build the ``Content-Disposition`` value of ``Serve``.

    v0.40.6 quotes every name with ``strconv.Quote`` (``filesystem.go``
    l.587; since v0.40.0, commit b259534d).  v0.22.34 writes the name as is
    (l.418), which is also the form of v0.23.0 to v0.39.x.  Either value then
    goes through :func:`latin1_header_value`, like Go's header writer.
    """
    if compat == POCKETBASE_022:
        return f"{disposition}; filename={served_name}"
    return f"{disposition}; filename={go_quote(served_name)}"


def file_security_headers(compat: str = "") -> dict[str, str]:
    """The global security headers PocketBase sends with a file."""
    headers = dict(FILE_SECURITY_HEADERS)
    if compat != POCKETBASE_022:
        headers["Cross-Origin-Opener-Policy"] = CROSS_ORIGIN_OPENER_POLICY
    return headers


def latin1_header_value(value: str) -> str:
    """Return ``value`` as Go's header writer would send it, for Starlette.

    Go replaces CR and LF with spaces and trims the value
    (``net/http`` ``Header.writeSubset``); the other control characters,
    which h11 refuses, become spaces too (only a crafted ``.attrs`` holds
    any).  Starlette encodes header values as Latin-1 while Go writes the
    UTF-8 bytes as is: mapping each UTF-8 byte to one Latin-1 code point
    sends the same octets on the wire.  A byte kept by ``surrogateescape``
    is sent as that byte; any other lone surrogate, which no Go string can
    hold, becomes U+FFFD, so that this never fails.
    """
    cleaned = _HEADER_CONTROL_RE.sub(" ", value).strip(" \t")
    cleaned = _NON_ESCAPE_SURROGATE_RE.sub("�", cleaned)
    return cleaned.encode("utf-8", "surrogateescape").decode("latin-1")


_HEADER_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
# Lone surrogates other than the U+DC80-U+DCFF bytes of ``surrogateescape``.
_NON_ESCAPE_SURROGATE_RE = re.compile("[\ud800-\udc7f\udd00-\udfff]")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def go_quote(value: str) -> str:
    """A Go ``strconv.Quote`` equivalent for header values."""
    out = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char in _GO_SHORT_ESCAPES:
            out.append(_GO_SHORT_ESCAPES[char])
        elif 0xDC80 <= code <= 0xDCFF:
            # An undecodable byte kept by surrogateescape: Go prints \xNN.
            out.append(f"\\x{code - 0xDC00:02x}")
        elif 0xD800 <= code <= 0xDFFF:
            # Only a hook can produce one: Go would see its invalid UTF-8.
            out.extend(
                f"\\x{byte:02x}" for byte in char.encode("utf-8", "surrogatepass")
            )
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02x}")
        elif code < 0x80 or char.isprintable():
            out.append(char)
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    out.append('"')
    return "".join(out)


_GO_SHORT_ESCAPES = {
    "\a": "\\a",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
}


def _go_ext(name: str) -> str:
    """Go ``filepath.Ext``: the suffix from the last dot of the last element."""
    base = name.rsplit("/", 1)[-1]
    index = base.rfind(".")
    return base[index:] if index >= 0 else ""


def thumb_size_allowed(thumb_size: str, field_thumbs: Any) -> bool:
    """Exact (case-sensitive, untrimmed) membership test of ``apis/file.go``.

    v0.22.34 l.163-164 == v0.40.6 l.165-166: the default ``100x100`` is
    always allowed, any other size must be one of the field's ``thumbs``.
    """
    if not thumb_size:
        return False
    if thumb_size in DEFAULT_THUMB_SIZES:
        return True
    if isinstance(field_thumbs, str):
        return thumb_size == field_thumbs
    if isinstance(field_thumbs, (list, tuple)):
        return any(
            isinstance(item, str) and item == thumb_size for item in field_thumbs
        )
    return False


def thumb_image_content_types(compat: str = "") -> frozenset[str]:
    """The stored types that get a thumbnail (``imageContentTypes``)."""
    if compat == POCKETBASE_022:
        return THUMB_IMAGE_CONTENT_TYPES_022
    return THUMB_IMAGE_CONTENT_TYPES


def thumb_names(thumb_size: str, filename: str) -> tuple[str, str]:
    """Return ``(directory, file)`` of a thumbnail (v0.22.34 l.174-175)."""
    served_name = f"{thumb_size}_{filename}"
    return f"thumbs_{filename}", served_name


# ``disintegration/imaging`` v1.6.2 ``io.go`` l.117-125 (``formatExts``).
_IMAGING_FORMAT_EXTS = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "gif": "GIF",
    "tif": "TIFF",
    "tiff": "TIFF",
    "bmp": "BMP",
}

# v0.40.6 ``filesystem.go`` l.706-721.
_V040_THUMB_FORMATS = {
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/tiff": "TIFF",
    "image/bmp": "BMP",
}


def thumb_output(
    original_content_type: str, thumb_name: str, *, compat: str = ""
) -> tuple[str, str]:
    """Return ``(Pillow format, side-car content type)`` of a new thumbnail.

    v0.22.34 encodes in the format named by the thumbnail's extension
    (``imaging.FormatFromFilename``, imaging v1.6.2 ``io.go`` l.144-156:
    lowercase, PNG when unknown; ``filesystem.go`` l.511-516) and records the
    original's content type (l.501-503).  v0.40.6 picks the format from the
    original's content type and encodes anything else, webp and ``image/jpg``
    included, as PNG recorded as ``image/png`` (l.700-721).
    """
    if compat == POCKETBASE_022:
        extension = _go_ext(thumb_name).removeprefix(".").lower()
        return _IMAGING_FORMAT_EXTS.get(extension, "PNG"), original_content_type
    pillow_format = _V040_THUMB_FORMATS.get(original_content_type)
    if pillow_format is None:
        return "PNG", "image/png"
    return pillow_format, original_content_type


# ---------------------------------------------------------------------------
# ``.attrs`` side-cars
# ---------------------------------------------------------------------------

_XATTRS_STRING_FIELDS = (
    "user.cache_control",
    "user.content_disposition",
    "user.content_encoding",
    "user.content_language",
    "user.content_type",
)
_XATTRS_FIELDS = (*_XATTRS_STRING_FIELDS, "user.metadata", "md5")
# The ``xattrs`` field names (all ASCII) by their Go v1 folded form.
_XATTRS_FOLDED = {name.upper(): name for name in _XATTRS_FIELDS}
_JSON_WHITESPACE_RE = re.compile(r"[ \t\n\r]*")


class _Pairs(list):
    """The members of one JSON object, in order, duplicates included."""


class _IntLiteral:
    """An integer JSON literal, kept as written (for a v1 ``[]byte`` array)."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


class _FloatLiteral:
    __slots__ = ()


def _reject_constant(name: str) -> Any:
    # Python accepts NaN/Infinity; Go's decoders do not.
    raise ValueError(f"invalid JSON literal {name}")


def _strict_pairs(pairs: list[tuple[str, Any]]) -> _Pairs:
    names = set()
    for name, _value in pairs:
        if name in names:
            raise ValueError("duplicate object member name")
        names.add(name)
    return _Pairs(pairs)


_V1_DECODER = json.JSONDecoder(
    object_pairs_hook=_Pairs,
    parse_int=_IntLiteral,
    parse_float=lambda _text: _FloatLiteral(),
    parse_constant=_reject_constant,
)
_V2_DECODER = json.JSONDecoder(
    object_pairs_hook=_strict_pairs,
    parse_int=_IntLiteral,
    parse_float=lambda _text: _FloatLiteral(),
    parse_constant=_reject_constant,
)


def _go_replace_errors(error: UnicodeError) -> tuple[str, int]:
    """One U+FFFD per invalid byte, like Go's ``utf8.DecodeRune``."""
    if not isinstance(error, UnicodeDecodeError):  # pragma: no cover
        raise error
    return "�", error.start + 1


codecs.register_error("ppbase-go-replace", _go_replace_errors)


def parse_attrs(payload: bytes, *, compat: str = "") -> FileAttributes:
    """Decode a ``fileblob`` side-car like the PocketBase of ``compat``.

    v0.40.6 decodes it with ``encoding/json/v2`` ``UnmarshalRead``
    (``internal/fileblob/attrs.go`` l.4, l.80): the whole input must be one
    JSON value, duplicate member names, invalid UTF-8 and invalid escaped
    surrogates are errors, and member names match case-sensitively.
    v0.22.34 uses gocloud v0.40.0 ``fileblob``, whose ``json.Decoder``
    (``encoding/json`` v1) reads the first value only, keeps the last of
    duplicate members, matches member names case-insensitively and replaces
    invalid UTF-8 and invalid surrogates with U+FFFD.  A side-car that cannot
    be decoded makes PocketBase answer 404 for the file.
    """
    v1 = compat == POCKETBASE_022
    try:
        if v1:
            text = payload.decode("utf-8", "ppbase-go-replace")
            start = _JSON_WHITESPACE_RE.match(text).end()
            decoded, _end = _V1_DECODER.raw_decode(text, start)
        else:
            text = payload.decode("utf-8")
            decoded = _V2_DECODER.decode(text)
            _reject_surrogates(decoded)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InvalidFileAttributes("The .attrs side-car cannot be decoded.") from exc
    if decoded is None:
        # A JSON null decodes into the zero struct.
        return FileAttributes(content_type="")
    if not isinstance(decoded, _Pairs):
        raise InvalidFileAttributes("The .attrs side-car is not a JSON object.")
    values: dict[str, Any] = {}
    try:
        for name, value in decoded:
            target = name if name in _XATTRS_FIELDS else None
            if target is None and v1:
                target = _XATTRS_FOLDED.get(_go_fold(name))
            if target is None:
                continue  # unknown members are ignored by both decoders
            _store_member(values, target, value, v1=v1)
    except InvalidFileAttributes:
        raise
    except ValueError as exc:
        # Any conversion failure is a decoding error for Go too (404).
        raise InvalidFileAttributes("The .attrs side-car cannot be decoded.") from exc
    metadata = values.get("user.metadata") or {}
    return FileAttributes(
        content_type=values.get("user.content_type", ""), metadata=dict(metadata)
    )


def _go_fold(name: str) -> str:
    """Go v1 ``foldName`` for a comparison with an ASCII field name.

    ASCII letters fold to upper case; U+017F and U+212A, whose simple case
    folding orbits contain ``s`` and ``k``, fold to ``S`` and ``K``.  Any
    other non-ASCII character folds to a non-ASCII rune (Python's full case
    mapping, ``"ı".upper() == "I"``, must not be used), so such a name
    cannot match.
    """
    folded = name.translate(_GO_FOLD_TABLE)
    return folded.upper() if folded.isascii() else folded


_GO_FOLD_TABLE = {0x017F: "S", 0x212A: "K"}


def _store_member(values: dict[str, Any], target: str, value: Any, *, v1: bool) -> None:
    if target in _XATTRS_STRING_FIELDS:
        if value is None:
            if not v1:
                values[target] = ""  # v2: null stores the zero value
            return  # v1: null leaves a string unchanged
        if type(value) is not str:
            raise InvalidFileAttributes(f"Invalid {target} in the .attrs side-car.")
        values[target] = _go_v1_string(value) if v1 else value
    elif target == "user.metadata":
        if value is None:
            values[target] = None
            return
        if not isinstance(value, _Pairs):
            raise InvalidFileAttributes("Invalid user.metadata in the .attrs side-car.")
        # v1 decodes into the map left by a previous duplicate member.
        metadata = dict(values.get(target) or {}) if v1 else {}
        for key, item in value:
            if item is None:
                item = ""
            elif type(item) is not str:
                raise InvalidFileAttributes(
                    "Invalid user.metadata in the .attrs side-car."
                )
            if v1:
                key, item = _go_v1_string(key), _go_v1_string(item)
            metadata[key] = item
        values[target] = metadata
    else:  # md5 ([]byte)
        _check_md5(value, v1=v1)


def _check_md5(value: Any, *, v1: bool) -> None:
    if value is None:
        return
    if type(value) is str:
        encoded = value.replace("\r", "").replace("\n", "") if v1 else value
        try:
            base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise InvalidFileAttributes("Invalid md5 in the .attrs side-car.") from exc
        return
    if v1 and type(value) is list:
        # encoding/json v1 also decodes a []byte from an array of numbers,
        # each parsed as a uint8 (``strconv.ParseUint``, a range error
        # otherwise).  A JSON integer has no leading zero: more than three
        # digits is out of range, and never reaches int() (which refuses
        # more than 4300 digits).
        for item in value:
            if item is None:
                continue
            if not (
                isinstance(item, _IntLiteral)
                and item.text.isascii()
                and item.text.isdigit()
                and len(item.text) <= 3
                and int(item.text) <= 0xFF
            ):
                raise InvalidFileAttributes("Invalid md5 in the .attrs side-car.")
        return
    raise InvalidFileAttributes("Invalid md5 in the .attrs side-car.")


def _go_v1_string(value: str) -> str:
    """An escaped lone surrogate decodes to U+FFFD in Go's v1 ``unquote``."""
    return _SURROGATE_RE.sub("�", value)


def _reject_surrogates(decoded: Any) -> None:
    """json/v2 refuses escapes that do not decode to a valid code point."""
    stack = [decoded]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SURROGATE_RE.search(item):
                raise ValueError("invalid escaped surrogate")
        elif isinstance(item, _Pairs):
            for name, value in item:
                stack.append(name)
                stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)


def encode_attrs(
    content: bytes,
    content_type: str,
    metadata: dict[str, str] | None = None,
    *,
    compat: str = "",
) -> bytes:
    """Encode the side-car ``fileblob`` writes next to a new blob.

    Same members and order as the ``xattrs`` struct (v0.40.6
    ``internal/fileblob/attrs.go`` l.35-43), compact, with the MD5 of the
    content in base64 like Go's ``[]byte`` JSON encoding.  v0.22.34 (gocloud
    v0.40.0 ``fileblob``, ``json.Encoder.Encode``) writes ``null`` for no
    metadata, escapes HTML characters and ends with a newline.  v0.40.6
    (``json.MarshalWrite``, l.53, json/v2 defaults) writes ``{}``, escapes
    no HTML character and adds no newline.  Both decoders read either form.
    """
    v1 = compat == POCKETBASE_022
    payload = {
        "user.cache_control": "",
        "user.content_disposition": "",
        "user.content_encoding": "",
        "user.content_language": "",
        "user.content_type": content_type,
        "user.metadata": (metadata or None) if v1 else dict(metadata or {}),
        "md5": base64.b64encode(
            hashlib.md5(content, usedforsecurity=False).digest()
        ).decode("ascii"),
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if v1:
        for char, escaped in _GO_JSON_HTML_ESCAPES:
            encoded = encoded.replace(char, escaped)
        encoded += "\n"
    return encoded.encode("utf-8")


# ``json.Encoder`` escapes these by default (``SetEscapeHTML(true)``).
_GO_JSON_HTML_ESCAPES = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    (" ", "\\u2028"),
    (" ", "\\u2029"),
)
