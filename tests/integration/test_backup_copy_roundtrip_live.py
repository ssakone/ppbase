"""Live COPY round-trip tests against a real PostgreSQL server (Phase 1).

Proves that the export -> compressed segment -> import pipeline preserves
PostgreSQL text-COPY output byte for byte across the full edge-case matrix
(NULL vs empty string, tabs/newlines/backslashes, literal ``\\N``/``\\.``,
Unicode/emoji, nested JSONB, arrays, timestamps, infinity/-infinity, NaN,
negative zero, empty tables).

The test connects with asyncpg directly (the engine uses the raw driver for
COPY).  It is skipped when no server is reachable.
"""

from __future__ import annotations

import io
import os
import uuid

import pytest

asyncpg = pytest.importorskip("asyncpg")

from ppbase.backup.copy_format import (  # noqa: E402
    SegmentReader,
    SegmentWriter,
    apply_copy_session_settings,
    export_table_segment,
    import_table_segment,
)


def _dsn() -> str:
    url = os.environ.get("PPBASE_TEST_DATABASE_URL")
    if url:
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return "postgresql://ppbase:ppbase@localhost:5433/postgres"


async def _connect() -> "asyncpg.Connection":
    try:
        return await asyncpg.connect(_dsn(), timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no reachable PostgreSQL server for live COPY test: {exc}")


async def _dump_query_bytes(conn: "asyncpg.Connection", query: str) -> bytes:
    """Return the raw text-COPY bytes for a query (small test data only)."""
    chunks: list[bytes] = []

    async def _sink(data: bytes) -> None:
        chunks.append(bytes(data))

    await conn.copy_from_query(
        query, output=_sink, format="text", delimiter="\t", null="\\N"
    )
    return b"".join(chunks)


_COLUMNS = ("id", "t", "n", "ts", "j", "arr")
_COLUMN_SQL = ", ".join(f'"{c}"' for c in _COLUMNS)


async def _create_table(conn: "asyncpg.Connection", name: str) -> None:
    await conn.execute(
        f'CREATE TABLE "{name}" ('
        "id TEXT PRIMARY KEY, "
        "t TEXT, "
        "n DOUBLE PRECISION, "
        "ts TIMESTAMPTZ, "
        "j JSONB, "
        "arr TEXT[])"
    )


async def _seed(conn: "asyncpg.Connection", name: str) -> int:
    rows = [
        ("r1", "", 0.0, "2021-01-02 03:04:05.678901+00", '{"a":[1,2,{"b":"x"}]}', ["a", "b"]),
        ("r2", None, float("nan"), "infinity", "null", []),
        ("r3", "tab\there\nnl\\bs", float("inf"), "-infinity", '{"k":"v"}', ["\\N", "\\."]),
        ("r4", "café 😀 ☃ 日本", float("-inf"), None, '"s"', ["x", "y", "z"]),
        ("r5", "\\N", -0.0, "1999-12-31 23:59:59+00", "123", ["line1\nline2", "\ttab"]),
    ]
    await conn.executemany(
        f'INSERT INTO "{name}" ({_COLUMN_SQL}) '
        "VALUES ($1,$2,$3,$4::text::timestamptz,$5::jsonb,$6)",
        rows,
    )
    return len(rows)


@pytest.mark.asyncio
async def test_copy_round_trip_preserves_all_edge_cases() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    src = f"copyfmt_src_{suffix}"
    dst = f"copyfmt_dst_{suffix}"
    try:
        async with conn.transaction():
            await apply_copy_session_settings(conn)
            await _create_table(conn, src)
            await _create_table(conn, dst)
            expected_rows = await _seed(conn, src)

            before = await _dump_query_bytes(
                conn, f'SELECT {_COLUMN_SQL} FROM "{src}" ORDER BY id'
            )

            # Export the source table into a compressed segment.
            sink = io.BytesIO()
            writer = SegmentWriter(sink)
            segment = await export_table_segment(
                conn, writer, table=src, columns=_COLUMNS
            )
            assert segment.row_count == expected_rows

            # Import the segment into a fresh identical table.
            data_copy = io.BytesIO(sink.getvalue())
            reader = SegmentReader(data_copy, total_size=len(sink.getvalue()))
            restored = segment.__class__(
                table=dst,
                columns=segment.columns,
                offset=segment.offset,
                compressed_size=segment.compressed_size,
                compressed_sha256=segment.compressed_sha256,
                uncompressed_size=segment.uncompressed_size,
                uncompressed_sha256=segment.uncompressed_sha256,
                row_count=segment.row_count,
                compression=segment.compression,
            )
            await import_table_segment(conn, reader, restored)

            after = await _dump_query_bytes(
                conn, f'SELECT {_COLUMN_SQL} FROM "{dst}" ORDER BY id'
            )

        assert after == before
    finally:
        await conn.execute(f'DROP TABLE IF EXISTS "{src}"')
        await conn.execute(f'DROP TABLE IF EXISTS "{dst}"')
        await conn.close()


@pytest.mark.asyncio
async def test_copy_round_trip_empty_table() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    src = f"copyfmt_empty_{suffix}"
    dst = f"copyfmt_emptydst_{suffix}"
    try:
        async with conn.transaction():
            await apply_copy_session_settings(conn)
            await _create_table(conn, src)
            await _create_table(conn, dst)

            sink = io.BytesIO()
            writer = SegmentWriter(sink)
            segment = await export_table_segment(
                conn, writer, table=src, columns=_COLUMNS
            )
            assert segment.row_count == 0

            data_copy = io.BytesIO(sink.getvalue())
            reader = SegmentReader(data_copy, total_size=len(sink.getvalue()))
            restored = segment.__class__(
                table=dst,
                columns=segment.columns,
                offset=segment.offset,
                compressed_size=segment.compressed_size,
                compressed_sha256=segment.compressed_sha256,
                uncompressed_size=segment.uncompressed_size,
                uncompressed_sha256=segment.uncompressed_sha256,
                row_count=segment.row_count,
                compression=segment.compression,
            )
            await import_table_segment(conn, reader, restored)

            count = await conn.fetchval(f'SELECT count(*) FROM "{dst}"')
        assert count == 0
    finally:
        await conn.execute(f'DROP TABLE IF EXISTS "{src}"')
        await conn.execute(f'DROP TABLE IF EXISTS "{dst}"')
        await conn.close()
