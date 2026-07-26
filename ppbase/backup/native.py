"""Native (v2) backup orchestration: schema.json + data.copy over asyncpg.

This module ties the individual native building blocks together into the two
operations the :class:`~ppbase.backup.service.NativeBackupService` needs:

* :func:`export_native_database` — introspect ``public`` against the live
  PPBase inventory, stream every table out with ``COPY ... TO STDOUT`` into a
  compressed ``data.copy`` container, and write the validated
  ``schema.json``.  It never shells out to ``pg_dump``.
* :func:`restore_native_database` — load a completeness-checked
  :class:`~ppbase.backup.schema_contract.DatabaseSchema` and replay it into a
  target database with :func:`ppbase.backup.rebuild.run_native_destructive_restore`.
  It never shells out to ``pg_restore``/``psql``.

Both run entirely on a raw asyncpg connection.  The caller owns the surrounding
snapshot/transaction and MVCC coordination (write barrier, exported snapshot);
this module only performs the ordered introspection/COPY steps.
"""

from __future__ import annotations

import hashlib
import hmac
import stat
from pathlib import Path
from typing import Any, BinaryIO

from ppbase.backup.copy_format import (
    SegmentReader,
    SegmentWriter,
    apply_copy_session_settings,
    export_table_segment,
)
from ppbase.backup.canonical import build_canonical_tables
from ppbase.backup.models import BackupError
from ppbase.backup.schema_contract import (
    CopySegment,
    DatabaseSchema,
    MigrationSpec,
    introspect_public_schema,
)


class NativeExportError(BackupError):
    """Raised when a native (v2) backup cannot be produced."""


class NativeRestoreError(BackupError):
    """Raised when a native (v2) archive fails its pre-restore verification."""


async def build_public_inventory(
    pg_conn: Any,
) -> tuple[dict[str, str], frozenset[str]]:
    """Return ``(table_inventory, view_inventory)`` for the live PPBase schema.

    The inventory is assembled from two authoritative sources so the schema
    contract's *bidirectional* reconciliation has an exact expected set:

    * the fixed system infrastructure tables declared as SQLAlchemy ORM models
      (``_collections``, ``_superusers``, ``_params`` …), always ``system``; and
    * one relation per ``_collections`` row — a ``view`` collection becomes a
      view, an ``auth`` collection an ``auth`` table, everything else a ``base``
      table.

    A name that is both a system table and a collection row (PocketBase stores
    some system collections in ``_collections`` too) stays classified as a
    system table.
    """
    # Imported lazily so the backup package does not hard-depend on the ORM at
    # import time (keeps copy_format/schema_contract importable stand-alone).
    from ppbase.db.system_tables import Base

    table_inventory: dict[str, str] = {
        name: "system" for name in Base.metadata.tables
    }
    view_inventory: set[str] = set()

    # Fully schema-qualified so the query is correct even when the caller has
    # locked ``search_path`` (e.g. after contract inspection pins pg_catalog).
    rows = await pg_conn.fetch('SELECT name, type FROM public."_collections"')
    for row in rows:
        name = row["name"]
        if not isinstance(name, str) or not name:
            raise NativeExportError("_collections row has an empty name")
        if name in table_inventory:
            # A system infrastructure table always wins its classification.
            continue
        kind = row["type"]
        if kind == "view":
            view_inventory.add(name)
        elif kind == "auth":
            table_inventory[name] = "auth"
        elif kind == "base":
            table_inventory[name] = "base"
        else:
            raise NativeExportError(
                f"collection {name!r} has unsupported type {kind!r}"
            )
    return table_inventory, frozenset(view_inventory)


def _hash_applied_migration(
    migrations_dir: Path,
    file_name: str,
    *,
    error_cls: type[BackupError] = NativeExportError,
) -> str:
    """Return the SHA-256 of an applied migration file, failing closed.

    An applied migration recorded in ``_migrations`` (create) or in the archive
    contract (restore) MUST resolve to a readable regular file living directly
    under ``migrations_dir``.  A name that is not a plain basename, a symlink,
    an irregular file, a path that escapes the directory, or a file that is
    simply missing raises ``error_cls`` — a verifiable hash is mandatory, never
    a null.
    """
    if (
        not isinstance(file_name, str)
        or not file_name
        or file_name in (".", "..")
        or "/" in file_name
        or "\\" in file_name
        or Path(file_name).name != file_name
        or not file_name[0].isdigit()
        or Path(file_name).suffix != ".py"
    ):
        raise error_cls(
            f"applied migration {file_name!r} is not a valid migration filename"
        )
    if migrations_dir.is_symlink():
        raise error_cls("the migrations directory must not be a symlink")
    root = migrations_dir.resolve()
    if not root.is_dir():
        raise error_cls("the migrations directory is missing or is not a directory")
    candidate = migrations_dir / file_name
    # Never follow a symlink standing in for the migration file.
    if candidate.is_symlink():
        raise error_cls(
            f"applied migration {file_name!r} is a symlink and cannot be trusted"
        )
    try:
        resolved = candidate.resolve()
        if resolved.parent != root:
            raise error_cls(
                f"applied migration {file_name!r} escapes the migrations directory"
            )
        file_stat = candidate.stat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise error_cls(
                f"applied migration {file_name!r} is not a regular file"
            )
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as exc:
        raise error_cls(
            f"applied migration {file_name!r} is missing or unreadable"
        ) from exc


def verify_migration_hashes_on_disk(
    migrations: tuple[MigrationSpec, ...], *, migrations_dir: Path
) -> None:
    """Verify every archived applied migration against ``migrations_dir``.

    For each :class:`MigrationSpec` recorded in the archive, recompute the
    SHA-256 of the on-disk file and require it to match.  Refuses when the spec
    carries no hash, or the file is missing, a symlink, irregular, escapes the
    directory, or hashes to a different value (including a different but validly
    shaped 64-hex string).  Extra unapplied local migration files that are not
    present in the archive are ignored, so a newer runtime may keep them.
    """
    for spec in migrations:
        if spec.sha256 is None:
            raise NativeRestoreError(
                f"archived migration {spec.file!r} has no verifiable hash"
            )
        actual = _hash_applied_migration(
            migrations_dir, spec.file, error_cls=NativeRestoreError
        )
        if not hmac.compare_digest(actual, spec.sha256):
            raise NativeRestoreError(
                f"archived migration {spec.file!r} does not match the file in "
                "the migrations directory"
            )


async def collect_applied_migrations(
    pg_conn: Any, *, migrations_dir: Path
) -> tuple[MigrationSpec, ...]:
    """Return one :class:`MigrationSpec` per row in the ``_migrations`` table.

    Each spec pairs the recorded filename and applied timestamp with the
    SHA-256 of the file's current bytes under ``migrations_dir``.  Every applied
    migration must resolve to a readable regular file directly inside
    ``migrations_dir``; a missing, symlinked, irregular, or path-escaping file
    fails the backup closed (see :func:`_hash_applied_migration`) rather than
    recording an unverifiable null hash.
    """
    # Fully schema-qualified so the query is correct even when the caller has
    # locked ``search_path`` (e.g. after contract inspection pins pg_catalog).
    rows = await pg_conn.fetch(
        'SELECT file, applied FROM public."_migrations" ORDER BY file'
    )
    specs: list[MigrationSpec] = []
    for row in rows:
        file_name = row["file"]
        applied = row["applied"]
        applied_text = applied.isoformat() if hasattr(applied, "isoformat") else str(applied)
        sha256 = _hash_applied_migration(migrations_dir, file_name)
        specs.append(
            MigrationSpec(file=file_name, sha256=sha256, applied=applied_text)
        )
    return tuple(specs)


async def export_native_database(
    pg_conn: Any,
    *,
    schema_path: Path,
    copy_path: Path,
    migrations: tuple[MigrationSpec, ...] = (),
    source_postgres_version: int | None = None,
) -> DatabaseSchema:
    """Introspect ``public`` and stream it to ``schema_path`` + ``copy_path``.

    ``pg_conn`` must be a raw asyncpg connection already inside the export
    transaction (REPEATABLE READ, READ ONLY, snapshot-pinned by the caller).
    The COPY session GUCs are applied here so exported text values match the
    contract recorded in ``schema.json``.  Returns the validated
    :class:`DatabaseSchema` that was written.
    """
    await apply_copy_session_settings(pg_conn)
    table_inventory, view_inventory = await build_public_inventory(pg_conn)

    # Build the canonical PPBase model (system tables from the ORM, collections
    # from ``_collections.schema``) and require it to cover the table inventory
    # exactly.  Passing it to introspection reconciles every live table's
    # columns / primary key / unique constraints against the shape PPBase
    # authored — so a raw table merely registered in ``_collections`` is
    # refused instead of silently captured.
    canonical_tables = await build_canonical_tables(pg_conn)
    if set(canonical_tables) != set(table_inventory):
        missing = sorted(set(table_inventory) - set(canonical_tables))
        extra = sorted(set(canonical_tables) - set(table_inventory))
        raise NativeExportError(
            "backup refused: canonical model does not exactly cover the table "
            f"inventory (missing={missing}, unexpected={extra})"
        )

    # 1. Schema-only introspection: fails closed on any unmanaged object and
    #    reconciles the live schema against the inventory before a byte is read.
    schema_only = await introspect_public_schema(
        pg_conn,
        table_inventory=table_inventory,
        view_inventory=view_inventory,
        canonical_tables=canonical_tables,
        migrations=migrations,
        source_postgres_version=source_postgres_version,
    )

    # 2. Stream every table (name order) into the compressed data.copy sink.
    segments: list[CopySegment] = []
    with open(copy_path, "wb") as copy_file:
        writer = SegmentWriter(copy_file)
        for table in sorted(schema_only.tables, key=lambda t: t.name):
            segment = await export_table_segment(
                pg_conn,
                writer,
                table=table.name,
                columns=table.column_names,
            )
            segments.append(segment)
        copy_file.flush()
        data_copy_size = writer.total_size

    # 3. Assemble and re-validate the complete archive, then persist schema.json.
    schema = DatabaseSchema(
        tables=schema_only.tables,
        views=schema_only.views,
        indexes=schema_only.indexes,
        migrations=schema_only.migrations,
        segments=tuple(segments),
        data_copy_size=data_copy_size,
        source_postgres_version=schema_only.source_postgres_version,
    )
    schema.assert_every_table_exported()
    schema_path.write_bytes(schema.to_canonical_bytes())
    return schema


async def restore_native_database(
    pg_conn: Any,
    *,
    schema_bytes: bytes,
    copy_file: BinaryIO,
    copy_size: int,
    restore_id: str,
    verify: bool = True,
) -> DatabaseSchema:
    """Rebuild a native archive into ``pg_conn`` and write the commit marker.

    ``schema_bytes`` is the raw ``schema.json`` payload and ``copy_file`` is a
    seekable binary handle over the pinned ``data.copy`` of ``copy_size`` bytes.
    Delegates the atomic drop-and-replace (plus the
    ``_ppbase_restore_control.commits`` marker) to
    :func:`ppbase.backup.rebuild.run_native_destructive_restore`.
    """
    # Imported here to avoid a heavy import chain at module load.
    from ppbase.backup.rebuild import run_native_destructive_restore

    schema = DatabaseSchema.from_archive_bytes(schema_bytes)
    reader = SegmentReader(copy_file, total_size=copy_size)
    await run_native_destructive_restore(
        pg_conn, schema, reader, restore_id=restore_id, verify=verify
    )
    return schema


__all__ = [
    "NativeExportError",
    "NativeRestoreError",
    "build_public_inventory",
    "collect_applied_migrations",
    "export_native_database",
    "restore_native_database",
    "verify_migration_hashes_on_disk",
]
