from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _run(
    app_client: AsyncClient,
    admin_token: str,
    query: str,
    *,
    confirm: bool = False,
    **extra,
):
    return await app_client.post(
        "/api/sql/execute",
        json={"query": query, "confirm": confirm, **extra},
        headers={"Authorization": admin_token},
    )


@pytest.fixture
async def table(app_client: AsyncClient, admin_token: str):
    name = f"sqlc_{uuid.uuid4().hex[:8]}"
    resp = await _run(
        app_client,
        admin_token,
        f"CREATE TABLE {name} (id int primary key, title text, meta jsonb);"
        f"INSERT INTO {name} VALUES (1, 'a', '{{\"k\": 1}}'), (2, 'b', null)",
        confirm=True,
    )
    assert resp.status_code == 200, resp.text
    yield name
    await _run(app_client, admin_token, f"DROP TABLE IF EXISTS {name}", confirm=True)


async def _count(app_client: AsyncClient, admin_token: str, table: str) -> int:
    resp = await _run(app_client, admin_token, f"SELECT count(*) AS n FROM {table}")
    assert resp.status_code == 200, resp.text
    return resp.json()["statements"][0]["rows"][0][0]


async def test_requires_admin(app_client: AsyncClient):
    resp = await app_client.post("/api/sql/execute", json={"query": "SELECT 1"})
    assert resp.status_code == 401


async def test_select_runs_without_confirmation(app_client, admin_token, table):
    resp = await _run(app_client, admin_token, f"SELECT id, title, meta FROM {table} ORDER BY id")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requiresConfirmation"] is False
    assert body["executed"] is True
    assert body["committed"] is False
    (stmt,) = body["statements"]
    assert [c["name"] for c in stmt["columns"]] == ["id", "title", "meta"]
    assert stmt["rows"] == [[1, "a", {"k": 1}], [2, "b", None]]
    assert stmt["status"] == "SELECT 2"


async def test_row_limit_truncates(app_client, admin_token):
    resp = await _run(app_client, admin_token, "SELECT generate_series(1, 50)", maxRows=10)
    (stmt,) = resp.json()["statements"]
    assert stmt["rowCount"] == 10
    assert stmt["truncated"] is True


async def test_delete_requires_confirmation_and_is_not_executed(app_client, admin_token, table):
    resp = await _run(app_client, admin_token, f"SELECT 1; DELETE FROM {table}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["requiresConfirmation"] is True
    assert body["executed"] is False
    kinds = [(s["command"], s["kind"], s["mutating"]) for s in body["statements"]]
    assert kinds == [("SELECT", "read", False), ("DELETE", "delete", True)]
    assert "without a WHERE" in body["statements"][1]["warning"]
    assert await _count(app_client, admin_token, table) == 2


async def test_confirmed_write_is_committed(app_client, admin_token, table):
    resp = await _run(
        app_client,
        admin_token,
        f"UPDATE {table} SET title = 'z' WHERE id = 1; DELETE FROM {table} WHERE id = 2",
        confirm=True,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["committed"] is True
    assert [s["status"] for s in body["statements"]] == ["UPDATE 1", "DELETE 1"]
    assert [s["rowCount"] for s in body["statements"]] == [1, 1]
    assert await _count(app_client, admin_token, table) == 1


async def test_hidden_write_is_caught_by_read_only_transaction(app_client, admin_token, table):
    fn = f"{table}_wipe"
    resp = await _run(
        app_client,
        admin_token,
        f"CREATE FUNCTION {fn}() RETURNS int LANGUAGE sql AS "
        f"$$ DELETE FROM {table}; SELECT 1 $$",
        confirm=True,
    )
    assert resp.status_code == 200, resp.text

    resp = await _run(app_client, admin_token, f"SELECT {fn}()")
    body = resp.json()
    assert body["requiresConfirmation"] is True
    (stmt,) = body["statements"]
    assert stmt["mutating"] is True
    assert "PostgreSQL detected" in stmt["warning"]
    assert await _count(app_client, admin_token, table) == 2

    await _run(app_client, admin_token, f"DROP FUNCTION {fn}()", confirm=True)


async def test_error_rolls_back_whole_script(app_client, admin_token, table):
    resp = await _run(
        app_client,
        admin_token,
        f"DELETE FROM {table} WHERE id = 1; SELECT * FROM missing_table_xyz",
        confirm=True,
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "missing_table_xyz" in body["message"]
    assert body["data"]["statementIndex"] == 1
    assert body["data"]["sqlstate"] == "42P01"
    assert await _count(app_client, admin_token, table) == 2


async def test_transaction_control_is_rejected(app_client, admin_token):
    resp = await _run(app_client, admin_token, "BEGIN; SELECT 1; COMMIT")
    assert resp.status_code == 400
    assert "single transaction" in resp.json()["message"]


async def test_session_state_does_not_leak_into_pool(app_client, admin_token):
    resp = await _run(app_client, admin_token, "SET application_name = 'sql-console-leak'")
    assert resp.status_code == 200, resp.text
    resp = await _run(app_client, admin_token, "SHOW application_name", confirm=True)
    assert resp.json()["statements"][0]["rows"] != [["sql-console-leak"]]
    # Regular API traffic still works on the shared pool.
    health = await app_client.get("/api/health")
    assert health.status_code == 200


async def test_empty_query(app_client, admin_token):
    resp = await _run(app_client, admin_token, "  -- nothing  ")
    assert resp.status_code == 400
