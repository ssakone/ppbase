"""Failure handling for Dashboard migration-file generation."""

from pathlib import Path
from unittest.mock import AsyncMock, create_autospec

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ppbase.db.system_tables import CollectionRecord
from ppbase.services import collection_service


@pytest.mark.asyncio
async def test_history_failure_removes_newly_generated_file(tmp_path: Path) -> None:
    session = create_autospec(AsyncSession, instance=True)
    session.flush = AsyncMock(side_effect=RuntimeError("history failed"))
    collection = CollectionRecord(
        id="generation001",
        name="generation_test",
        type="base",
        system=False,
        schema=[],
        indexes=[],
        options={},
    )

    with pytest.raises(RuntimeError, match="history failed"):
        await collection_service._maybe_generate_migration(
            session,
            auto_migrate=True,
            migrations_dir=str(tmp_path),
            kind="create",
            record=collection,
        )

    assert not list(tmp_path.glob("*.py"))


@pytest.mark.asyncio
async def test_generation_error_rolls_back_instead_of_being_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = create_autospec(AsyncSession, instance=True)

    async def _fail(*_args, **_kwargs):
        raise RuntimeError("generation failed")

    monkeypatch.setattr(collection_service, "_maybe_generate_migration", _fail)

    with pytest.raises(RuntimeError, match="generation failed"):
        await collection_service._generate_migration_or_rollback(
            session,
            auto_migrate=True,
            migrations_dir="unused",
            kind="create",
        )

    session.rollback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_commit_failure_removes_only_current_generated_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = tmp_path / "current.py"
    existing = tmp_path / "existing.py"
    current.write_text("current", encoding="utf-8")
    existing.write_text("existing", encoding="utf-8")
    session = create_autospec(AsyncSession, instance=True)
    session.commit = AsyncMock(side_effect=RuntimeError("commit failed"))
    engine = create_autospec(AsyncEngine, instance=True)

    async def _known_rollback(
        _engine: AsyncEngine,
        generated_paths: list[str],
        *,
        lock_timeout_seconds: float,
    ) -> set[str]:
        collection_service._cleanup_generated_migrations(generated_paths)
        return set()

    history = AsyncMock(side_effect=_known_rollback)
    monkeypatch.setattr(
        collection_service,
        "_reconcile_generated_migrations",
        history,
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        await collection_service._commit_with_generated_migrations(
            session,
            engine,
            [str(current)],
        )

    session.rollback.assert_awaited_once_with()
    history.assert_awaited_once_with(
        engine,
        [str(current)],
        lock_timeout_seconds=30.0,
    )
    assert not current.exists()
    assert existing.exists()


@pytest.mark.asyncio
async def test_unverifiable_commit_preserves_generated_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services.migration_runner import MigrationCommitOutcomeError

    current = tmp_path / "ambiguous.py"
    current.write_text("intent", encoding="utf-8")
    session = create_autospec(AsyncSession, instance=True)
    session.commit = AsyncMock(side_effect=RuntimeError("commit connection lost"))
    engine = create_autospec(AsyncEngine, instance=True)
    history = AsyncMock(side_effect=RuntimeError("verification unavailable"))
    monkeypatch.setattr(
        collection_service,
        "_reconcile_generated_migrations",
        history,
    )

    with pytest.raises(MigrationCommitOutcomeError, match="outcome is unknown"):
        await collection_service._commit_with_generated_migrations(
            session,
            engine,
            [str(current)],
        )

    assert current.exists()


@pytest.mark.asyncio
async def test_bulk_import_preserves_files_for_ambiguous_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services.migration_runner import MigrationCommitOutcomeError

    generated = tmp_path / "1700000000_bulk_import.py"
    session = create_autospec(AsyncSession, instance=True)
    engine = create_autospec(AsyncEngine, instance=True)

    async def _import_with_generated_file(
        *_args,
        generated_paths: list[str],
        **_kwargs,
    ) -> None:
        generated.write_text("durable intent", encoding="utf-8")
        generated_paths.append(str(generated))

    monkeypatch.setattr(
        collection_service,
        "_lock_migration_producer",
        AsyncMock(),
    )
    monkeypatch.setattr(
        collection_service,
        "_import_collections_impl",
        _import_with_generated_file,
    )
    monkeypatch.setattr(
        collection_service,
        "_commit_with_generated_migrations",
        AsyncMock(side_effect=MigrationCommitOutcomeError("ambiguous outcome")),
    )

    with pytest.raises(MigrationCommitOutcomeError, match="ambiguous outcome"):
        await collection_service.import_collections(session, engine, [])

    session.rollback.assert_awaited_once_with()
    assert generated.exists()


@pytest.mark.asyncio
async def test_collection_mutation_lock_is_independent_of_file_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import migration_runner

    session = create_autospec(AsyncSession, instance=True)
    acquire = AsyncMock()
    monkeypatch.setattr(
        migration_runner,
        "acquire_migration_transaction_lock",
        acquire,
    )

    await collection_service._lock_migration_producer(
        session,
        migration_lock_timeout=2.5,
    )

    acquire.assert_awaited_once_with(session, timeout_seconds=2.5)
