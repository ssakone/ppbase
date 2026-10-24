"""Paired PocketBase 0.22 / PPBase probe for hidden auth fields.

PocketBase 0.22.34 resolves only ``id``, ``created``, ``updated``,
``username``, ``verified``, ``emailVisibility``, ``email`` and the schema
fields of an auth collection (``resolvableSystemFieldNames``,
resolvers/record_field_resolve_runner.go l.718-732): ``passwordHash``,
``tokenKey``, ``lastResetSentAt``... are unknown fields (400), and ``email``
gets ``AND emailVisibility = TRUE`` unless hidden fields are allowed (l.356-374):
admins for a list, always for view/update/delete rules and ``@collection``/
``@request`` paths, never for a realtime subscription filter
(apis/realtime.go l.555-590).  PPBase stores these values in its own columns
(``password_hash``, ``token_key``, ``email_visibility``).

A system field never takes a modifier (``email:lower``, ``email:length``
and ``id:length`` are unknown fields, l.361-395) and a dotted path must start
with a relation, a back relation or a JSON field, so ``ph_users.password_hash``
is a 400, not a read of the current table's column.

The probe runs the same HTTP scenarios for four callers (guest, ordinary
user, owner of the hidden-email record, admin) against an official
PocketBase 0.22 binary and against PPBase: list filters and sorts; expand of
relations whose target viewRule is nil or requires auth (expand keys and
names only); realtime SSE subscriptions on ``ph_users/*`` (client filters,
the admin-only ``@collection.``/``@request.`` check on filter and sort, and
the email of each received record, with ``manageRule`` null then set to the
owner); and rule validation at collection save.  It writes
``pocketbase.json``, ``ppbase.json`` and ``diff.json`` in ``--out``.
Differences listed in ``KNOWN_DIVERGENCES`` are reported separately.

Only synthetic records are created.  Both servers listen on random loopback
ports and are always stopped; PPBase uses ``--database-url``, which must point
to an empty, disposable PostgreSQL database.

Usage (from the repository root)::

    PYTHONPATH=. python tests/pocketbase_parity/hidden_fields_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --out /tmp/hidden-fields-probe
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
import urllib.parse
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
USERS = {
    # name: (emailVisibility, verified)
    "vis": (True, False),
    "hid": (False, True),
    "other": (False, False),
}
CALLERS = ("guest", "user", "owner", "admin")

# PocketBase validates rules when a collection is saved
# (forms/collection_upsert.go l.372-391); PPBase does not.
KNOWN_DIVERGENCES = {
    "rule_save.tokenKey": "PPBase saves a rule PocketBase rejects at save time",
    "rule_save.password_hash": "PPBase saves a rule PocketBase rejects at save time",
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


def number_field(name: str) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "number",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {"min": None, "max": None, "noDecimal": False},
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


def _error(response: httpx.Response) -> list[Any]:
    body = _json(response)
    return [response.status_code, body.get("message"), body.get("data")]


async def _collection(
    client: httpx.AsyncClient, admin: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    payload = {**PUBLIC, "type": "base", **body}
    response = await client.post("/api/collections", headers=admin, json=payload)
    if response.status_code != 200:
        raise RuntimeError(
            f"collection {body.get('name')} failed: {response.text[:400]}"
        )
    return response.json()


async def _patch_collection(
    client: httpx.AsyncClient, admin: dict[str, str], coll: dict[str, Any], extra: list
) -> dict[str, Any]:
    response = await client.patch(
        f"/api/collections/{coll['id']}",
        headers=admin,
        json={"schema": [*coll["schema"], *extra]},
    )
    if response.status_code != 200:
        raise RuntimeError(f"patch {coll['name']} failed: {response.text[:400]}")
    return response.json()


async def _create(
    client: httpx.AsyncClient,
    name: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/collections/{name}/records", json=payload, headers=headers
    )
    if response.status_code != 200:
        raise RuntimeError(f"create {name} failed: {response.text[:300]}")
    return response.json()


async def _names(
    client: httpx.AsyncClient,
    name: str,
    headers: dict[str, str],
    **params: Any,
) -> Any:
    query = {"perPage": 200, **params}
    query.setdefault("sort", "name")
    response = await client.get(
        f"/api/collections/{name}/records", params=query, headers=headers
    )
    if response.status_code != 200:
        return _error(response)
    names = [item.get("name") for item in response.json()["items"]]
    return names if "sort" in params else sorted(names)


async def dataset(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, Any]:
    users = await _collection(
        client,
        admin,
        {
            "name": "ph_users",
            "type": "auth",
            "schema": [text_field("name"), number_field("counter")],
            "options": AUTH_OPTIONS,
            "updateRule": None,
            "deleteRule": None,
        },
    )
    users = await _patch_collection(
        client,
        admin,
        users,
        [
            relation_field("friend", users["id"]),
            relation_field("friends", users["id"], 5),
        ],
    )
    ids: dict[str, str] = {}
    for name, (visible, verified) in USERS.items():
        # emailVisibility and verified are managed auth fields: only an admin
        # or a manager may set them, so the seed records are created as admin.
        record = await _create(
            client,
            "ph_users",
            {
                "username": f"ph{name}",
                "email": f"{name}@example.invalid",
                "emailVisibility": visible,
                "verified": verified,
                "password": USER_PASSWORD,
                "passwordConfirm": USER_PASSWORD,
                "name": name,
            },
            headers=admin,
        )
        ids[name] = record["id"]
    for name, friend, friends in (
        ("vis", "hid", ["vis"]),
        ("hid", "vis", ["hid"]),
        ("other", "", ["vis", "hid"]),
    ):
        response = await client.patch(
            f"/api/collections/ph_users/records/{ids[name]}",
            headers=admin,
            json={"friend": ids.get(friend, ""), "friends": [ids[f] for f in friends]},
        )
        response.raise_for_status()

    secret = await _collection(
        client,
        admin,
        {
            "name": "ph_secret",
            "schema": [text_field("name")],
            "listRule": None,
            "viewRule": None,
        },
    )
    guarded = await _collection(
        client,
        admin,
        {
            "name": "ph_guarded",
            "schema": [text_field("name")],
            "viewRule": '@request.auth.id != ""',
        },
    )
    s1 = await _create(client, "ph_secret", {"name": "s1"}, headers=admin)
    g1 = await _create(client, "ph_guarded", {"name": "g1"})
    await _collection(
        client,
        admin,
        {
            "name": "ph_posts",
            "schema": [
                text_field("name"),
                relation_field("author", users["id"]),
                relation_field("authors", users["id"], 5),
                relation_field("secret", secret["id"]),
                relation_field("guarded", guarded["id"]),
            ],
        },
    )
    for name, payload in (
        (
            "p_vis",
            {
                "author": ids["vis"],
                "authors": [ids["vis"]],
                "secret": s1["id"],
                "guarded": g1["id"],
            },
        ),
        ("p_hid", {"author": ids["hid"], "authors": [ids["hid"], ids["vis"]]}),
        ("p_none", {}),
    ):
        await _create(client, "ph_posts", {"name": name, **payload})

    await _collection(
        client,
        admin,
        {
            "name": "ph_members",
            "type": "auth",
            "schema": [text_field("name"), number_field("counter")],
            "options": AUTH_OPTIONS,
            "listRule": 'email ~ "example"',
            "viewRule": 'email ~ "example"',
        },
    )
    for name, visible in (("m_vis", True), ("m_hid", False)):
        record = await _create(
            client,
            "ph_members",
            {
                "username": f"ph{name.replace('_', '')}",
                "email": f"{name.replace('_', '')}@example.invalid",
                "emailVisibility": visible,
                "password": USER_PASSWORD,
                "passwordConfirm": USER_PASSWORD,
                "name": name,
            },
            headers=admin,
        )
        ids[name] = record["id"]
    return ids


async def caller_headers(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> dict[str, dict[str, str]]:
    headers: dict[str, dict[str, str]] = {"guest": {}, "admin": admin}
    for caller, user in (("user", "other"), ("owner", "hid")):
        response = await client.post(
            "/api/collections/ph_users/auth-with-password",
            json={"identity": f"{user}@example.invalid", "password": USER_PASSWORD},
        )
        response.raise_for_status()
        headers[caller] = {"Authorization": response.json()["token"]}
    return headers


USER_FILTERS = {
    "id": 'id != ""',
    "password_hash": 'password_hash != ""',
    "passwordHash": 'passwordHash != ""',
    "password": 'password != ""',
    "token_key": 'token_key != ""',
    "tokenKey": 'tokenKey != ""',
    "email_visibility": "email_visibility = false",
    "emailVisibility_false": "emailVisibility = false",
    "emailVisibility_true": "emailVisibility = true",
    "verified": "verified = true",
    "lastResetSentAt": 'lastResetSentAt = ""',
    "last_reset_sent_at": 'last_reset_sent_at = ""',
    "lastVerificationSentAt": 'lastVerificationSentAt = ""',
    "email_like": 'email ~ "example"',
    "email_hid": 'email = "hid@example.invalid"',
    "email_not_empty": 'email != ""',
    "email_empty": 'email = ""',
    "email_or_name": 'email = "hid@example.invalid" || name = "vis"',
    "token_key_length": "token_key:length > 0",
    "friend_email": 'friend.email ~ "example"',
    "friend_email_hid": 'friend.email = "hid@example.invalid"',
    "friend_token_key": 'friend.token_key != ""',
    "friend_tokenKey": 'friend.tokenKey != ""',
    "friend_password_hash": 'friend.password_hash != ""',
    "friends_email": 'friends.email ~ "example"',
    "friends_any_email_hid": 'friends.email ?= "hid@example.invalid"',
    "back_email": 'ph_users_via_friend.email ~ "example"',
    "collection_email_hid": '@collection.ph_users.email ?= "hid@example.invalid"',
    "collection_token_key": '@collection.ph_users.token_key ?!= ""',
    "collection_tokenKey": '@collection.ph_users.tokenKey ?!= ""',
    "request_email": "@request.auth.email = email",
    # Table-qualified self references and other unresolved dotted paths.
    "qualified_password_hash": 'ph_users.password_hash ~ "$2"',
    "qualified_token_key": 'ph_users.token_key != ""',
    "qualified_email_hid": 'ph_users.email = "hid@example.invalid"',
    "qualified_name": 'ph_users.name != ""',
    "scalar_as_relation": "password_hash.x = 1",
    "text_as_relation": 'name.x = "a"',
    "other_table": 'ph_posts.name != ""',
    # Modifiers on system fields (email:lower used to drop the guard).
    "email_lower_hid": 'email:lower = "hid@example.invalid"',
    "email_lower_like": 'email:lower ~ "hid"',
    "email_length": "email:length > 3",
    "email_isset": "email:isset = true",
    "email_each": 'email:each = "x"',
    "emailVisibility_isset": "emailVisibility:isset = true",
    "verified_length": "verified:length > 0",
    "id_length": "id:length > 0",
    "created_isset": "created:isset = true",
    "friend_email_lower": 'friend.email:lower = "hid@example.invalid"',
    "friend_email_length": "friend.email:length > 3",
    "collection_email_length": "@collection.ph_users.email:length > 3",
}

POST_FILTERS = {
    "author_email": 'author.email ~ "example"',
    "author_email_hid": 'author.email = "hid@example.invalid"',
    "author_password_hash": 'author.password_hash != ""',
    "author_passwordHash": 'author.passwordHash != ""',
    "author_token_key": 'author.token_key != ""',
    "authors_email": 'authors.email ~ "example"',
    "authors_any_email_hid": 'authors.email ?= "hid@example.invalid"',
    "authors_email_visibility": "authors.email_visibility ?= false",
    "author_email_lower": 'author.email:lower = "hid@example.invalid"',
    "qualified_users_hash": 'ph_users.password_hash != ""',
}

USER_SORTS = {
    "email": "email,name",
    "-email": "-email,name",
    "emailVisibility": "emailVisibility,name",
    "verified": "-verified,name",
    "password_hash": "password_hash",
    "passwordHash": "passwordHash",
    "token_key": "token_key",
    "tokenKey": "tokenKey",
    "email_visibility": "email_visibility",
    "lastResetSentAt": "lastResetSentAt",
    "qualified_email": "ph_users.email",
    "email_lower": "email:lower",
}


async def list_scenarios(
    client: httpx.AsyncClient, headers: dict[str, dict[str, str]]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for caller in CALLERS:
        h = headers[caller]
        out[caller] = {
            "users": {
                k: await _names(client, "ph_users", h, filter=v)
                for k, v in USER_FILTERS.items()
            },
            "posts": {
                k: await _names(client, "ph_posts", h, filter=v)
                for k, v in POST_FILTERS.items()
            },
            "sort": {
                k: await _names(client, "ph_users", h, sort=v)
                for k, v in USER_SORTS.items()
            },
            "members_list": await _names(client, "ph_members", h),
            "members_filter_email": await _names(
                client, "ph_members", h, filter='email ~ "mhid"'
            ),
        }
    return out


async def rule_save_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
    out: dict[str, Any] = {}
    for key, rule in (
        ("tokenKey", 'author.tokenKey != ""'),
        ("password_hash", 'author.password_hash != ""'),
        ("email", 'author.email != ""'),
    ):
        response = await client.post(
            "/api/collections",
            headers=admin,
            json={
                **PUBLIC,
                "name": f"ph_rule_{key.lower()}",
                "type": "base",
                "schema": [
                    text_field("name"),
                    relation_field("author", (await _collection_id(client, admin))),
                ],
                "listRule": rule,
            },
        )
        out[key] = response.status_code
    return out


async def _collection_id(client: httpx.AsyncClient, admin: dict[str, str]) -> str:
    response = await client.get("/api/collections/ph_users", headers=admin)
    return response.json()["id"]


async def expand_scenarios(
    client: httpx.AsyncClient, headers: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """Which relations are expanded, per caller (target viewRule).

    ``ph_secret`` has a nil viewRule (admin only), ``ph_guarded`` requires an
    authenticated caller (apis/record_helpers.go l.177-210).  Only the expand
    keys and the expanded names are compared: the serialization of expanded
    auth records is a separate, documented gap.
    """
    out: dict[str, Any] = {}
    for caller in CALLERS:
        response = await client.get(
            "/api/collections/ph_posts/records",
            headers=headers[caller],
            params={"expand": "secret,guarded,author", "sort": "name"},
        )
        if response.status_code != 200:
            out[caller] = _error(response)
            continue
        rows: dict[str, Any] = {}
        for item in response.json()["items"]:
            expand = item.get("expand")
            rows[item["name"]] = (
                None
                if expand is None
                else {key: value.get("name") for key, value in sorted(expand.items())}
            )
        out[caller] = rows
    return out


# Realtime subscriptions on ph_users/* (one SSE client per caller).
REALTIME_OPTIONS = {
    "plain": {},
    "filter_name": {"filter": 'name != ""'},
    "filter_token_key": {"filter": 'token_key != ""'},
    "filter_qualified_hash": {"filter": 'ph_users.password_hash ~ "$2"'},
    "filter_email_hid": {"filter": 'email = "hid@example.invalid"'},
    "filter_email_vis": {"filter": 'email = "vis@example.invalid"'},
    "filter_email_lower": {"filter": 'email:lower ~ "hid"'},
    "filter_collection": {"filter": "@collection.ph_users.id ?= id"},
    "sort_only_request": {"sort": "@request.method"},
    "filter_and_sort_request": {"filter": 'name != ""', "sort": "@request.method"},
}


def _topic(options: dict[str, Any]) -> str:
    if not options:
        return "ph_users/*"
    payload = json.dumps({"query": options, "headers": {}}, separators=(",", ":"))
    return "ph_users/*?options=" + urllib.parse.quote(payload, safe="")


async def _sse_reader(
    client: httpx.AsyncClient, ready: asyncio.Future, sink: list[dict[str, Any]]
) -> None:
    async with client.stream(
        "GET", "/api/realtime", timeout=httpx.Timeout(None)
    ) as response:
        event: dict[str, str] = {}
        async for line in response.aiter_lines():
            if line:
                key, _, value = line.partition(":")
                event[key] = value.removeprefix(" ")
                continue
            if event.get("event") == "PB_CONNECT" and not ready.done():
                ready.set_result(json.loads(event["data"])["clientId"])
            elif event.get("data"):
                sink.append(event)
            event = {}


async def realtime_round(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    headers: dict[str, dict[str, str]],
    ids: dict[str, str],
    tick: int,
) -> dict[str, Any]:
    topics = {_topic(options): key for key, options in REALTIME_OPTIONS.items()}
    sinks: dict[str, list[dict[str, Any]]] = {}
    tasks: list[asyncio.Task] = []
    try:
        for caller in CALLERS:
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            sinks[caller] = []
            tasks.append(asyncio.create_task(_sse_reader(client, ready, sinks[caller])))
            client_id = await asyncio.wait_for(ready, timeout=15)
            response = await client.post(
                "/api/realtime",
                headers=headers[caller],
                json={"clientId": client_id, "subscriptions": list(topics)},
            )
            if response.status_code not in (200, 204):
                raise RuntimeError(f"subscribe {caller}: {response.text[:200]}")
        await asyncio.sleep(0.5)
        for name in ("vis", "hid"):
            response = await client.patch(
                f"/api/collections/ph_users/records/{ids[name]}",
                headers=admin,
                json={"counter": tick},
            )
            response.raise_for_status()
            await asyncio.sleep(0.3)
        await asyncio.sleep(1.5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    out: dict[str, Any] = {}
    for caller, events in sinks.items():
        seen: dict[str, Any] = {key: {} for key in REALTIME_OPTIONS}
        for event in events:
            key = topics.get(event.get("event", ""))
            if key is None:
                continue
            record = json.loads(event["data"]).get("record", {})
            seen[key][record.get("name")] = record.get("email", "<absent>")
        out[caller] = seen
    return out


async def realtime_scenarios(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    headers: dict[str, dict[str, str]],
    ids: dict[str, str],
) -> dict[str, Any]:
    """Client filters, the admin-only filter check and the email payload.

    PocketBase 0.22.34 sends a hidden email only to the record owner whose
    manageRule check passes (apis/realtime.go l.439-447), so the second round
    sets ``manageRule`` to the owner.
    """
    out = {"manage_null": await realtime_round(client, admin, headers, ids, 1)}
    collection = (await client.get("/api/collections/ph_users", headers=admin)).json()
    response = await client.patch(
        f"/api/collections/{collection['id']}",
        headers=admin,
        json={"options": {**AUTH_OPTIONS, "manageRule": "id = @request.auth.id"}},
    )
    response.raise_for_status()
    out["manage_owner"] = await realtime_round(client, admin, headers, ids, 2)
    response = await client.patch(
        f"/api/collections/{collection['id']}",
        headers=admin,
        json={"options": AUTH_OPTIONS},
    )
    response.raise_for_status()
    return out


async def run_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
    ids = await dataset(client, admin)
    headers = await caller_headers(client, admin)
    return {
        "lists": await list_scenarios(client, headers),
        "expand": await expand_scenarios(client, headers),
        "realtime": await realtime_scenarios(client, admin, headers, ids),
        "rule_save": await rule_save_scenarios(client, admin),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_health(client: httpx.AsyncClient, what: str) -> None:
    deadline = time.monotonic() + 60
    while True:
        try:
            if (await client.get("/api/health")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            raise RuntimeError(f"{what} did not start")
        await asyncio.sleep(0.2)


@contextlib.asynccontextmanager
async def pocketbase_client(binary: Path) -> AsyncIterator[httpx.AsyncClient]:
    with tempfile.TemporaryDirectory(prefix="ppbase-pb-hidden-probe-") as root:
        flags = [
            "--dir",
            str(Path(root) / "pb_data"),
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
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=30
            ) as client:
                await _wait_health(client, "PocketBase")
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
    import uvicorn
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ppbase.app import create_app
    from ppbase.config import Settings
    from ppbase.db.engine import get_engine
    from ppbase.db.system_tables import ParamRecord

    with tempfile.TemporaryDirectory(prefix="ppbase-hidden-probe-") as root:
        settings = Settings(
            database_url=database_url,
            jwt_secret="probe-secret-key-for-hidden-auth-fields-000001",
            auto_migrate=False,
            apply_migrations_on_start=False,
            generate_migrations=False,
            data_dir=str(Path(root) / "data"),
            migrations_dir=str(Path(root) / "migrations"),
            hooks_dir=str(Path(root) / "hooks"),
            log_level="WARNING",
        )
        port = _free_port()
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings),
                host="127.0.0.1",
                port=port,
                log_level="warning",
                lifespan="on",
            )
        )
        task = asyncio.create_task(server.serve())
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=30
            ) as client:
                await _wait_health(client, "PPBase")
                factory = async_sessionmaker(
                    bind=get_engine(), class_=AsyncSession, expire_on_commit=False
                )
                async with factory() as session:
                    row = (
                        (
                            await session.execute(
                                select(ParamRecord).where(
                                    ParamRecord.key == "_setup_token"
                                )
                            )
                        )
                        .scalars()
                        .first()
                    )
                setup = (
                    row.value.get("token")
                    if row and isinstance(row.value, dict)
                    else None
                )
                body = {
                    "email": ADMIN_EMAIL,
                    "password": ADMIN_PASSWORD,
                    "passwordConfirm": ADMIN_PASSWORD,
                }
                if setup:
                    body["setupToken"] = setup
                response = await client.post("/api/admins/init", json=body)
                response.raise_for_status()
                return await run_scenarios(
                    client, {"Authorization": response.json()["token"]}
                )
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=30)


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


def _ppbase_module() -> str:
    import ppbase

    return str(Path(ppbase.__file__).resolve().parent)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skip-pocketbase", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    reference = args.out / "pocketbase.json"
    if args.skip_pocketbase and reference.exists():
        pocketbase = json.loads(reference.read_text())
    else:
        pocketbase = asyncio.run(run_pocketbase(args.pocketbase))
        reference.write_text(json.dumps(pocketbase, indent=1, sort_keys=True) + "\n")
    ppbase = asyncio.run(run_ppbase(args.database_url))
    differences = _diff(pocketbase, ppbase)
    for row in differences:
        row["known"] = KNOWN_DIVERGENCES.get(row["path"])
    for name, payload in (("ppbase.json", ppbase), ("diff.json", differences)):
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
                # The PPBase actually exercised (run with PYTHONPATH=<checkout>,
                # otherwise the installed package is imported).
                "ppbase_module": _ppbase_module(),
            },
            indent=1,
        )
    )
    for row in differences:
        print(json.dumps(row, sort_keys=True))
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
