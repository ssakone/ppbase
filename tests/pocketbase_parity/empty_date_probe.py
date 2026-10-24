"""Paired PocketBase 0.22 / PPBase probe for empty date values.

PocketBase stores an empty date as ``''`` in a ``TEXT DEFAULT '' NOT NULL``
column and serves it as ``""`` (v0.22.34 ``tools/types/datetime.go``
l.47-58, ``models/schema/schema_field.go`` l.163); PPBase stores it as
``NULL`` in a ``TIMESTAMPTZ NULL`` column.  This probe runs the same HTTP
scenarios (writes, reads, expand, auth records, views, filters and sorts)
against an official PocketBase 0.22 binary and against PPBase, then writes
``pocketbase.json``, ``ppbase.json`` and ``diff.json`` in ``--out``.  It
backs the expected values of ``tests/integration/test_empty_dates.py``.

Differences listed in ``KNOWN_DIVERGENCES`` are reported separately: they
are documented in ``docs/empty-dates.md`` and are not regressions.

Only synthetic records are created.  PocketBase runs in a temporary data
directory on a random loopback port and is always stopped; PPBase runs
in-process (ASGI) on ``--database-url``, which must point to an empty,
disposable PostgreSQL database.

Usage (from the repository root)::

    python tests/pocketbase_parity/empty_date_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --out /tmp/empty-date-probe
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

ADMIN_EMAIL = "probe-admin@example.invalid"
ADMIN_PASSWORD = "probe-Password-123"
USER_PASSWORD = "probe-User-Password-1"
PUBLIC = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}
AUTH_OPTIONS = {
    "allowEmailAuth": True,
    "allowOAuth2Auth": False,
    "allowUsernameAuth": True,
    "exceptEmailDomains": None,
    "manageRule": None,
    "minPasswordLength": 8,
    "onlyEmailDomains": None,
    "onlyVerified": False,
    "requireEmail": False,
}
MID = "2024-06-01 00:00:00.000Z"

# PPBase cannot tell a SQL NULL of a view column (LEFT JOIN without match)
# from an empty date: both are NULL in PostgreSQL, while SQLite keeps NULL
# and '' apart.  Only the operators where SQLite NULL and '' differ show it.
KNOWN_DIVERGENCES = {
    "views.filters.td_lt_mid": "view LEFT JOIN NULL compared with <",
    "views.filters.td_not_like": "view LEFT JOIN NULL compared with !~",
    "views.sort.td_asc": "view LEFT JOIN NULL sorts before '' in SQLite",
    # PocketBase 0.22 zeroes an unparseable date (cast.ToTime) and stores
    # ''; PPBase rejects it with a validation error.  Out of this probe's
    # empty-value scope, recorded for the decision log.
    "write.invalid_string": "unparseable date: PocketBase stores '', PPBase 400",
}


def text_field(name: str) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "text",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {"min": None, "max": None, "pattern": ""},
    }


def date_field(name: str) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "date",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {"min": "", "max": ""},
    }


def relation_field(
    name: str, collection_id: str, max_select: int = 1
) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "relation",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {
            "collectionId": collection_id,
            "cascadeDelete": False,
            "minSelect": None,
            "maxSelect": max_select,
            "displayFields": None,
        },
    }


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"_text": response.text[:200]}


async def _collection(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    body: dict[str, Any],
) -> dict[str, Any]:
    payload = {**PUBLIC, "type": "base", **body}
    if payload["type"] == "view":
        payload.update(createRule=None, updateRule=None, deleteRule=None)
    response = await client.post("/api/collections", headers=admin, json=payload)
    if response.status_code != 200:
        raise RuntimeError(
            f"collection {body.get('name')} failed: {response.text[:400]}"
        )
    return response.json()


async def _create(
    client: httpx.AsyncClient,
    name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(f"/api/collections/{name}/records", json=payload)
    if response.status_code != 200:
        raise RuntimeError(f"create {name} failed: {response.text[:300]}")
    return response.json()


async def _titles(
    client: httpx.AsyncClient,
    name: str,
    *,
    filter_expr: str | None = None,
    sort: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    params: dict[str, Any] = {"perPage": 200}
    if filter_expr is not None:
        params["filter"] = filter_expr
    params["sort"] = sort or "title"
    response = await client.get(
        f"/api/collections/{name}/records", params=params, headers=headers
    )
    if response.status_code != 200:
        return {"status": response.status_code}
    titles = [item["title"] for item in response.json()["items"]]
    return titles if sort else sorted(titles)


async def write_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
    """Values returned by create and update for empty and non-empty dates."""
    await _collection(
        client,
        admin,
        {"name": "pd_writes", "schema": [text_field("title"), date_field("d")]},
    )
    out: dict[str, Any] = {}
    for key, payload in (
        ("empty_string", {"d": ""}),
        ("null", {"d": None}),
        ("absent", {}),
        ("blank", {"d": "   "}),
        ("value", {"d": "2024-02-03 04:05:06.789Z"}),
        ("value_iso_t", {"d": "2024-02-03T04:05:06Z"}),
        ("invalid_string", {"d": "not-a-date"}),
    ):
        response = await client.post(
            "/api/collections/pd_writes/records", json={"title": key, **payload}
        )
        body = _json(response)
        out[key] = [response.status_code, body.get("d", "<missing>")]
    response = await client.post(
        "/api/collections/pd_writes/records",
        files=[("title", (None, "multipart")), ("d", (None, ""))],
    )
    out["multipart_empty"] = [response.status_code, _json(response).get("d")]

    record = await _create(
        client, "pd_writes", {"title": "upd", "d": "2024-02-03 04:05:06.789Z"}
    )
    updates = []
    for payload in ({"d": ""}, {"d": "2024-05-06 07:08:09.000Z"}, {"d": None}, {}):
        response = await client.patch(
            f"/api/collections/pd_writes/records/{record['id']}", json=payload
        )
        updates.append([response.status_code, _json(response).get("d")])
    out["updates"] = updates
    response = await client.get(f"/api/collections/pd_writes/records/{record['id']}")
    out["view_after_updates"] = _json(response).get("d")
    return out


async def dataset(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, str]:
    targets = await _collection(
        client,
        admin,
        {"name": "pd_targets", "schema": [text_field("name"), date_field("td")]},
    )
    await _collection(
        client,
        admin,
        {
            "name": "pd_dates",
            "schema": [
                text_field("title"),
                date_field("d"),
                date_field("e"),
                relation_field("other", targets["id"]),
            ],
        },
    )
    t_empty = (await _create(client, "pd_targets", {"name": "t_empty", "td": ""}))["id"]
    t_full = (
        await _create(
            client, "pd_targets", {"name": "t_full", "td": "2024-03-01 10:00:00.000Z"}
        )
    )["id"]
    rows = (
        ("a_empty", {"d": "", "e": "", "other": t_empty}),
        ("b_null", {"d": None, "e": "2024-01-01 00:00:00.000Z", "other": t_full}),
        ("c_absent", {"other": t_empty}),
        ("d_early", {"d": "2023-06-15 08:30:00.000Z", "e": "", "other": t_full}),
        ("e_mid", {"d": MID, "e": "2023-01-01 00:00:00.000Z", "other": t_full}),
        (
            "f_late",
            {
                "d": "2025-01-01 12:00:00.123Z",
                "e": "2025-01-01 12:00:00.123Z",
                "other": t_empty,
            },
        ),
        ("g_orphan", {"d": ""}),
    )
    ids: dict[str, str] = {}
    for title, payload in rows:
        ids[title] = (await _create(client, "pd_dates", {"title": title, **payload}))[
            "id"
        ]
    await _collection(
        client,
        admin,
        {
            "name": "pd_multi",
            "schema": [text_field("title"), relation_field("others", targets["id"], 5)],
        },
    )
    for title, others in (
        ("m_both", [t_empty, t_full]),
        ("m_empty", [t_empty]),
        ("m_full", [t_full]),
        ("m_none", []),
    ):
        await _create(client, "pd_multi", {"title": title, "others": others})
    return ids


async def read_scenarios(client: httpx.AsyncClient, ids: dict[str, str]) -> Any:
    out: dict[str, Any] = {}
    response = await client.get(
        "/api/collections/pd_dates/records",
        params={"perPage": 200, "sort": "title", "expand": "other"},
    )
    out["list"] = {
        item["title"]: {
            "d": item["d"],
            "e": item["e"],
            "expand_td": (item.get("expand") or {}).get("other", {}).get("td"),
        }
        for item in response.json()["items"]
    }
    response = await client.get(f"/api/collections/pd_dates/records/{ids['a_empty']}")
    body = response.json()
    out["view_one"] = {"d": body["d"], "e": body["e"]}
    response = await client.get(
        f"/api/collections/pd_dates/records/{ids['a_empty']}",
        params={"fields": "d"},
    )
    out["view_fields_d"] = response.json()
    return out


FILTERS = {
    "eq_empty": 'd = ""',
    "eq_empty_single": "d = ''",
    "eq_null": "d = null",
    "neq_empty": 'd != ""',
    "neq_null": "d != null",
    "eq_value": f'd = "{MID}"',
    "neq_value": f'd != "{MID}"',
    "lt_value": f'd < "{MID}"',
    "lte_value": f'd <= "{MID}"',
    "gt_value": f'd > "{MID}"',
    "gte_value": f'd >= "{MID}"',
    "value_gt_field": f'"{MID}" > d',
    "value_lt_field": f'"{MID}" < d',
    "lt_empty": 'd < ""',
    "lte_empty": 'd <= ""',
    "gt_empty": 'd > ""',
    "gte_empty": 'd >= ""',
    "empty_lt_field": '"" < d',
    "empty_gte_field": '"" >= d',
    "lt_null": "d < null",
    "lte_null": "d <= null",
    "gt_null": "d > null",
    "gte_null": "d >= null",
    "lt_now": "d < @now",
    "gt_now": "d > @now",
    "field_eq": "d = e",
    "field_neq": "d != e",
    "field_lt": "d < e",
    "field_lte": "d <= e",
    "field_gt": "d > e",
    "field_gte": "d >= e",
    "like_year": 'd ~ "2024"',
    "not_like_year": 'd !~ "2024"',
    "like_empty": 'd ~ ""',
    "not_like_empty": 'd !~ ""',
    "like_null": "d ~ null",
    "not_like_null": "d !~ null",
    "like_full_value": f'd ~ "{MID}"',
    "and_range": f'd >= "2023-01-01 00:00:00.000Z" && d <= "{MID}"',
    "or_empty_or_late": 'd = "" || d > "2024-12-31 00:00:00.000Z"',
    "rel_eq_empty": 'other != "" && other.td = ""',
    "rel_neq_empty": 'other != "" && other.td != ""',
    "rel_lt_value": f'other != "" && other.td < "{MID}"',
    "rel_gt_value": 'other != "" && other.td > "2024-01-01 00:00:00.000Z"',
    "rel_not_like": 'other != "" && other.td !~ "2024"',
    # Without the ``other != ""`` guard: a missing target is SQL NULL.
    "rel_any_eq_empty": 'other.td = ""',
    "rel_any_neq_empty": 'other.td != ""',
    "rel_any_lt_value": f'other.td < "{MID}"',
    "rel_any_lte_empty": 'other.td <= ""',
    "rel_any_gte_empty": 'other.td >= ""',
    "rel_any_gt_empty": 'other.td > ""',
    "rel_any_not_like": 'other.td !~ "2024"',
    "rel_any_like": 'other.td ~ "2024"',
    "rel_vs_field_lt": "other.td < d",
    "rel_vs_field_lte": "other.td <= d",
    "rel_vs_field_gt": "other.td > d",
    "rel_vs_field_gte": "other.td >= d",
    "field_vs_rel_lt": "d < other.td",
}

SORTS = {
    "d_asc": "d,title",
    "d_desc": "-d,title",
    "d_desc_title_desc": "-d,-title",
    "e_asc_d_desc": "e,-d,title",
}


# Multiple relation leaves: PocketBase's multi-match needs every target.
MULTI_FILTERS = {
    "eq_empty": 'others.td = ""',
    "neq_empty": 'others.td != ""',
    "eq_value": 'others.td = "2024-03-01 10:00:00.000Z"',
    "neq_value": 'others.td != "2024-03-01 10:00:00.000Z"',
    "eq_null": "others.td = null",
    "lt_null": "others.td < null",
    "any_eq_empty": 'others.td ?= ""',
    "lt_value": f'others.td < "{MID}"',
    "any_lt_value": f'others.td ?< "{MID}"',
    "gt_value": 'others.td > "2024-01-01 00:00:00.000Z"',
    "any_gt_value": 'others.td ?> "2024-01-01 00:00:00.000Z"',
    "lte_empty": 'others.td <= ""',
    "gte_empty": 'others.td >= ""',
    "not_like": 'others.td !~ "2024"',
    "any_not_like": 'others.td ?!~ "2024"',
}

# ``@collection`` is admin-only in a request filter (403 otherwise) and is
# always a multi-match over every row of the collection.
COLLECTION_FILTERS = {
    "eq_empty_and_id": '@collection.pd_targets.td = "" && @collection.pd_targets.id = other',
    "any_eq_empty_and_id": '@collection.pd_targets.td ?= "" && @collection.pd_targets.id ?= other',
    "lt_value": f'@collection.pd_targets.td < "{MID}"',
    "any_lt_value": f'@collection.pd_targets.td ?< "{MID}"',
    "gt_value": '@collection.pd_targets.td > "2024-01-01 00:00:00.000Z"',
    "any_gt_value": '@collection.pd_targets.td ?> "2024-01-01 00:00:00.000Z"',
    "neq_empty": '@collection.pd_targets.td != ""',
    "any_neq_empty": '@collection.pd_targets.td ?!= ""',
    "lte_empty": '@collection.pd_targets.td <= ""',
    "gte_empty": '@collection.pd_targets.td >= ""',
    "not_like": '@collection.pd_targets.td !~ "2024"',
    "any_lt_field": "@collection.pd_targets.td ?< d",
}


async def filter_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
    out: dict[str, Any] = {
        "filters": {
            key: await _titles(client, "pd_dates", filter_expr=expression)
            for key, expression in FILTERS.items()
        },
        "multi_filters": {
            key: await _titles(client, "pd_multi", filter_expr=expression)
            for key, expression in MULTI_FILTERS.items()
        },
        "collection_filters": {
            key: await _titles(
                client, "pd_dates", filter_expr=expression, headers=admin
            )
            for key, expression in COLLECTION_FILTERS.items()
        },
        "collection_filter_anonymous": await _titles(
            client, "pd_dates", filter_expr=COLLECTION_FILTERS["lt_value"]
        ),
        "sort": {
            key: await _titles(client, "pd_dates", sort=sort)
            for key, sort in SORTS.items()
        },
    }
    return out


async def view_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
    out: dict[str, Any] = {}
    joined = await _collection(
        client,
        admin,
        {
            "name": "pd_view",
            "type": "view",
            "schema": [text_field("title"), date_field("d"), date_field("td")],
            "options": {
                "query": (
                    "SELECT p.id, p.title, p.d, t.td FROM pd_dates p "
                    "LEFT JOIN pd_targets t ON t.id = p.other"
                )
            },
        },
    )
    with_created = await _collection(
        client,
        admin,
        {
            "name": "pd_view_created",
            "type": "view",
            "schema": [text_field("title"), date_field("d")],
            "options": {
                "query": "SELECT p.id, p.created, p.title, p.d FROM pd_dates p"
            },
        },
    )
    out["schema"] = {
        "pd_view": {f["name"]: f["type"] for f in joined["schema"]},
        "pd_view_created": {f["name"]: f["type"] for f in with_created["schema"]},
    }
    response = await client.get(
        "/api/collections/pd_view/records", params={"perPage": 200, "sort": "title"}
    )
    items = response.json()["items"]
    out["keys"] = sorted(items[0])
    out["values"] = {item["title"]: [item["d"], item["td"]] for item in items}
    response = await client.get(f"/api/collections/pd_view/records/{items[0]['id']}")
    out["view_one_keys"] = sorted(response.json())
    response = await client.get(
        "/api/collections/pd_view_created/records",
        params={"perPage": 200, "sort": "title"},
    )
    created_items = response.json()["items"]
    out["created_keys"] = sorted(created_items[0])
    out["created_is_set"] = sorted(
        {bool(item.get("created")) for item in created_items}
    )
    out["filters"] = {
        "td_eq_empty": await _titles(client, "pd_view", filter_expr='td = ""'),
        "td_neq_empty": await _titles(client, "pd_view", filter_expr='td != ""'),
        "td_lt_mid": await _titles(client, "pd_view", filter_expr=f'td < "{MID}"'),
        "td_gt": await _titles(
            client, "pd_view", filter_expr='td > "2024-01-01 00:00:00.000Z"'
        ),
        "td_not_like": await _titles(client, "pd_view", filter_expr='td !~ "2024"'),
        "d_eq_empty": await _titles(client, "pd_view", filter_expr='d = ""'),
        "d_lt_mid": await _titles(client, "pd_view", filter_expr=f'd < "{MID}"'),
    }
    out["sort"] = {
        "td_asc": await _titles(client, "pd_view", sort="td,title"),
        "td_desc": await _titles(client, "pd_view", sort="-td,title"),
        "d_asc": await _titles(client, "pd_view", sort="d,title"),
        "d_desc": await _titles(client, "pd_view", sort="-d,title"),
    }
    return out


async def auth_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
    out: dict[str, Any] = {}
    await _collection(
        client,
        admin,
        {
            "name": "pd_people",
            "type": "auth",
            "schema": [text_field("title"), date_field("birth")],
            "options": AUTH_OPTIONS,
        },
    )
    created = await _create(
        client,
        "pd_people",
        {
            "username": "pdperson1",
            "email": "pdperson1@example.invalid",
            "password": USER_PASSWORD,
            "passwordConfirm": USER_PASSWORD,
            "title": "p1",
            "birth": "",
        },
    )
    out["create"] = created["birth"]
    response = await client.post(
        "/api/collections/pd_people/auth-with-password",
        json={"identity": "pdperson1@example.invalid", "password": USER_PASSWORD},
    )
    body = _json(response)
    out["auth_with_password"] = [
        response.status_code,
        (body.get("record") or {}).get("birth", body.get("message")),
    ]
    token = {"Authorization": str(body.get("token", ""))}
    response = await client.post(
        "/api/collections/pd_people/auth-refresh", headers=token
    )
    body = _json(response)
    out["auth_refresh"] = [
        response.status_code,
        (body.get("record") or {}).get("birth", body.get("message")),
    ]
    response = await client.patch(
        f"/api/collections/pd_people/records/{created['id']}",
        headers=token,
        json={"birth": None},
    )
    out["update_null"] = [response.status_code, _json(response).get("birth")]
    response = await client.get(
        "/api/collections/pd_people/records",
        params={"filter": 'birth = ""'},
        headers=admin,
    )
    out["filter_eq_empty"] = [
        response.status_code,
        _json(response).get("totalItems") if response.status_code == 200 else None,
    ]
    return out


async def run_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
    out: dict[str, Any] = {"write": await write_scenarios(client, admin)}
    ids = await dataset(client, admin)
    out["read"] = await read_scenarios(client, ids)
    out.update(await filter_scenarios(client, admin))
    out["views"] = await view_scenarios(client, admin)
    out["auth"] = await auth_scenarios(client, admin)
    return out


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def pocketbase_client(binary: Path) -> AsyncIterator[httpx.AsyncClient]:
    with tempfile.TemporaryDirectory(prefix="ppbase-pb-date-probe-") as root:
        data_dir = str(Path(root) / "pb_data")
        flags = [
            "--dir",
            data_dir,
            "--migrationsDir",
            str(Path(root) / "pb_migrations"),
            "--hooksDir",
            str(Path(root) / "pb_hooks"),
            "--automigrate=false",
        ]
        await asyncio.to_thread(
            subprocess.run,
            [str(binary), "migrate", "up", *flags],
            check=True,
            capture_output=True,
        )
        created = await asyncio.to_thread(
            subprocess.run,
            [str(binary), "admin", "create", ADMIN_EMAIL, ADMIN_PASSWORD, *flags],
            check=True,
            capture_output=True,
            text=True,
        )
        if "Successfully" not in created.stdout + created.stderr:
            raise RuntimeError(f"PocketBase admin creation failed: {created.stderr}")
        port = _free_port()
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "serve",
            "--http",
            f"127.0.0.1:{port}",
            "--publicDir",
            str(Path(root) / "pb_public"),
            *flags,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 30
            async with httpx.AsyncClient(base_url=base, timeout=30) as client:
                while True:
                    try:
                        if (await client.get("/api/health")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("PocketBase did not start")
                    await asyncio.sleep(0.2)
                yield client
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def run_pocketbase(binary: Path) -> dict[str, Any]:
    async with pocketbase_client(binary) as client:
        response = await client.post(
            "/api/admins/auth-with-password",
            json={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        response.raise_for_status()
        return await run_scenarios(client, {"Authorization": response.json()["token"]})


async def run_ppbase(database_url: str) -> dict[str, Any]:
    from httpx import ASGITransport
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ppbase.app import create_app
    from ppbase.config import Settings
    from ppbase.db.bootstrap import bootstrap_system_collections
    from ppbase.db.engine import close_engine, init_engine
    from ppbase.db.system_tables import CollectionRecord, create_system_tables
    from ppbase.services.admin_service import create_admin
    from ppbase.services.auth_service import create_admin_token

    secret = "probe-secret-key-for-empty-date-values-00000001"
    with tempfile.TemporaryDirectory(prefix="ppbase-date-probe-data-") as data_dir:
        settings = Settings(
            database_url=database_url,
            jwt_secret=secret,
            auto_migrate=False,
            data_dir=data_dir,
        )
        app = create_app(settings)
        engine = await init_engine(database_url, pool_size=5, max_overflow=5)
        try:
            await create_system_tables(engine)
            factory = async_sessionmaker(
                bind=engine, class_=AsyncSession, expire_on_commit=False
            )
            async with factory() as session, session.begin():
                await bootstrap_system_collections(session, engine)
            async with factory() as session:
                admin_record = await create_admin(session, ADMIN_EMAIL, ADMIN_PASSWORD)
                await session.commit()
                superusers = (
                    (
                        await session.execute(
                            select(CollectionRecord).where(
                                CollectionRecord.name == "_superusers"
                            )
                        )
                    )
                    .scalars()
                    .first()
                )

            class _TokenSettings:
                admin_token_duration = 86400

                def get_jwt_secret(self) -> str:
                    return secret

            token = create_admin_token(
                admin_record, _TokenSettings(), superusers_collection=superusers
            )
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver", timeout=60
            ) as client:
                return await run_scenarios(client, {"Authorization": token})
        finally:
            await close_engine()


def _diff(left: Any, right: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(left, dict) and isinstance(right, dict):
        rows: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            rows.extend(
                _diff(left.get(key), right.get(key), f"{path}.{key}" if path else key)
            )
        return rows
    if left != right:
        return [{"path": path, "pocketbase": left, "ppbase": right}]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    pocketbase = asyncio.run(run_pocketbase(args.pocketbase))
    (args.out / "pocketbase.json").write_text(
        json.dumps(pocketbase, indent=1, sort_keys=True) + "\n"
    )
    ppbase = asyncio.run(run_ppbase(args.database_url))
    differences = _diff(pocketbase, ppbase)
    for row in differences:
        row["known"] = KNOWN_DIVERGENCES.get(row["path"])
    for name, payload in (
        ("ppbase.json", ppbase),
        ("diff.json", differences),
    ):
        (args.out / name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n"
        )
    unexpected = [row for row in differences if not row["known"]]
    print(
        json.dumps(
            {
                "differences": len(differences),
                "known": len(differences) - len(unexpected),
                "unexpected": len(unexpected),
            },
            indent=1,
        )
    )
    for row in differences:
        print(json.dumps(row, sort_keys=True))
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
