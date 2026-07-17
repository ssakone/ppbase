"""Integration coverage for the shared record/files write barrier."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Iterator
from io import BytesIO
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from ppbase.api import files as files_api
from ppbase.config import Settings
from ppbase.ext import record_repository as repository_module
from ppbase.ext.record_repository import RecordRepository
from ppbase.services import file_storage, write_barrier as write_barrier_module
from ppbase.services import record_storage_coordinator as coordinator
from ppbase.services.record_storage_coordinator import (
    ConnectionEngineAdapter,
    run_record_storage_transaction,
)
from ppbase.services.write_barrier import (
    WriteBarrierConnectionLostError,
    WriteBarrierError,
    backup_write_barrier,
    current_write_barrier_lease,
    mutation_write_barrier,
    storage_runtime_switch_barrier,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def record_barrier_url() -> Iterator[str]:
    with PostgresContainer(
        image="postgres:16-alpine",
        username="pprecords",
        password="pprecords",
        dbname="pprecords",
    ) as postgres:
        host = postgres.get_container_host_ip()
        port = postgres.get_exposed_port(5432)
        yield (
            "postgresql+asyncpg://pprecords:pprecords@"
            f"{host}:{port}/pprecords"
        )


@pytest_asyncio.fixture
async def record_engine(record_barrier_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        record_barrier_url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    async with engine.begin() as connection:
        await connection.execute(text('DROP TABLE IF EXISTS "posts"'))
        await connection.execute(
            text(
                'CREATE TABLE "posts" ('
                '"id" text PRIMARY KEY, "attachment" text)'
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def local_record_storage(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    return data_dir


@pytest.fixture
def record_collection_metadata(monkeypatch):
    collection = SimpleNamespace(
        id="posts_id",
        name="posts",
        schema=[{"name": "attachment", "type": "file"}],
    )

    async def _collections(_engine):
        return [collection]

    monkeypatch.setattr(coordinator, "get_all_collections", _collections)
    return collection


async def test_record_files_mutation_blocks_backup_until_transaction_finishes(
    record_engine: AsyncEngine,
    record_barrier_url: str,
    local_record_storage,
    record_collection_metadata,
) -> None:
    backup_engine = create_async_engine(record_barrier_url, poolclass=NullPool)
    mutation_entered = asyncio.Event()
    release_mutation = asyncio.Event()
    backup_entered = asyncio.Event()

    async def operation(active_engine: ConnectionEngineAdapter) -> str:
        lease = current_write_barrier_lease()
        assert lease is not None
        saved = file_storage.save_files(
            "posts_id",
            "record_1",
            "attachment",
            [("proof.txt", b"coordinated")],
            lease=lease,
        )
        async with active_engine.connect() as connection:
            await connection.execute(
                text(
                    'INSERT INTO "posts" ("id", "attachment") '
                    "VALUES (:id, :attachment)"
                ),
                {"id": "record_1", "attachment": saved[0]},
            )
        mutation_entered.set()
        await release_mutation.wait()
        return saved[0]

    async def mutate() -> str:
        return await run_record_storage_transaction(record_engine, operation)

    async def backup() -> None:
        async with backup_write_barrier(backup_engine, timeout_seconds=3):
            backup_entered.set()

    mutation_task = asyncio.create_task(mutate())
    backup_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(mutation_entered.wait(), timeout=1)
        backup_task = asyncio.create_task(backup())
        await asyncio.sleep(0.1)
        assert not backup_entered.is_set()

        release_mutation.set()
        filename = await asyncio.wait_for(mutation_task, timeout=1)
        await asyncio.wait_for(backup_entered.wait(), timeout=1)
        await backup_task

        assert file_storage.read_file_bytes(
            "posts_id",
            "record_1",
            filename,
        ) == b"coordinated"
        async with record_engine.connect() as connection:
            result = await connection.execute(
                text('SELECT "attachment" FROM "posts" WHERE "id" = :id'),
                {"id": "record_1"},
            )
            assert result.scalar_one() == filename
    finally:
        release_mutation.set()
        if not mutation_task.done():
            mutation_task.cancel()
        if backup_task is not None and not backup_task.done():
            backup_task.cancel()
        await asyncio.gather(
            mutation_task,
            *([backup_task] if backup_task is not None else []),
            return_exceptions=True,
        )
        await backup_engine.dispose()


async def test_hook_repository_reuses_outer_pid_with_pool_size_one(
    record_engine: AsyncEngine,
    monkeypatch,
) -> None:
    pids: list[int] = []
    collection = SimpleNamespace(id="posts_id", name="posts", schema=[])

    async def _resolve(_engine, _target):
        return collection

    async def _create(active_engine, _collection, data, files=None):
        async with active_engine.connect() as connection:
            pid_result = await connection.execute(text("SELECT pg_backend_pid()"))
            pids.append(int(pid_result.scalar_one()))
            await connection.execute(
                text('INSERT INTO "posts" ("id") VALUES (:id)'),
                {"id": data["id"]},
            )
        return {"id": data["id"]}

    monkeypatch.setattr(repository_module, "resolve_collection", _resolve)
    monkeypatch.setattr(repository_module, "create_record", _create)

    async def outer(active_engine: ConnectionEngineAdapter) -> dict[str, str]:
        async with active_engine.connect() as connection:
            pid_result = await connection.execute(text("SELECT pg_backend_pid()"))
            pids.append(int(pid_result.scalar_one()))
        return await RecordRepository("posts", engine=record_engine).create(
            {"id": "hook_record"}
        )

    result = await run_record_storage_transaction(record_engine, outer)
    assert result == {"id": "hook_record"}
    assert len(pids) == 2 and pids[0] == pids[1]

    async with record_engine.connect() as connection:
        count = await connection.scalar(
            text('SELECT count(*) FROM "posts" WHERE "id" = :id'),
            {"id": "hook_record"},
        )
        assert int(count) == 1


async def test_lost_shared_session_preserves_files_and_fails_closed(
    record_engine: AsyncEngine,
    record_barrier_url: str,
    local_record_storage,
) -> None:
    killer_engine = create_async_engine(record_barrier_url, poolclass=NullPool)
    written: list[str] = []

    async def operation(active_engine: ConnectionEngineAdapter) -> None:
        lease = current_write_barrier_lease()
        assert lease is not None
        written.extend(
            file_storage.save_files(
                "posts_id",
                "lost_record",
                "attachment",
                [("preserved.txt", b"preserve on lease loss")],
                lease=lease,
            )
        )
        async with killer_engine.connect() as killer:
            terminated = await killer.scalar(
                text("SELECT pg_terminate_backend(:pid)"),
                {"pid": lease.backend_pid},
            )
            assert bool(terminated)
        async with active_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    try:
        with pytest.raises(WriteBarrierConnectionLostError):
            await run_record_storage_transaction(record_engine, operation)
        assert written
        assert file_storage.read_file_bytes(
            "posts_id",
            "lost_record",
            written[0],
        ) == b"preserve on lease loss"
    finally:
        await killer_engine.dispose()


async def test_public_storage_writers_reject_direct_calls_and_accept_owning_lease(
    record_engine: AsyncEngine,
    local_record_storage,
) -> None:
    with pytest.raises(WriteBarrierError, match="explicit shared"):
        file_storage.save_files(
            "posts_id",
            "direct_record",
            "attachment",
            [("direct.txt", b"must fail")],
        )

    async with mutation_write_barrier(record_engine) as lease:
        saved = file_storage.save_files(
            "posts_id",
            "leased_record",
            "attachment",
            [("leased.txt", b"allowed")],
            lease=lease,
        )
        assert file_storage.read_file_bytes(
            "posts_id",
            "leased_record",
            saved[0],
        ) == b"allowed"
        file_storage.delete_files(
            "posts_id",
            "leased_record",
            saved,
            lease=lease,
        )

    assert file_storage.read_file_bytes(
        "posts_id",
        "leased_record",
        saved[0],
    ) is None


async def test_thumbnail_cache_miss_reopens_current_backend_after_switch(
    record_engine: AsyncEngine,
    record_barrier_url: str,
    local_record_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local cache miss must not publish after an intervening S3 switch."""
    collection_id = "posts_id"
    record_id = "thumbswitch01"
    source_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    runtime_settings = file_storage._settings
    assert runtime_settings is not None

    async with mutation_write_barrier(record_engine) as lease:
        filename = file_storage.save_files(
            collection_id,
            record_id,
            "attachment",
            [("source.png", source_png)],
            lease=lease,
        )[0]

    class _FakeS3Client:
        def __init__(self) -> None:
            self.objects: dict[tuple[str, str], bytes] = {}

        def get_object(self, *, Bucket: str, Key: str, **_kwargs):  # noqa: N803
            payload = self.objects[(Bucket, Key)]
            return {
                "Body": BytesIO(payload),
                "ContentLength": len(payload),
            }

    fake_s3 = _FakeS3Client()
    fake_s3.objects[
        ("thumbnail-switch-bucket", f"{collection_id}/{record_id}/{filename}")
    ] = source_png
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _config: fake_s3)

    collection = SimpleNamespace(
        id=collection_id,
        name="posts",
        type="base",
        schema=[
            {
                "name": "attachment",
                "type": "file",
                "options": {"thumbs": ["16x16"]},
            }
        ],
    )

    async def _resolve_collection(_session, _target):
        return collection

    monkeypatch.setattr(files_api, "resolve_collection", _resolve_collection)

    class _RowResult:
        def mappings(self):
            return self

        def first(self):
            return {"id": record_id, "attachment": filename}

    class _RequestSession:
        def __init__(self, connection) -> None:
            self._connection = connection

        async def execute(self, _statement, _params=None):
            return _RowResult()

        async def connection(self):
            return self._connection

    request = SimpleNamespace(
        query_params={"thumb": "16x16"},
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(extension_registry=None)),
    )

    cache_miss_observed = asyncio.Event()
    resume_cache_miss = asyncio.Event()
    resolve_calls: list[tuple[str, bool]] = []
    original_resolve_thumb = files_api._resolve_thumb_bytes

    async def _pause_after_first_cache_miss(
        collection_id_arg,
        record_id_arg,
        filename_arg,
        field_def,
        thumb_option,
        source_loader,
        storage_config,
        lease,
    ):
        result = await original_resolve_thumb(
            collection_id_arg,
            record_id_arg,
            filename_arg,
            field_def,
            thumb_option,
            source_loader,
            storage_config,
            lease,
        )
        resolve_calls.append((storage_config.backend, lease is None))
        if lease is None and result is None and not cache_miss_observed.is_set():
            cache_miss_observed.set()
            await resume_cache_miss.wait()
        return result

    monkeypatch.setattr(
        files_api,
        "_resolve_thumb_bytes",
        _pause_after_first_cache_miss,
    )

    switch_engine = create_async_engine(record_barrier_url, poolclass=NullPool)
    serve_task: asyncio.Task | None = None
    try:
        async with record_engine.connect() as request_connection:
            serve_task = asyncio.create_task(
                files_api.serve_file(
                    "posts",
                    record_id,
                    filename,
                    request,  # type: ignore[arg-type]
                    session=_RequestSession(request_connection),  # type: ignore[arg-type]
                    settings=runtime_settings,
                )
            )
            await asyncio.wait_for(cache_miss_observed.wait(), timeout=2)

            async with storage_runtime_switch_barrier(
                switch_engine,
                timeout_seconds=2,
            ) as switch_lease:
                file_storage.configure_storage_runtime_from_settings_payload(
                    {
                        "s3": {
                            "enabled": True,
                            "bucket": "thumbnail-switch-bucket",
                            "accessKey": "thumbnail-switch-access",
                            "secret": "thumbnail-switch-secret",
                        }
                    },
                    lease=switch_lease,
                )

            assert file_storage.get_storage_backend() == "s3"
            resume_cache_miss.set()
            response = await asyncio.wait_for(serve_task, timeout=2)
            body = b"".join(
                [chunk async for chunk in response.body_iterator]
            )
            assert body == source_png

        # The retry selected S3 only after taking the shared lease, so the old
        # local configuration was used exactly once for the lock-free cache read.
        assert resolve_calls == [("local", True)]
        old_variant = (
            local_record_storage
            / "storage"
            / collection_id
            / record_id
            / f"thumbs_{filename}"
            / f"16x16_{filename}"
        )
        assert not old_variant.exists()
    finally:
        resume_cache_miss.set()
        if serve_task is not None and not serve_task.done():
            serve_task.cancel()
            await asyncio.gather(serve_task, return_exceptions=True)
        file_storage._clear_runtime_storage_overrides_unchecked()
        await switch_engine.dispose()


async def test_detached_task_acquires_own_lease_and_ignores_parent_trackers(
    record_engine: AsyncEngine,
    local_record_storage,
) -> None:
    child_attempted = asyncio.Event()
    child_entered = asyncio.Event()
    release_parent = asyncio.Event()
    parent_targets: set[tuple[str, str, str]] = set()
    parent_lease_id: int | None = None
    child_lease_id: int | None = None

    async def detached_writer() -> str:
        nonlocal child_lease_id
        child_attempted.set()
        async with mutation_write_barrier(
            record_engine,
            timeout_seconds=2,
        ) as child_lease:
            child_lease_id = id(child_lease)
            child_entered.set()
            saved = file_storage.save_files(
                "posts_id",
                "detached_record",
                "attachment",
                [("detached.txt", b"child")],
                lease=child_lease,
            )
            return saved[0]

    child_task: asyncio.Task[str] | None = None
    try:
        async with mutation_write_barrier(record_engine) as parent_lease:
            parent_lease_id = id(parent_lease)
            with file_storage.capture_storage_writes(parent_targets):
                child_task = asyncio.create_task(detached_writer())
                await asyncio.wait_for(child_attempted.wait(), timeout=1)
                await asyncio.sleep(0.1)
                assert not child_entered.is_set()
                release_parent.set()

        assert child_task is not None
        filename = await asyncio.wait_for(child_task, timeout=2)
        assert child_lease_id is not None
        assert child_lease_id != parent_lease_id
        assert parent_targets == set()
        assert file_storage.read_file_bytes(
            "posts_id",
            "detached_record",
            filename,
        ) == b"child"
    finally:
        release_parent.set()
        if child_task is not None and not child_task.done():
            child_task.cancel()
            await asyncio.gather(child_task, return_exceptions=True)


async def test_detached_task_cannot_reuse_explicit_parent_lease(
    record_engine: AsyncEngine,
    local_record_storage,
) -> None:
    async with mutation_write_barrier(record_engine) as parent_lease:
        async def misuse_parent_lease() -> None:
            with pytest.raises(WriteBarrierError, match="current asyncio task"):
                file_storage.save_files(
                    "posts_id",
                    "misuse_record",
                    "attachment",
                    [("misuse.txt", b"forbidden")],
                    lease=parent_lease,
                )

        await asyncio.create_task(misuse_parent_lease())


async def test_authenticated_record_hook_and_batch_complete_with_pool_size_one(
    record_barrier_url: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real HTTP/auth path without a second pool checkout."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ppbase.api import records as records_api
    from ppbase.app import create_app
    from ppbase.db import engine as engine_module
    from ppbase.db.bootstrap import bootstrap_system_collections
    from ppbase.db.engine import close_engine, init_engine
    from ppbase.db.system_tables import CollectionRecord, create_system_tables
    from ppbase.ext.registry import ExtensionRegistry, HOOK_RECORD_CREATE_REQUEST
    from ppbase.services.admin_service import create_admin
    from ppbase.services.auth_service import create_admin_token

    # ``init_engine`` and ``create_app`` bind process-global runtime state.
    # Preserve the session-scoped integration app when this test runs inside
    # the full suite, while still disposing this test's pool in ``finally``.
    monkeypatch.setattr(engine_module, "_engine", engine_module._engine)
    monkeypatch.setattr(
        engine_module,
        "_session_factory",
        engine_module._session_factory,
    )
    for name in (
        "_settings",
        "_runtime_storage_overrides",
        "_s3_client_cache",
        "_s3_client_cache_key",
    ):
        monkeypatch.setattr(file_storage, name, getattr(file_storage, name))

    settings = Settings(
        database_url=record_barrier_url,
        data_dir=str(tmp_path / "http-data"),
        jwt_secret="pool-size-one-secret",
        pool_size=1,
        max_overflow=0,
        auto_migrate=False,
    )
    extensions = ExtensionRegistry()
    app = create_app(settings, extensions=extensions)
    engine = await init_engine(
        record_barrier_url,
        pool_size=1,
        max_overflow=0,
    )
    await create_system_tables(engine)
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        async with session.begin():
            await bootstrap_system_collections(session, engine)
            admin = await create_admin(
                session,
                "barrier-admin@example.test",
                "correct horse battery staple",
            )
        superusers = (
            await session.execute(
                select(CollectionRecord).where(
                    CollectionRecord.name == "_superusers"
                )
            )
        ).scalars().one()
        token = create_admin_token(
            admin,
            settings,
            superusers_collection=superusers,
        )

    detached_audit_tasks: list[asyncio.Task[dict[str, object]]] = []

    async def audit_hook(event):
        result = await event.next()
        if getattr(event.collection, "name", "") == "barrier_posts":
            hook_lease = current_write_barrier_lease()
            assert hook_lease is not None
            audit_payload = {
                "source": str(result.body.decode("utf-8"))[:80]
            }
            if str(event.data.get("title", "")) == "image":
                created_payload = json.loads(result.body)
                file_storage.write_local_storage_variant_bytes(
                    str(created_payload["collectionId"]),
                    str(created_payload["id"]),
                    "hook_metadata",
                    "lease.txt",
                    b"hook used explicit lease",
                    lease=hook_lease,
                )
            repository = event.records("barrier_audits")
            if str(event.data.get("title", "")) == "detached":
                detached_audit_tasks.append(
                    asyncio.create_task(repository.create(audit_payload))
                )
            else:
                await repository.create(audit_payload)
        return result

    extensions.hooks.get(HOOK_RECORD_CREATE_REQUEST).bind_func(audit_hook)
    headers = {"Authorization": token}
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            for name, schema in (
                (
                    "barrier_posts",
                    [
                        {"name": "title", "type": "text", "required": True},
                        {
                            "name": "attachment",
                            "type": "file",
                            "required": False,
                            "options": {"thumbs": ["16x16"]},
                        },
                    ],
                ),
                (
                    "barrier_audits",
                    [{"name": "source", "type": "text", "required": True}],
                ),
            ):
                response = await client.post(
                    "/api/collections",
                    headers=headers,
                    json={
                        "name": name,
                        "type": "base",
                        "schema": schema,
                        "listRule": "",
                        "viewRule": "",
                        "createRule": "",
                        "updateRule": "",
                        "deleteRule": "",
                    },
                )
                assert response.status_code == 200, response.text

            created = await asyncio.wait_for(
                client.post(
                    "/api/collections/barrier_posts/records",
                    headers=headers,
                    data={"title": "single"},
                    files={
                        "attachment": (
                            "single.txt",
                            b"single record file",
                            "text/plain",
                        )
                    },
                ),
                timeout=5,
            )
            assert created.status_code == 200, created.text
            created_record = created.json()
            thumbnail_fallback = await asyncio.wait_for(
                client.get(
                    "/api/files/"
                    f"{created_record['collectionId']}/"
                    f"{created_record['id']}/"
                    f"{created_record['attachment']}",
                    params={"thumb": "100x100"},
                ),
                timeout=5,
            )
            assert thumbnail_fallback.status_code == 200
            assert thumbnail_fallback.content == b"single record file"

            source_png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
                "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            image_created = await client.post(
                "/api/collections/barrier_posts/records",
                headers=headers,
                data={"title": "image"},
                files={
                    "attachment": (
                        "pixel.png",
                        source_png,
                        "image/png",
                    )
                },
            )
            assert image_created.status_code == 200, image_created.text
            image_record = image_created.json()

            thumbnail_lock_acquired = asyncio.Event()
            thumbnail_connection_invalidated = asyncio.Event()
            wait_forever = asyncio.Event()
            captured_thumbnail_connection = None
            thumbnail_invalidation_observed = False
            original_acquire_lock = write_barrier_module._acquire_lock
            original_cleanup_connection = write_barrier_module._cleanup_connection

            async def acquire_thumbnail_lock_then_wait(
                connection,
                *,
                key: int,
                mode,
                deadline: float,
                label: str,
                finish_transaction: bool,
            ) -> None:
                nonlocal captured_thumbnail_connection
                if finish_transaction:
                    await original_acquire_lock(
                        connection,
                        key=key,
                        mode=mode,
                        deadline=deadline,
                        label=label,
                        finish_transaction=finish_transaction,
                    )
                    return
                captured_thumbnail_connection = connection
                result = await connection.execute(
                    text("SELECT pg_advisory_lock_shared(:key)"),
                    {"key": key},
                )
                assert result.scalar_one() is None
                thumbnail_lock_acquired.set()
                await wait_forever.wait()

            async def observe_thumbnail_invalidation(connection) -> None:
                nonlocal thumbnail_invalidation_observed
                await original_cleanup_connection(connection)
                thumbnail_invalidation_observed = connection.invalidated
                thumbnail_connection_invalidated.set()

            with monkeypatch.context() as thumbnail_patch:
                thumbnail_patch.setattr(
                    write_barrier_module,
                    "_acquire_lock",
                    acquire_thumbnail_lock_then_wait,
                )
                thumbnail_patch.setattr(
                    write_barrier_module,
                    "_cleanup_connection",
                    observe_thumbnail_invalidation,
                )
                cancelled_thumbnail = asyncio.create_task(
                    client.get(
                        "/api/files/"
                        f"{image_record['collectionId']}/"
                        f"{image_record['id']}/"
                        f"{image_record['attachment']}",
                        params={"thumb": "16x16"},
                    )
                )
                await asyncio.wait_for(thumbnail_lock_acquired.wait(), timeout=2)
                cancelled_thumbnail.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await cancelled_thumbnail
                await asyncio.wait_for(
                    thumbnail_connection_invalidated.wait(),
                    timeout=2,
                )
                assert captured_thumbnail_connection is not None
                assert thumbnail_invalidation_observed

            # The cancelled cache publication must not poison the one-entry
            # pool; a fresh request reconnects, takes the shared barrier, and
            # publishes the thumbnail normally.
            generated_thumbnail = await client.get(
                "/api/files/"
                f"{image_record['collectionId']}/"
                f"{image_record['id']}/"
                f"{image_record['attachment']}",
                params={"thumb": "16x16"},
            )
            assert generated_thumbnail.status_code == 200
            assert generated_thumbnail.content.startswith(b"\x89PNG\r\n\x1a\n")
            assert generated_thumbnail.content != source_png
            assert file_storage.read_local_storage_variant_bytes(
                image_record["collectionId"],
                image_record["id"],
                "hook_metadata",
                "lease.txt",
            ) == b"hook used explicit lease"

            batch = await asyncio.wait_for(
                client.post(
                    "/api/batch",
                    headers=headers,
                    json={
                        "requests": [
                            {
                                "method": "POST",
                                "url": "/api/collections/barrier_posts/records",
                                "body": {"title": "batch-one"},
                            },
                            {
                                "method": "POST",
                                "url": "/api/collections/barrier_posts/records",
                                "body": {"title": "batch-two"},
                            },
                        ]
                    },
                ),
                timeout=5,
            )
            assert batch.status_code == 200, batch.text
            assert len(batch.json()) == 2

            detached = await client.post(
                "/api/collections/barrier_posts/records",
                headers=headers,
                json={"title": "detached"},
            )
            assert detached.status_code == 200, detached.text
            assert len(detached_audit_tasks) == 1
            await asyncio.wait_for(detached_audit_tasks[0], timeout=5)

            switch_to_s3 = await asyncio.wait_for(
                client.patch(
                    "/api/settings",
                    headers=headers,
                    json={
                        "s3": {
                            "enabled": True,
                            "bucket": "pool-one-bucket",
                            "accessKey": "pool-one-access",
                            "secret": "pool-one-secret",
                        }
                    },
                ),
                timeout=5,
            )
            assert switch_to_s3.status_code == 200, switch_to_s3.text
            assert file_storage.get_storage_backend() == "s3"

            switch_to_local = await asyncio.wait_for(
                client.patch(
                    "/api/settings",
                    headers=headers,
                    json={"s3": {"enabled": False}},
                ),
                timeout=5,
            )
            assert switch_to_local.status_code == 200, switch_to_local.text
            assert file_storage.get_storage_backend() == "local"

        async with engine.connect() as connection:
            post_count = await connection.scalar(
                text('SELECT count(*) FROM "barrier_posts"')
            )
            audit_count = await connection.scalar(
                text('SELECT count(*) FROM "barrier_audits"')
            )
            assert int(post_count) == 5
            assert int(audit_count) == 5
    finally:
        for task in detached_audit_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*detached_audit_tasks, return_exceptions=True)
        # Make accidental future use of the uncoordinated dependency obvious.
        assert records_api._get_mutation_auth is not records_api.get_optional_auth
        await close_engine()
