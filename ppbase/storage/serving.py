"""PocketBase file-serving rules shared by ``GET/HEAD /api/files/...``.

Only the rules that PocketBase v0.22.34 and the current release (v0.40.6)
apply identically are implemented here; each constant cites both sources.
The differences between the two versions are listed in
``docs/file-serving.md`` and are deliberately not applied.

PocketBase serves a stored file with ``tools/filesystem`` ``Serve``
(v0.22.34 ``filesystem.go`` l.393-430, v0.40.6 l.562-599):

- the real content type is the one recorded in the blob attributes, which a
  local (``fileblob``) storage keeps in a JSON ``<file>.attrs`` side-car
  (gocloud ``fileblob``; vendored copy v0.40.6
  ``tools/filesystem/internal/fileblob/attrs.go`` l.28-87);
- ``Content-Disposition`` is ``inline`` for a short list of safe types and
  ``attachment`` otherwise or when ``?download`` parses as a Go boolean true;
- ``.svg`` and ``.css`` force their content type;
- a sandboxing ``Content-Security-Policy`` and a 30-day ``Cache-Control``
  are always sent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

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

#: v0.22.34 ``filesystem.go`` l.380-383.  v0.40.6 l.534-549 adds ``.js``,
#: ``.mjs``, ``.xlsx``, ``.docx`` and ``.pptx``: not applied (0.40 only).
#: The extension is matched case-sensitively, like Go's ``filepath.Ext``.
MANUAL_EXTENSION_CONTENT_TYPES = {
    ".svg": "image/svg+xml",
    ".css": "text/css",
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
#: deletes it (v0.22.34 ``apis/file.go`` l.206, v0.40.6 l.205).  v0.40.6
#: also sends ``Cross-Origin-Opener-Policy`` (l.296): not applied.
FILE_SECURITY_HEADERS = {
    "X-XSS-Protection": "1; mode=block",
    "X-Content-Type-Options": "nosniff",
}

#: v0.22.34 ``apis/file.go`` l.27 == v0.40.6 ``apis/file.go`` l.23.
DEFAULT_THUMB_SIZES = ("100x100",)

#: v0.22.34 ``apis/file.go`` l.26 == v0.40.6 l.22 for these four types.
#: v0.40.6 adds ``image/webp`` (0.22.34 serves a webp original instead of a
#: thumbnail); PPBase already generated webp thumbnails before this module
#: and keeps doing so, see ``docs/file-serving.md``.
THUMB_IMAGE_CONTENT_TYPES = frozenset(
    {"image/png", "image/jpg", "image/jpeg", "image/gif", "image/webp"}
)

#: The query parameter forcing an attachment (v0.22.34 ``filesystem.go``
#: l.387, v0.40.6 l.553).
FORCE_ATTACHMENT_PARAM = "download"

#: Content type of a blob without ``.attrs`` side-car in ``fileblob``
#: (v0.40.6 ``internal/fileblob/attrs.go`` l.69-74).  PPBase keeps its own
#: extension-based detection for files it uploaded itself (they have no
#: side-car); see ``docs/file-serving.md``.
FILEBLOB_DEFAULT_CONTENT_TYPE = "application/octet-stream"

ATTRS_SUFFIX = ".attrs"

#: An RFC 9110 ``token`` (tchar+).  Every name PocketBase or PPBase generates
#: for a stored file or a thumbnail is a token.
_HTTP_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

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
    errors in Go, hence false (v0.22.34 ``filesystem.go`` l.400-403).
    """
    if not raw:
        return False
    return go_parse_bool(raw) is True


def disposition_type(real_content_type: str, *, forced: bool) -> str:
    """``inline`` for the safe types, ``attachment`` otherwise.

    The check uses the stored (real) content type, before the extension
    overrides (v0.22.34 ``filesystem.go`` l.405-409).
    """
    if not forced and real_content_type in INLINE_SERVE_CONTENT_TYPES:
        return "inline"
    return "attachment"


def response_content_type(real_content_type: str, served_name: str) -> str:
    """Apply the ``.svg``/``.css`` overrides of ``Serve``.

    v0.22.34 takes the extension of the served name (l.414); v0.40.6 the one
    of the served key (l.583): both end with the same file name unless a
    download hook renames one of them.
    """
    ext = _go_ext(served_name)
    return MANUAL_EXTENSION_CONTENT_TYPES.get(ext, real_content_type)


def content_disposition(disposition: str, served_name: str) -> str:
    """Build ``<disposition>; filename=<name>``.

    For a token name (every generated name) this is byte-for-byte the
    v0.22.34 header (``filesystem.go`` l.418).  Any other name, which only a
    download hook can produce, is quoted like v0.40.6 does for every name
    (``strconv.Quote``, l.587), so that it can never split the header.
    """
    if _HTTP_TOKEN_RE.fullmatch(served_name):
        return f"{disposition}; filename={served_name}"
    return f"{disposition}; filename={go_quote(served_name)}"


def latin1_header_value(value: str) -> str:
    """Return ``value`` so that Starlette writes its UTF-8 bytes unchanged.

    Starlette encodes header values as Latin-1; Go writes the UTF-8 bytes
    of a header as is.  Mapping each UTF-8 byte to one Latin-1 code point
    sends the same octets on the wire.
    """
    return value.encode("utf-8", "surrogateescape").decode("latin-1")


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


def thumb_names(thumb_size: str, filename: str) -> tuple[str, str]:
    """Return ``(directory, file)`` of a thumbnail (v0.22.34 l.174-175)."""
    served_name = f"{thumb_size}_{filename}"
    return f"thumbs_{filename}", served_name


def parse_attrs(payload: bytes) -> FileAttributes:
    """Decode a ``fileblob`` side-car like Go's ``json.Decoder`` would.

    A side-car that is not a JSON object, or whose known keys have the wrong
    type, fails in ``fileblob`` and PocketBase answers 404 for the file.
    """
    try:
        text = payload.decode("utf-8")
        # Like ``json.Decoder.Decode``: the first JSON value, after optional
        # leading whitespace; anything after it is never read.
        decoded, _end = _JSON_DECODER.raw_decode(text, len(text) - len(text.lstrip()))
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidFileAttributes("The .attrs side-car is not valid JSON.") from exc
    if decoded is None:
        # Go decodes ``null`` into the zero struct.
        return FileAttributes(content_type="")
    if not isinstance(decoded, dict):
        raise InvalidFileAttributes("The .attrs side-car is not a JSON object.")
    content_type = _attr_string(decoded, "user.content_type")
    raw_metadata = decoded.get("user.metadata")
    metadata: dict[str, str] = {}
    if raw_metadata is not None:
        if not isinstance(raw_metadata, dict) or not all(
            isinstance(value, str) for value in raw_metadata.values()
        ):
            raise InvalidFileAttributes("Invalid user.metadata in the .attrs side-car.")
        metadata = dict(raw_metadata)
    for key in (
        "user.cache_control",
        "user.content_disposition",
        "user.content_encoding",
        "user.content_language",
    ):
        _attr_string(decoded, key)
    md5 = decoded.get("md5")
    if md5 is not None and not isinstance(md5, str):
        raise InvalidFileAttributes("Invalid md5 in the .attrs side-car.")
    return FileAttributes(content_type=content_type, metadata=metadata)


def _attr_string(decoded: dict[str, Any], key: str) -> str:
    value = decoded.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InvalidFileAttributes(f"Invalid {key} in the .attrs side-car.")
    return value


def encode_attrs(
    content: bytes,
    content_type: str,
    metadata: dict[str, str] | None = None,
) -> bytes:
    """Encode the side-car ``fileblob`` writes next to a new blob.

    Same keys and order as the ``xattrs`` struct (v0.40.6
    ``internal/fileblob/attrs.go`` l.35-43), the MD5 of the content in
    base64 like Go's ``[]byte`` JSON encoding, compact, newline-terminated
    like ``json.Encoder.Encode`` (gocloud v0.40.0, used by v0.22.34).
    """
    payload = {
        "user.cache_control": "",
        "user.content_disposition": "",
        "user.content_encoding": "",
        "user.content_language": "",
        "user.content_type": content_type,
        "user.metadata": metadata if metadata else None,
        "md5": base64.b64encode(
            hashlib.md5(content, usedforsecurity=False).digest()
        ).decode("ascii"),
    }
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    for char, escaped in _GO_JSON_HTML_ESCAPES:
        encoded = encoded.replace(char, escaped)
    return (encoded + "\n").encode("utf-8")


_JSON_DECODER = json.JSONDecoder()

# ``json.Encoder`` escapes these by default (``SetEscapeHTML(true)``).
_GO_JSON_HTML_ESCAPES = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    (" ", "\\u2028"),
    (" ", "\\u2029"),
)
