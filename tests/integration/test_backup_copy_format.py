"""Unit tests for the native COPY segment container (Phase 1).

These tests exercise the pure-Python container: streaming compression, the
bomb-guarded reader, segment metadata validation and the data.copy layout
checks.  They do not require a PostgreSQL server; the live COPY round-trip is
covered separately.
"""

from __future__ import annotations

import hashlib
import io
import random
import zlib
from dataclasses import replace

import pytest

from ppbase.backup.copy_format import (
    COMPRESSION_ZLIB,
    CopyFormatError,
    CopySegment,
    SegmentReader,
    SegmentWriter,
    parse_copy_row_count,
    validate_segment_layout,
)


def _write(sink: io.BytesIO, payloads: dict[str, bytes]) -> list[CopySegment]:
    writer = SegmentWriter(sink)
    segments: list[CopySegment] = []
    for table, data in payloads.items():
        seg = writer.begin_segment(table=table, columns=("id", "value"))
        # Feed in irregular fragments to prove chunk boundaries are irrelevant.
        step = 7
        for i in range(0, len(data), step):
            seg.feed(data[i : i + step])
        segments.append(seg.finish(row_count=len(data)))
    return segments


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_single_segment() -> None:
    sink = io.BytesIO()
    rng = random.Random(1234)
    rows = [
        f"{i}\trow-with-\ttab-and-\\backslash-{rng.random()}\n".encode()
        for i in range(2000)
    ]
    raw = b"id\tvalue\n" + b"".join(rows)
    [segment] = _write(sink, {"posts": raw})

    reader = SegmentReader(sink, total_size=len(sink.getvalue()))
    recovered = b"".join(reader.iter_uncompressed(segment))
    assert recovered == raw
    assert segment.uncompressed_size == len(raw)
    assert segment.uncompressed_sha256 == hashlib.sha256(raw).hexdigest()


def test_round_trip_multiple_segments_layout_is_exact() -> None:
    sink = io.BytesIO()
    payloads = {
        "alpha": b"\x00\x01\x02" * 50,
        "bravo": b"",  # empty table
        "charlie": bytes(range(256)) * 40,
    }
    segments = _write(sink, payloads)
    total = len(sink.getvalue())

    ordered = validate_segment_layout(segments, total_size=total)
    assert [s.table for s in ordered] == ["alpha", "bravo", "charlie"]

    reader = SegmentReader(sink, total_size=total)
    for segment, (_, raw) in zip(ordered, payloads.items(), strict=True):
        assert b"".join(reader.iter_uncompressed(segment)) == raw


def test_empty_segment_metadata() -> None:
    sink = io.BytesIO()
    [segment] = _write(sink, {"empty": b""})
    assert segment.uncompressed_size == 0
    reader = SegmentReader(sink, total_size=len(sink.getvalue()))
    assert b"".join(reader.iter_uncompressed(segment)) == b""


# ---------------------------------------------------------------------------
# Layout validation
# ---------------------------------------------------------------------------


def _seg(table: str, offset: int, comp: int, unc: int) -> CopySegment:
    payload = b"x" * comp
    return CopySegment(
        table=table,
        columns=("id",),
        offset=offset,
        compressed_size=comp,
        compressed_sha256=hashlib.sha256(payload).hexdigest(),
        uncompressed_size=unc,
        uncompressed_sha256=hashlib.sha256(b"y" * unc).hexdigest(),
        row_count=0,
        compression=COMPRESSION_ZLIB,
    )


def test_layout_rejects_gap() -> None:
    segments = [_seg("a", 0, 10, 10), _seg("b", 11, 10, 10)]
    with pytest.raises(CopyFormatError, match="contiguous"):
        validate_segment_layout(segments, total_size=21)


def test_layout_rejects_overlap() -> None:
    segments = [_seg("a", 0, 10, 10), _seg("b", 9, 10, 10)]
    with pytest.raises(CopyFormatError, match="contiguous"):
        validate_segment_layout(segments, total_size=19)


def test_layout_rejects_running_past_total() -> None:
    segments = [_seg("a", 0, 10, 10)]
    with pytest.raises(CopyFormatError, match="past data.copy"):
        validate_segment_layout(segments, total_size=5)


def test_layout_rejects_incomplete_coverage() -> None:
    segments = [_seg("a", 0, 10, 10)]
    with pytest.raises(CopyFormatError, match="cover"):
        validate_segment_layout(segments, total_size=20)


def test_layout_rejects_total_uncompressed_budget() -> None:
    segments = [_seg("a", 0, 10, 100)]
    with pytest.raises(CopyFormatError, match="decompressed-byte budget"):
        validate_segment_layout(
            segments, total_size=10, max_total_uncompressed=50, max_ratio=1000
        )


def test_layout_rejects_ratio() -> None:
    segments = [_seg("a", 0, 10, 10_000)]
    with pytest.raises(CopyFormatError, match="ratio"):
        validate_segment_layout(segments, total_size=10, max_ratio=100)


# ---------------------------------------------------------------------------
# Integrity / tampering
# ---------------------------------------------------------------------------


def test_reader_detects_compressed_hash_tampering() -> None:
    sink = io.BytesIO()
    [segment] = _write(sink, {"posts": b"hello world" * 100})
    data = bytearray(sink.getvalue())
    data[segment.offset] ^= 0xFF  # flip a byte in the compressed stream
    tampered = io.BytesIO(bytes(data))
    reader = SegmentReader(tampered, total_size=len(data))
    with pytest.raises(CopyFormatError):
        list(reader.iter_uncompressed(segment))


def test_reader_detects_uncompressed_hash_mismatch() -> None:
    sink = io.BytesIO()
    [segment] = _write(sink, {"posts": b"consistent" * 100})
    # Keep valid compressed bytes/hash but claim a different plaintext hash.
    forged = replace(segment, uncompressed_sha256="0" * 64)
    reader = SegmentReader(sink, total_size=len(sink.getvalue()))
    with pytest.raises(CopyFormatError, match="hash mismatch"):
        list(reader.iter_uncompressed(forged))


def test_reader_detects_wrong_uncompressed_size() -> None:
    sink = io.BytesIO()
    [segment] = _write(sink, {"posts": b"data" * 100})
    forged = replace(segment, uncompressed_size=segment.uncompressed_size + 1)
    reader = SegmentReader(sink, total_size=len(sink.getvalue()))
    with pytest.raises(CopyFormatError):
        list(reader.iter_uncompressed(forged))


def test_reader_detects_truncated_data_copy() -> None:
    sink = io.BytesIO()
    [segment] = _write(sink, {"posts": b"payload" * 100})
    full = sink.getvalue()
    truncated = io.BytesIO(full[:-5])
    reader = SegmentReader(truncated, total_size=len(full))
    with pytest.raises(CopyFormatError, match="ended inside"):
        list(reader.iter_uncompressed(segment))


def test_reader_rejects_decompression_bomb() -> None:
    # Craft a segment whose recorded metadata is honest but whose compressed
    # payload inflates far past the declared uncompressed size.
    bomb_plain = b"\x00" * (5 << 20)
    compressed = zlib.compress(bomb_plain, 9)
    sink = io.BytesIO()
    sink.write(compressed)
    honest = CopySegment(
        table="posts",
        columns=("id",),
        offset=0,
        compressed_size=len(compressed),
        compressed_sha256=hashlib.sha256(compressed).hexdigest(),
        # Lie: claim only 10 bytes decompress out of it.
        uncompressed_size=10,
        uncompressed_sha256=hashlib.sha256(b"\x00" * 10).hexdigest(),
        row_count=0,
        compression=COMPRESSION_ZLIB,
    )
    reader = SegmentReader(sink, total_size=len(compressed), max_ratio=10_000)
    with pytest.raises(CopyFormatError, match="inflates past"):
        list(reader.iter_uncompressed(honest))


def test_reader_rejects_unused_bytes_after_zlib_stream() -> None:
    # A valid zlib stream followed by extra bytes (still inside the recorded
    # compressed_size) must be rejected: those bytes are unaccounted for and
    # could smuggle a second hidden payload past the hash if tolerated.
    plain = b"id\tvalue\n" + b"row\tafter\trow\n" * 40
    valid = zlib.compress(plain, 9)
    trailing = b"\x00\x01\x02\x03"
    combined = valid + trailing
    sink = io.BytesIO(combined)
    forged = CopySegment(
        table="posts",
        columns=("id", "value"),
        offset=0,
        compressed_size=len(combined),
        compressed_sha256=hashlib.sha256(combined).hexdigest(),
        uncompressed_size=len(plain),
        uncompressed_sha256=hashlib.sha256(plain).hexdigest(),
        row_count=0,
        compression=COMPRESSION_ZLIB,
    )
    reader = SegmentReader(sink, total_size=len(combined))
    with pytest.raises(CopyFormatError, match="unused bytes after the zlib stream"):
        list(reader.iter_uncompressed(forged))


class _ShortWriteSink:
    """A binary sink whose ``write`` accepts at most ``chunk`` bytes per call.

    Emulates a raw (unbuffered) file that reports short writes, so the writer's
    partial-write loop is actually exercised instead of always seeing a full
    write like ``io.BytesIO`` provides.
    """

    def __init__(self, chunk: int) -> None:
        self._chunk = chunk
        self.buffer = bytearray()

    def tell(self) -> int:
        return len(self.buffer)

    def write(self, data) -> int:
        view = memoryview(data)
        take = min(self._chunk, len(view))
        self.buffer.extend(view[:take])
        return take


def test_writer_handles_partial_writes() -> None:
    # Every byte offered to a short-writing sink must still land intact, and the
    # resulting container must read back byte-for-byte.
    sink = _ShortWriteSink(chunk=3)
    writer = SegmentWriter(sink)
    raw = b"id\tvalue\n" + b"".join(f"{i}\tvalue-{i}\n".encode() for i in range(500))
    seg = writer.begin_segment(table="posts", columns=("id", "value"))
    for i in range(0, len(raw), 11):
        seg.feed(raw[i : i + 11])
    segment = seg.finish(row_count=500)
    assert writer.total_size == len(sink.buffer)

    reader = SegmentReader(io.BytesIO(bytes(sink.buffer)), total_size=len(sink.buffer))
    assert b"".join(reader.iter_uncompressed(segment)) == raw


class _RefusingSink:
    """A sink that always reports zero bytes written."""

    def tell(self) -> int:
        return 0

    def write(self, data) -> int:
        return 0


def test_writer_raises_when_sink_accepts_no_bytes() -> None:
    writer = SegmentWriter(_RefusingSink())
    seg = writer.begin_segment(table="posts", columns=("id", "value"))
    with pytest.raises(CopyFormatError, match="accepted no bytes"):
        # A payload large enough to force a flush past the internal buffer.
        seg.feed(b"x\ty\n" * 100_000)


# ---------------------------------------------------------------------------
# Metadata validation
# ---------------------------------------------------------------------------


def test_segment_rejects_bad_identifier() -> None:
    with pytest.raises(CopyFormatError, match="table"):
        CopySegment(
            table="drop table",
            columns=("id",),
            offset=0,
            compressed_size=0,
            compressed_sha256="0" * 64,
            uncompressed_size=0,
            uncompressed_sha256="0" * 64,
            row_count=0,
        )


def test_segment_rejects_duplicate_columns() -> None:
    with pytest.raises(CopyFormatError, match="duplicate"):
        CopySegment(
            table="posts",
            columns=("id", "id"),
            offset=0,
            compressed_size=0,
            compressed_sha256="0" * 64,
            uncompressed_size=0,
            uncompressed_sha256="0" * 64,
            row_count=0,
        )


def test_segment_dict_round_trip() -> None:
    original = _seg("posts", 0, 10, 10)
    restored = CopySegment.from_dict(original.to_dict())
    assert restored == original


def test_segment_from_dict_rejects_unknown_fields() -> None:
    payload = _seg("posts", 0, 10, 10).to_dict()
    payload["evil"] = 1
    with pytest.raises(CopyFormatError, match="unknown or missing"):
        CopySegment.from_dict(payload)


@pytest.mark.parametrize(
    "status,expected",
    [("COPY 0", 0), ("COPY 12345", 12345)],
)
def test_parse_copy_row_count(status: str, expected: int) -> None:
    assert parse_copy_row_count(status) == expected


@pytest.mark.parametrize("status", ["COPY", "COPY x", "INSERT 0 1", "", "COPY -1"])
def test_parse_copy_row_count_rejects_bad(status: str) -> None:
    with pytest.raises(CopyFormatError):
        parse_copy_row_count(status)
