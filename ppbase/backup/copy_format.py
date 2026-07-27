"""Streaming, compressed COPY segment container for native logical backups.

This module is the security-critical core of the native (asyncpg-only) backup
engine that replaces ``pg_dump``/``pg_restore``.  It defines ``data.copy``: an
opaque byte file that concatenates one independently-compressed segment per
exported table.  Segments are never located by scanning for delimiters inside
data; every segment is addressed by an explicit ``offset``/``compressed_size``
recorded in ``schema.json`` (see :class:`CopySegment`).

Design invariants (see the project backup spec):

* COPY uses PostgreSQL ``FORMAT text`` only.  The wire bytes are treated as
  opaque here; PostgreSQL owns escaping, this module owns framing.
* Memory is bounded: export and import stream fixed-size chunks; no table and
  no segment is ever materialised whole in memory.
* Every segment carries the SHA-256 and byte length of both its compressed and
  its decompressed stream, plus the COPY row count.
* Decompression is bomb-guarded: a per-segment ratio cap, the recorded
  decompressed size, and a global decompressed-byte budget all bound output.
* Segment layout is validated to be gap-free, non-overlapping and to cover the
  data file exactly before any restore reads it.

Phase 1 provides the container writer/reader plus the COPY export.  The
destructive restore that consumes segments lives in later phases; the reader
here is what that restore (and integrity tests) build on.
"""

from __future__ import annotations

import hashlib
import re
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

from ppbase.backup.models import BackupManifestError


# ---------------------------------------------------------------------------
# COPY session settings
# ---------------------------------------------------------------------------

# Applied with ``SET LOCAL`` on the export transaction so the text COPY output
# is portable across PostgreSQL versions, architectures and server locales.
# extra_float_digits=3 forces shortest round-trippable float text; DateStyle,
# TimeZone and IntervalStyle pin timestamp/interval rendering; UTF8 pins the
# byte encoding.  standard_conforming_strings pins backslash handling.
COPY_SESSION_SETTINGS: tuple[tuple[str, str], ...] = (
    ("client_encoding", "UTF8"),
    ("DateStyle", "ISO, MDY"),
    ("TimeZone", "UTC"),
    ("IntervalStyle", "postgres"),
    ("extra_float_digits", "3"),
    ("standard_conforming_strings", "on"),
)

# Explicit COPY text framing markers.  Kept explicit (rather than relying on
# server defaults) so export and import always agree byte for byte.
COPY_DELIMITER = "\t"
COPY_NULL_MARKER = "\\N"
COPY_FORMAT = "text"

COMPRESSION_ZLIB = "zlib"
_SUPPORTED_COMPRESSION = frozenset({COMPRESSION_ZLIB})

# Streaming chunk size for compression / decompression / hashing.
_CHUNK_SIZE = 1 << 20  # 1 MiB
_ZLIB_LEVEL = 6

# Default decompression-bomb guards.  A single segment may not inflate beyond
# ``_DEFAULT_MAX_RATIO`` times its stored size, and the whole archive may not
# inflate beyond ``_DEFAULT_MAX_TOTAL`` bytes.  Both are overridable per call.
_DEFAULT_MAX_RATIO = 200
_DEFAULT_MAX_TOTAL = 64 * (1 << 30)  # 64 GiB

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# PostgreSQL unquoted identifier grammar that PPBase itself emits.  Segment
# table/column names must round-trip as physical identifiers; anything else is
# rejected rather than quoted, because the backup only ever describes objects
# PPBase created.
_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_IDENTIFIER_MAX_BYTES = 63


class CopyFormatError(BackupManifestError):
    """Raised when a COPY segment or the data.copy layout is malformed."""


def _validate_identifier(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise CopyFormatError(f"{label} is not a valid unquoted identifier")
    if len(value.encode("utf-8")) > _IDENTIFIER_MAX_BYTES:
        raise CopyFormatError(f"{label} exceeds {_IDENTIFIER_MAX_BYTES} bytes")
    return value


def _validate_non_negative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CopyFormatError(f"{label} must be a non-negative integer")
    return value


def _validate_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise CopyFormatError(f"{label} must be 64 lowercase hex characters")
    return value


# ---------------------------------------------------------------------------
# Segment metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CopySegment:
    """Addressable, integrity-checked description of one table's COPY stream.

    ``offset`` and ``compressed_size`` locate the segment inside ``data.copy``.
    The two hash/size pairs let a restore verify the compressed bytes before
    decompressing and the decompressed bytes before importing.  ``row_count``
    is compared against the ``COPY n`` status PostgreSQL returns on import.
    """

    table: str
    columns: tuple[str, ...]
    offset: int
    compressed_size: int
    compressed_sha256: str
    uncompressed_size: int
    uncompressed_sha256: str
    row_count: int
    compression: str = COMPRESSION_ZLIB

    def __post_init__(self) -> None:
        object.__setattr__(self, "table", _validate_identifier(self.table, label="segment table"))
        columns = tuple(self.columns)
        if not columns:
            raise CopyFormatError("segment must list at least one column")
        seen: set[str] = set()
        for column in columns:
            name = _validate_identifier(column, label="segment column")
            if name in seen:
                raise CopyFormatError(f"duplicate segment column: {name}")
            seen.add(name)
        object.__setattr__(self, "columns", columns)
        object.__setattr__(
            self, "offset", _validate_non_negative_int(self.offset, label="segment offset")
        )
        object.__setattr__(
            self,
            "compressed_size",
            _validate_non_negative_int(self.compressed_size, label="compressed_size"),
        )
        object.__setattr__(
            self,
            "uncompressed_size",
            _validate_non_negative_int(self.uncompressed_size, label="uncompressed_size"),
        )
        object.__setattr__(
            self, "row_count", _validate_non_negative_int(self.row_count, label="row_count")
        )
        object.__setattr__(
            self,
            "compressed_sha256",
            _validate_sha256(self.compressed_sha256, label="compressed_sha256"),
        )
        object.__setattr__(
            self,
            "uncompressed_sha256",
            _validate_sha256(self.uncompressed_sha256, label="uncompressed_sha256"),
        )
        if self.compression not in _SUPPORTED_COMPRESSION:
            raise CopyFormatError(f"unsupported segment compression: {self.compression!r}")

    @property
    def end_offset(self) -> int:
        return self.offset + self.compressed_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "columns": list(self.columns),
            "offset": self.offset,
            "compressed_size": self.compressed_size,
            "compressed_sha256": self.compressed_sha256,
            "uncompressed_size": self.uncompressed_size,
            "uncompressed_sha256": self.uncompressed_sha256,
            "row_count": self.row_count,
            "compression": self.compression,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CopySegment":
        expected = {
            "table",
            "columns",
            "offset",
            "compressed_size",
            "compressed_sha256",
            "uncompressed_size",
            "uncompressed_sha256",
            "row_count",
            "compression",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise CopyFormatError("segment has unknown or missing fields")
        columns = value["columns"]
        if not isinstance(columns, list):
            raise CopyFormatError("segment columns must be an array")
        return cls(
            table=value["table"],
            columns=tuple(columns),
            offset=value["offset"],
            compressed_size=value["compressed_size"],
            compressed_sha256=value["compressed_sha256"],
            uncompressed_size=value["uncompressed_size"],
            uncompressed_sha256=value["uncompressed_sha256"],
            row_count=value["row_count"],
            compression=value["compression"],
        )


def validate_segment_layout(
    segments: Iterable[CopySegment],
    *,
    total_size: int,
    max_total_uncompressed: int = _DEFAULT_MAX_TOTAL,
    max_ratio: int = _DEFAULT_MAX_RATIO,
) -> tuple[CopySegment, ...]:
    """Prove segments tile ``data.copy`` exactly with no gaps or overlaps.

    Raises :class:`CopyFormatError` on any gap, overlap, out-of-bounds offset,
    misordering, or aggregate/ratio budget violation.  Returns the segments as
    an ordered tuple when the layout is sound.
    """
    ordered = tuple(segments)
    cursor = 0
    total_uncompressed = 0
    for segment in ordered:
        if segment.offset != cursor:
            raise CopyFormatError(
                f"segment for {segment.table!r} is not contiguous: "
                f"expected offset {cursor}, found {segment.offset}"
            )
        end = segment.end_offset
        if end < segment.offset:  # pragma: no cover - overflow guard
            raise CopyFormatError("segment length overflow")
        if end > total_size:
            raise CopyFormatError(
                f"segment for {segment.table!r} runs past data.copy size"
            )
        _check_ratio(segment, max_ratio=max_ratio)
        total_uncompressed += segment.uncompressed_size
        if total_uncompressed > max_total_uncompressed:
            raise CopyFormatError("archive exceeds the decompressed-byte budget")
        cursor = end
    if cursor != total_size:
        raise CopyFormatError(
            f"segments cover {cursor} bytes but data.copy is {total_size} bytes"
        )
    return ordered


def _check_ratio(segment: CopySegment, *, max_ratio: int) -> None:
    if segment.compressed_size == 0:
        if segment.uncompressed_size != 0:
            raise CopyFormatError(
                f"empty compressed segment for {segment.table!r} claims data"
            )
        return
    if segment.uncompressed_size > segment.compressed_size * max_ratio:
        raise CopyFormatError(
            f"segment for {segment.table!r} exceeds the {max_ratio}x "
            "decompression ratio cap"
        )


# ---------------------------------------------------------------------------
# Writing segments (streaming compression)
# ---------------------------------------------------------------------------


class _ByteSink(Protocol):
    def write(self, data: bytes, /) -> Any: ...
    def tell(self) -> int: ...


class SegmentWriter:
    """Append independently-compressed segments to a ``data.copy`` sink.

    The sink is any binary file-like object opened for writing.  Compression is
    streamed chunk by chunk; only :data:`_CHUNK_SIZE`-bounded buffers exist at
    any time.  Each finished segment returns a :class:`CopySegment` describing
    exactly where it landed and how to verify it.
    """

    def __init__(self, sink: _ByteSink) -> None:
        self._sink = sink
        self._base = sink.tell()
        self._written = 0
        self._closed = False
        self._open: "_OpenSegment | None" = None

    @property
    def total_size(self) -> int:
        """Compressed bytes written by this writer so far."""
        return self._written

    def _emit(self, data: bytes) -> None:
        if not data:
            return
        # ``write`` on a raw (unbuffered) file may consume fewer bytes than
        # offered and report the short count; loop until every byte lands so a
        # partial write can never silently truncate a segment.
        view = memoryview(data)
        total = len(view)
        offset = 0
        while offset < total:
            written = self._sink.write(view[offset:])
            if written is None:
                # Buffered/text sinks return None but always accept everything.
                offset = total
                break
            if written == 0:
                raise CopyFormatError("segment sink accepted no bytes")
            offset += written
        self._written += total

    def begin_segment(self, *, table: str, columns: tuple[str, ...]) -> "_OpenSegment":
        """Start an incremental segment; feed raw COPY bytes then finish it.

        Feeding keeps memory bounded: bytes are compressed and flushed to the
        sink as they arrive, so neither the plain nor the compressed table is
        ever held whole.  Only one segment may be open at a time.
        """
        if self._closed:
            raise CopyFormatError("SegmentWriter is closed")
        if self._open is not None:
            raise CopyFormatError("a segment is already open")
        # Validate identifiers eagerly so a bad name fails before any bytes land.
        _validate_identifier(table, label="segment table")
        segment = _OpenSegment(self, table=table, columns=tuple(columns))
        self._open = segment
        return segment

    def write_segment(
        self,
        *,
        table: str,
        columns: tuple[str, ...],
        chunks: Iterable[bytes],
        row_count: int,
    ) -> CopySegment:
        """Compress and append one table's raw COPY-text ``chunks`` at once."""
        segment = self.begin_segment(table=table, columns=columns)
        for chunk in chunks:
            segment.feed(chunk)
        return segment.finish(row_count=row_count)


class _OpenSegment:
    """A segment being streamed into a :class:`SegmentWriter`."""

    __slots__ = (
        "_writer",
        "_table",
        "_columns",
        "_offset",
        "_compressor",
        "_raw_hash",
        "_comp_hash",
        "_uncompressed_size",
        "_compressed_size",
        "_done",
    )

    def __init__(self, writer: "SegmentWriter", *, table: str, columns: tuple[str, ...]) -> None:
        self._writer = writer
        self._table = table
        self._columns = columns
        self._offset = writer._written
        self._compressor = zlib.compressobj(_ZLIB_LEVEL)
        self._raw_hash = hashlib.sha256()
        self._comp_hash = hashlib.sha256()
        self._uncompressed_size = 0
        self._compressed_size = 0
        self._done = False

    def feed(self, data: bytes) -> None:
        if self._done:
            raise CopyFormatError("segment is already finished")
        if not data:
            return
        payload = bytes(data)
        self._raw_hash.update(payload)
        self._uncompressed_size += len(payload)
        compressed = self._compressor.compress(payload)
        if compressed:
            self._comp_hash.update(compressed)
            self._compressed_size += len(compressed)
            self._writer._emit(compressed)

    def finish(self, *, row_count: int) -> CopySegment:
        if self._done:
            raise CopyFormatError("segment is already finished")
        self._done = True
        tail = self._compressor.flush()
        if tail:
            self._comp_hash.update(tail)
            self._compressed_size += len(tail)
            self._writer._emit(tail)
        self._writer._open = None
        return CopySegment(
            table=self._table,
            columns=self._columns,
            offset=self._offset,
            compressed_size=self._compressed_size,
            compressed_sha256=self._comp_hash.hexdigest(),
            uncompressed_size=self._uncompressed_size,
            uncompressed_sha256=self._raw_hash.hexdigest(),
            row_count=row_count,
            compression=COMPRESSION_ZLIB,
        )


# ---------------------------------------------------------------------------
# Reading segments (bomb-guarded streaming decompression)
# ---------------------------------------------------------------------------


class SegmentReader:
    """Read and verify one segment at a time from a pinned ``data.copy``.

    The file object must be a real seekable binary file (ideally an anonymous
    pinned descriptor).  Reads are bounded to the segment's recorded byte
    range; decompression output is bounded by the recorded uncompressed size
    and a ratio cap, so a hostile archive cannot exhaust memory.
    """

    def __init__(
        self,
        fileobj: BinaryIO,
        *,
        total_size: int,
        max_ratio: int = _DEFAULT_MAX_RATIO,
    ) -> None:
        self._file = fileobj
        self._total_size = _validate_non_negative_int(total_size, label="total_size")
        self._max_ratio = max_ratio

    def iter_uncompressed(self, segment: CopySegment) -> Iterator[bytes]:
        """Yield verified decompressed chunks of ``segment``.

        Both the compressed and decompressed SHA-256 and byte counts are
        checked; a mismatch, an overrun, or trailing compressed bytes raise
        :class:`CopyFormatError`.  The full segment is validated by the time the
        iterator is exhausted.
        """
        if segment.end_offset > self._total_size:
            raise CopyFormatError("segment runs past the pinned data.copy size")
        _check_ratio(segment, max_ratio=self._max_ratio)
        if segment.compression != COMPRESSION_ZLIB:
            raise CopyFormatError(f"unsupported compression: {segment.compression!r}")

        comp_hash = hashlib.sha256()
        raw_hash = hashlib.sha256()
        decompressor = zlib.decompressobj()
        remaining_compressed = segment.compressed_size
        produced = 0
        self._file.seek(segment.offset)

        try:
            while remaining_compressed > 0:
                want = min(_CHUNK_SIZE, remaining_compressed)
                compressed_chunk = self._file.read(want)
                if len(compressed_chunk) != want:
                    raise CopyFormatError("data.copy ended inside a segment")
                remaining_compressed -= want
                comp_hash.update(compressed_chunk)
                # Bound decompressor output per call so a bomb cannot inflate
                # past the recorded size within a single read.  The +1 lets an
                # overrun be detected instead of silently truncated.
                pending: bytes | None = compressed_chunk
                while pending is not None:
                    budget = segment.uncompressed_size - produced + 1
                    plain = decompressor.decompress(pending, budget)
                    produced += len(plain)
                    if produced > segment.uncompressed_size:
                        raise CopyFormatError("segment inflates past its recorded size")
                    if plain:
                        raw_hash.update(plain)
                        yield plain
                    pending = (
                        decompressor.unconsumed_tail
                        if decompressor.unconsumed_tail
                        else None
                    )

            tail = decompressor.flush()
        except zlib.error as exc:
            raise CopyFormatError("segment is not valid zlib data") from exc
        produced += len(tail)
        if produced != segment.uncompressed_size:
            raise CopyFormatError("segment decompressed size mismatch")
        if tail:
            raw_hash.update(tail)
            yield tail
        if not decompressor.eof:
            raise CopyFormatError("segment has trailing compressed bytes")
        if decompressor.unused_data:
            # The zlib stream terminated before the recorded compressed_size:
            # bytes past the stream end are unaccounted for and rejected.
            raise CopyFormatError("segment has unused bytes after the zlib stream")
        if comp_hash.hexdigest() != segment.compressed_sha256:
            raise CopyFormatError("segment compressed hash mismatch")
        if raw_hash.hexdigest() != segment.uncompressed_sha256:
            raise CopyFormatError("segment decompressed hash mismatch")


# ---------------------------------------------------------------------------
# COPY export (asyncpg)
# ---------------------------------------------------------------------------


def parse_copy_row_count(status: Any) -> int:
    """Parse the ``COPY n`` command tag asyncpg returns from a COPY."""
    if not isinstance(status, str):
        raise CopyFormatError("COPY status is not a string")
    parts = status.split()
    if len(parts) != 2 or parts[0] != "COPY" or not parts[1].isdigit():
        raise CopyFormatError(f"unexpected COPY status: {status!r}")
    return int(parts[1])


async def apply_copy_session_settings(pg_conn: Any) -> None:
    """Pin the export session GUCs with ``SET LOCAL`` inside the txn."""
    for name, value in COPY_SESSION_SETTINGS:
        # name is a fixed literal from COPY_SESSION_SETTINGS; value is passed as
        # a quoted literal via set_config to avoid any interpolation.
        await pg_conn.execute(
            "SELECT set_config($1, $2, true)",
            name,
            value,
        )


async def export_table_segment(
    pg_conn: Any,
    writer: SegmentWriter,
    *,
    table: str,
    columns: tuple[str, ...],
    schema: str = "public",
) -> CopySegment:
    """Stream ``schema.table`` (given column order) into ``writer``.

    Uses ``COPY (SELECT ... FROM schema.table) TO STDOUT`` in text format on the
    supplied asyncpg connection, which must already be inside the export
    transaction with :func:`apply_copy_session_settings` applied.  The raw COPY
    bytes are handed to the writer chunk by chunk; nothing is buffered whole.
    """
    _validate_identifier(schema, label="schema")
    _validate_identifier(table, label="table")
    if not columns:
        raise CopyFormatError("export requires an explicit column list")
    column_sql = ", ".join(f'"{_validate_identifier(c, label="column")}"' for c in columns)
    query = f'SELECT {column_sql} FROM "{schema}"."{table}"'

    segment = writer.begin_segment(table=table, columns=columns)

    # asyncpg invokes this callback with raw COPY-stream bytes; feeding them
    # straight into the segment keeps memory bounded to one compressor chunk.
    async def _sink(data: bytes) -> None:
        segment.feed(data)

    status = await pg_conn.copy_from_query(
        query,
        output=_sink,
        format=COPY_FORMAT,
        delimiter=COPY_DELIMITER,
        null=COPY_NULL_MARKER,
    )
    row_count = parse_copy_row_count(status)
    return segment.finish(row_count=row_count)


async def import_table_segment(
    pg_conn: Any,
    reader: SegmentReader,
    segment: CopySegment,
    *,
    schema: str = "public",
) -> None:
    """Verify and stream ``segment`` back into ``schema.table`` via COPY.

    Decompression and integrity checks run inside :meth:`SegmentReader.
    iter_uncompressed`.  The recorded ``row_count`` is checked against the
    ``COPY n`` status PostgreSQL returns.
    """
    _validate_identifier(schema, label="schema")

    async def _source() -> Any:
        # asyncpg consumes an async iterable of byte chunks.  Verification and
        # bomb-guarded decompression happen lazily inside iter_uncompressed as
        # each chunk is pulled, so memory stays bounded.
        for chunk in reader.iter_uncompressed(segment):
            yield chunk

    status = await pg_conn.copy_to_table(
        segment.table,
        source=_source(),
        columns=list(segment.columns),
        schema_name=schema,
        format=COPY_FORMAT,
        delimiter=COPY_DELIMITER,
        null=COPY_NULL_MARKER,
    )
    imported = parse_copy_row_count(status)
    if imported != segment.row_count:
        raise CopyFormatError(
            f"imported {imported} rows for {segment.table!r} but segment "
            f"recorded {segment.row_count}"
        )


__all__ = [
    "COMPRESSION_ZLIB",
    "COPY_DELIMITER",
    "COPY_FORMAT",
    "COPY_NULL_MARKER",
    "COPY_SESSION_SETTINGS",
    "CopyFormatError",
    "CopySegment",
    "SegmentReader",
    "SegmentWriter",
    "apply_copy_session_settings",
    "export_table_segment",
    "import_table_segment",
    "parse_copy_row_count",
    "validate_segment_layout",
]
