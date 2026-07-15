"""Focused transaction-boundary tests for the dynamic schema manager."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    AsyncTransaction,
)

from ppbase.db.schema_manager import (
    _managed_indexes,
    create_collection_table,
    update_collection_table,
    validate_view_query,
)


def _collection(
    *,
    name: str,
    collection_type: str = "base",
    schema: list[dict] | None = None,
    indexes: list[str] | None = None,
    query: str | None = None,
) -> SimpleNamespace:
    options = {"query": query} if query is not None else {}
    return SimpleNamespace(
        name=name,
        type=collection_type,
        schema=schema or [],
        indexes=indexes or [],
        options=options,
    )


def _connection() -> AsyncConnection:
    connection = create_autospec(AsyncConnection, instance=True)
    result = MagicMock()
    result.one_or_none.return_value = None
    connection.execute = AsyncMock(return_value=result)
    return connection


def _nested_transaction(connection: AsyncConnection) -> AsyncTransaction:
    transaction = create_autospec(AsyncTransaction, instance=True)

    async def begin_nested() -> AsyncTransaction:
        return transaction

    connection.begin_nested = MagicMock(side_effect=begin_nested)
    return transaction


@pytest.mark.asyncio
async def test_create_reuses_supplied_connection_without_starting_transaction() -> None:
    connection = _connection()

    await create_collection_table(
        connection,
        _collection(name="transactional_articles"),
    )

    connection.begin.assert_not_called()
    connection.begin_nested.assert_not_called()
    sql = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert sql[0].startswith('CREATE TABLE IF NOT EXISTS "transactional_articles"')
    assert any(
        statement.startswith(
            'CREATE INDEX IF NOT EXISTS "idx_transactional_articles_created"'
        )
        for statement in sql
    )


@pytest.mark.asyncio
async def test_create_reuses_session_connection_without_commit_or_rollback() -> None:
    session = create_autospec(AsyncSession, instance=True)
    connection = _connection()
    session.connection = AsyncMock(return_value=connection)

    await create_collection_table(
        session,
        _collection(name="session_articles"),
    )

    session.connection.assert_awaited_once_with()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    assert connection.execute.await_count == 3


@pytest.mark.asyncio
async def test_engine_fallback_keeps_standalone_transaction_behavior() -> None:
    engine = create_autospec(AsyncEngine, instance=True)
    connection = _connection()
    transaction_context = MagicMock()
    transaction_context.__aenter__ = AsyncMock(return_value=connection)
    transaction_context.__aexit__ = AsyncMock(return_value=False)
    engine.begin.return_value = transaction_context

    await create_collection_table(
        engine,
        _collection(name="standalone_articles"),
    )

    engine.begin.assert_called_once_with()
    transaction_context.__aenter__.assert_awaited_once_with()
    transaction_context.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_runs_alter_and_index_sync_on_same_connection() -> None:
    connection = _connection()

    async def execute(statement, parameters=None):
        del parameters
        result = MagicMock()
        if (
            "FROM pg_catalog.pg_class AS index_relation" in str(statement)
            and "pg_get_indexdef" not in str(statement)
        ):
            result.one_or_none.return_value = ("i", "new_articles", "public")
        else:
            result.one_or_none.return_value = None
        return result

    connection.execute.side_effect = execute
    old = _collection(
        name="old_articles",
        schema=[{
            "id": "field_tags",
            "name": "tags",
            "type": "select",
            "options": {"maxSelect": 1},
        }],
    )
    new = _collection(
        name="new_articles",
        schema=[{
            "id": "field_tags",
            "name": "tags",
            "type": "select",
            "options": {"maxSelect": 2},
        }],
    )

    await update_collection_table(connection, old, new)

    connection.begin.assert_not_called()
    sql = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert sql[0] == 'ALTER TABLE "old_articles" RENAME TO "new_articles"'
    assert any(
        statement == 'DROP INDEX IF EXISTS "public"."idx_old_articles_created"'
        for statement in sql
    )
    assert any(
        statement.startswith(
            'CREATE INDEX IF NOT EXISTS "idx_new_articles_tags" '
        )
        for statement in sql
    )


@pytest.mark.asyncio
async def test_view_update_validates_and_replaces_on_same_connection() -> None:
    connection = _connection()
    transaction = _nested_transaction(connection)
    table_type = MagicMock()
    table_type.first.return_value = ("VIEW",)
    connection.execute.return_value = table_type

    await update_collection_table(
        connection,
        _collection(
            name="article_summary",
            collection_type="view",
            query="SELECT 1 AS id",
        ),
        _collection(
            name="article_summary",
            collection_type="view",
            query="SELECT 2 AS id",
        ),
    )

    connection.begin.assert_not_called()
    assert connection.begin_nested.call_count == 2
    transaction.rollback.assert_awaited_once_with()
    transaction.commit.assert_awaited_once_with()
    sql = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert sql[0].startswith('CREATE TEMP VIEW "_ppbase_validate_')
    assert sql[2] == (
        'CREATE OR REPLACE VIEW "article_summary" AS SELECT 2 AS id'
    )


@pytest.mark.asyncio
async def test_invalid_view_rolls_back_only_its_savepoint() -> None:
    connection = _connection()
    transaction = _nested_transaction(connection)
    connection.execute.side_effect = RuntimeError("syntax error at SELECT")

    with pytest.raises(ValueError, match="Invalid view query"):
        await validate_view_query(connection, "SELECT FROM")

    transaction.rollback.assert_awaited_once_with()
    connection.rollback.assert_not_awaited()


def test_managed_index_names_do_not_collide_after_postgres_truncation() -> None:
    shared_prefix = "a" * 59
    first = _collection(name=f"{shared_prefix}aaaa")
    second = _collection(name=f"{shared_prefix}bbbb")

    first_name = next(iter(_managed_indexes(first)))
    second_name = next(iter(_managed_indexes(second)))

    assert len(first_name.encode("utf-8")) <= 63
    assert len(second_name.encode("utf-8")) <= 63
    assert first_name != second_name


@pytest.mark.asyncio
async def test_update_never_drops_an_index_owned_by_another_table() -> None:
    connection = _connection()

    async def execute(statement, parameters=None):
        del parameters
        sql = str(statement)
        result = MagicMock()
        if "FROM pg_catalog.pg_class AS index_relation" in sql:
            result.one_or_none.return_value = (
                "i",
                "unrelated_collection",
                "public",
            )
        else:
            result.one_or_none.return_value = None
        return result

    connection.execute.side_effect = execute

    with pytest.raises(ValueError, match="unrelated_collection"):
        await update_collection_table(
            connection,
            _collection(name="old_articles"),
            _collection(name="new_articles"),
        )

    sql = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert not any(statement.startswith("DROP INDEX") for statement in sql)


@pytest.mark.asyncio
async def test_create_never_reuses_an_index_owned_by_another_table() -> None:
    connection = _connection()
    custom_statement = (
        'CREATE INDEX "shared_custom_index" '
        'ON "target_collection" ("created")'
    )

    async def execute(statement, parameters=None):
        result = MagicMock()
        if (
            "FROM pg_catalog.pg_class AS index_relation" in str(statement)
            and parameters == {"index_name": "shared_custom_index"}
        ):
            if "pg_get_indexdef" in str(statement):
                result.one_or_none.return_value = (
                    "i",
                    "unrelated_collection",
                    "public",
                    False,
                    True,
                    False,
                    False,
                    "btree",
                    ["created"],
                    [],
                    None,
                )
            else:
                result.one_or_none.return_value = (
                    "i",
                    "unrelated_collection",
                    "public",
                )
        else:
            result.one_or_none.return_value = None
        return result

    connection.execute.side_effect = execute

    with pytest.raises(ValueError, match="unrelated_collection"):
        await create_collection_table(
            connection,
            _collection(
                name="target_collection",
                indexes=[custom_statement],
            ),
        )

    sql = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert custom_statement not in sql


@pytest.mark.asyncio
async def test_collection_name_over_postgres_limit_is_rejected_before_ddl() -> None:
    connection = _connection()

    with pytest.raises(ValueError, match="63 bytes"):
        await create_collection_table(
            connection,
            _collection(name="a" * 64),
        )

    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_field_name_over_postgres_limit_is_rejected_before_ddl() -> None:
    connection = _connection()

    with pytest.raises(ValueError, match="Field name.*64 bytes"):
        await create_collection_table(
            connection,
            _collection(
                name="valid_collection",
                schema=[{
                    "id": "long_field",
                    "name": "f" * 64,
                    "type": "text",
                    "options": {},
                }],
            ),
        )

    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_custom_index_cannot_mask_a_managed_index() -> None:
    connection = _connection()

    with pytest.raises(ValueError, match="conflicts with a PPBase-managed index"):
        await create_collection_table(
            connection,
            _collection(
                name="articles",
                indexes=[
                    'CREATE INDEX "idx_articles_created" '
                    'ON "articles" ("updated")'
                ],
            ),
        )

    connection.execute.assert_not_awaited()
