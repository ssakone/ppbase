"""Live round-trip tests for the native (v2) orchestrator (``ppbase.backup.native``).

These exercise the full orchestration surface that ``NativeBackupService`` relies
on, end to end against a real PostgreSQL server:

* a source database is bootstrapped with the exact PPBase system schema
  (``Base.metadata.create_all``) plus a couple of collection relations and an
  applied migration, then :func:`export_native_database` streams it to a real
  ``schema.json`` + ``data.copy`` pair on a raw asyncpg connection inside the
  export transaction;
* :func:`restore_native_database` rebuilds that archive into a *separate empty*
  database and the data / schema / commit marker are verified.

Skipped when there is no reachable server or the role lacks CREATE DATABASE.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from ppbase.backup.native import (  # noqa: E402
    NativeExportError,
    build_public_inventory,
    collect_applied_migrations,
    export_native_database,
    restore_native_database,
)
from ppbase.backup.schema_contract import DatabaseSchema  # noqa: E402
from ppbase.db.system_tables import Base  # noqa: E402


def _admin_dsn() -> str:
    url = os.environ.get("PPBASE_TEST_DATABASE_URL")
    if url:
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return "postgresql://ppbase:ppbase@localhost:5433/postgres"


def _dsn_for(db: str) -> str:
    head, _, _tail = _admin_dsn().rpartition("/")
    return f"{head}/{db}"


def _sa_url_for(db: str) -> str:
    return _dsn_for(db).replace("postgresql://", "postgresql+asyncpg://", 1)


async def _connect_admin() -> "asyncpg.Connection":
    try:
        conn = await asyncpg.connect(_admin_dsn(), timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no reachable PostgreSQL server: {exc}")
    row = await conn.fetchrow(
        "SELECT rolcreatedb OR rolsuper AS can FROM pg_roles "
        "WHERE rolname = current_user"
    )
    if not row or not row["can"]:  # pragma: no cover - environment dependent
        await conn.close()
        pytest.skip("test role cannot CREATE DATABASE")
    return conn


async def _create_system_schema(db: str) -> None:
    """Bootstrap ``db`` with the exact PPBase system schema via the ORM."""
    engine = create_async_engine(
        make_url(_sa_url_for(db)), poolclass=NullPool
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _seed_source(conn: "asyncpg.Connection", *, base: str, view: str) -> int:
    """Create a base collection + a view collection and register them.

    Returns the number of seeded rows in the base collection's table.
    """
    # A base collection whose physical shape exactly matches what PPBase
    # authors (id/created/updated system columns + the ``text`` and integer
    # ``number`` fields), registered with the matching field schema so the v2
    # canonical model reconciles it as an authored table.
    base_schema = [
        {"name": "title", "type": "text"},
        {"name": "views", "type": "number", "options": {"onlyInt": True}},
    ]
    await conn.execute(
        f'CREATE TABLE "{base}" ('
        '"id" varchar(15) PRIMARY KEY, '
        '"created" timestamptz NOT NULL DEFAULT now(), '
        '"updated" timestamptz NOT NULL DEFAULT now(), '
        '"title" text NOT NULL DEFAULT \'\', '
        '"views" integer NOT NULL DEFAULT 0)'
    )
    rows = [
        ("rec1", "", 0),
        ("rec2", "tab\there\nnl\\bs", 5),
        ("rec3", "café 😀 ☃ 日本", 7),
    ]
    await conn.executemany(
        f'INSERT INTO "{base}" (id, title, views) VALUES ($1, $2, $3)', rows
    )
    await conn.execute(
        'INSERT INTO "_collections" (id, name, type, schema) '
        "VALUES ($1, $2, $3, $4)",
        uuid.uuid4().hex[:15],
        base,
        "base",
        json.dumps(base_schema),
    )

    # A view collection: real view + a `_collections` row of type ``view``.
    await conn.execute(
        f'CREATE VIEW "{view}" AS SELECT id, title FROM "{base}"'
    )
    await conn.execute(
        'INSERT INTO "_collections" (id, name, type) VALUES ($1, $2, $3)',
        uuid.uuid4().hex[:15],
        view,
        "view",
    )
    return len(rows)


async def _dump(conn: "asyncpg.Connection", table: str) -> bytes:
    chunks: list[bytes] = []

    async def _sink(data: bytes) -> None:
        chunks.append(bytes(data))

    await conn.copy_from_query(
        f'SELECT id, title, views FROM "{table}" ORDER BY id',
        output=_sink,
        format="text",
        delimiter="\t",
        null="\\N",
    )
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_build_public_inventory_classifies_system_base_and_view() -> None:
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_inv_{suffix}"
    base = f"items_{suffix}"
    view = f"itemsview_{suffix}"
    src = None
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}" TEMPLATE template0')
        await _create_system_schema(src_db)
        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        await _seed_source(src, base=base, view=view)

        tables, views = await build_public_inventory(src)

        # Every ORM system table is classified ``system``.
        for name in Base.metadata.tables:
            assert tables.get(name) == "system"
        # The base collection is a base table; the view collection is a view.
        assert tables.get(base) == "base"
        assert base not in views
        assert view in views
        assert view not in tables
    finally:
        if src is not None:
            await src.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_collect_applied_migrations_hashes_present_files(
    tmp_path: Path,
) -> None:
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_mig_{suffix}"
    src = None
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    first = "0001_first.py"
    second = "0002_second.py"
    (migrations_dir / first).write_bytes(b"# real migration bytes\n")
    (migrations_dir / second).write_bytes(b"# another migration\n")
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}" TEMPLATE template0')
        await _create_system_schema(src_db)
        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        await src.execute(
            'INSERT INTO "_migrations" (file) VALUES ($1), ($2)',
            first,
            second,
        )

        specs = await collect_applied_migrations(
            src, migrations_dir=migrations_dir
        )
        by_file = {s.file: s for s in specs}
        assert set(by_file) == {first, second}
        import hashlib

        for name in (first, second):
            # Every applied migration records a verifiable, non-null hash.
            assert by_file[name].sha256 == hashlib.sha256(
                (migrations_dir / name).read_bytes()
            ).hexdigest()
    finally:
        if src is not None:
            await src.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_collect_applied_migrations_refuses_missing_file(
    tmp_path: Path,
) -> None:
    """An applied migration with no file on disk fails the backup closed."""
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_mig_{suffix}"
    src = None
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    present = "0001_present.py"
    missing = "0002_missing.py"
    (migrations_dir / present).write_bytes(b"# real migration bytes\n")
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}" TEMPLATE template0')
        await _create_system_schema(src_db)
        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        # One applied row's file was deleted from disk (orphaned).
        await src.execute(
            'INSERT INTO "_migrations" (file) VALUES ($1), ($2)',
            present,
            missing,
        )

        with pytest.raises(NativeExportError):
            await collect_applied_migrations(src, migrations_dir=migrations_dir)
    finally:
        if src is not None:
            await src.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_collect_applied_migrations_refuses_symlink(
    tmp_path: Path,
) -> None:
    """An applied migration served by a symlink is refused."""
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_mig_{suffix}"
    src = None
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"# migration bytes outside the dir\n")
    linked = "0001_linked.py"
    (migrations_dir / linked).symlink_to(outside)
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}" TEMPLATE template0')
        await _create_system_schema(src_db)
        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        await src.execute(
            'INSERT INTO "_migrations" (file) VALUES ($1)', linked
        )

        with pytest.raises(NativeExportError):
            await collect_applied_migrations(src, migrations_dir=migrations_dir)
    finally:
        if src is not None:
            await src.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_export_then_restore_round_trip(tmp_path: Path) -> None:
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_nsrc_{suffix}"
    dst_db = f"ppb_ndst_{suffix}"
    base = f"items_{suffix}"
    view = f"itemsview_{suffix}"
    restore_id = uuid.uuid4().hex  # 32 lowercase hex
    schema_path = tmp_path / "schema.json"
    copy_path = tmp_path / "data.copy"
    migrations_dir = tmp_path / "pb_migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_init.py").write_bytes(b"# init migration\n")
    src = dst = None
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}" TEMPLATE template0')
        await admin.execute(f'CREATE DATABASE "{dst_db}" TEMPLATE template0')
        await _create_system_schema(src_db)

        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        expected_rows = await _seed_source(src, base=base, view=view)
        await src.execute(
            'INSERT INTO "_migrations" (file) VALUES ($1)', "0001_init.py"
        )
        before = await _dump(src, base)

        # Export inside the caller-owned snapshot transaction.  Production pins
        # ``search_path = pg_catalog, pg_temp`` (see
        # ``postgres.set_backup_control_search_path``) for the export so
        # ``pg_get_viewdef`` schema-qualifies ``public`` relations; mirror it so
        # the archived view definitions match the post-restore verification.
        async with src.transaction(isolation="repeatable_read", readonly=True):
            await src.execute("SET LOCAL search_path = pg_catalog, pg_temp")
            migrations = await collect_applied_migrations(
                src, migrations_dir=migrations_dir
            )
            schema = await export_native_database(
                src,
                schema_path=schema_path,
                copy_path=copy_path,
                migrations=migrations,
            )

        # The written schema.json is a valid, complete contract.
        on_disk = DatabaseSchema.from_archive_bytes(schema_path.read_bytes())
        assert on_disk == schema
        assert {t.name for t in schema.tables} >= {base}
        assert {v.name for v in schema.views} == {view}
        assert {m.file for m in schema.migrations} == {"0001_init.py"}
        assert schema.migrations[0].sha256 is not None
        assert copy_path.stat().st_size == schema.data_copy_size

        # Restore into the fresh empty target.
        dst = await asyncpg.connect(_dsn_for(dst_db), timeout=5)
        with open(copy_path, "rb") as copy_file:
            restored = await restore_native_database(
                dst,
                schema_bytes=schema_path.read_bytes(),
                copy_file=copy_file,
                copy_size=copy_path.stat().st_size,
                restore_id=restore_id,
            )
        assert restored == schema

        # Data is byte-for-byte, the view exists, the marker records this restore.
        after = await _dump(dst, base)
        assert after == before
        assert await dst.fetchval(f'SELECT count(*) FROM "{base}"') == expected_rows
        assert await dst.fetchval(f'SELECT count(*) FROM "{view}"') == expected_rows
        marker = await dst.fetchval(
            'SELECT restore_id FROM "_ppbase_restore_control".commits '
            "ORDER BY committed_at DESC LIMIT 1"
        )
        assert marker == restore_id
    finally:
        if src is not None:
            await src.close()
        if dst is not None:
            await dst.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.execute(f'DROP DATABASE IF EXISTS "{dst_db}" WITH (FORCE)')
        await admin.close()
