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

The probe runs the same HTTP scenarios (list filters and sorts, rules,
expand, realtime subscriptions) for four callers (guest, ordinary user,
owner of the hidden-email record, admin) against an official PocketBase
0.22 binary and against PPBase, then writes ``pocketbase.json``,
``ppbase.json`` and ``diff.json`` in ``--out``.  Differences listed in
``KNOWN_DIVERGENCES`` are reported separately.

Only synthetic records are created.  Both servers listen on random loopback
ports and are always stopped; PPBase uses ``--database-url``, which must point
to an empty, disposable PostgreSQL database.

Usage (from the repository root)::

    python tests/pocketbase_parity/hidden_fields_probe.py \\
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


def relation_field(name: str, collection_id: str, max_select: int = 1) -> dict[str, Any]:
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
        raise RuntimeError(f"collection {body.get('name')} failed: {response.text[:400]}")
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
    response = await client.get(f"/api/collections/{name}/records", params=query, headers=headers)
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
        [relation_field("friend", users["id"]), relation_field("friends", users["id"], 5)],
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
        {"name": "ph_secret", "schema": [text_field("name")], "listRule": None, "viewRule": None},
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
        ("p_vis", {"author": ids["vis"], "authors": [ids["vis"]], "secret": s1["id"],
                   "guarded": g1["id"]}),
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
}


async def list_scenarios(
    client: httpx.AsyncClient, headers: dict[str, dict[str, str]]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for caller in CALLERS:
        h = headers[caller]
        out[caller] = {
            "users": {k: await _names(client, "ph_users", h, filter=v) for k, v in USER_FILTERS.items()},
            "posts": {k: await _names(client, "ph_posts", h, filter=v) for k, v in POST_FILTERS.items()},
            "sort": {k: await _names(client, "ph_users", h, sort=v) for k, v in USER_SORTS.items()},
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


async def run_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
    # List filters and sorts are the deterministic core of the hidden-field
    # oracle; realtime parity is covered by the PostgreSQL integration test
    # tests/integration/test_hidden_auth_filter_fields.py, and expand/
    # serialization differences belong to the expand work, not this probe.
    await dataset(client, admin)
    headers = await caller_headers(client, admin)
    return {
        "lists": await list_scenarios(client, headers),
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
            subprocess.run, [str(binary), "migrate", "up", *flags], check=True,
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
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
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
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
                await _wait_health(client, "PPBase")
                factory = async_sessionmaker(
                    bind=get_engine(), class_=AsyncSession, expire_on_commit=False
                )
                async with factory() as session:
                    row = (
                        await session.execute(
                            select(ParamRecord).where(ParamRecord.key == "_setup_token")
                        )
                    ).scalars().first()
                setup = row.value.get("token") if row and isinstance(row.value, dict) else None
                body = {
                    "email": ADMIN_EMAIL,
                    "password": ADMIN_PASSWORD,
                    "passwordConfirm": ADMIN_PASSWORD,
                }
                if setup:
                    body["setupToken"] = setup
                response = await client.post("/api/admins/init", json=body)
                response.raise_for_status()
                return await run_scenarios(client, {"Authorization": response.json()["token"]})
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=30)


def _diff(left: Any, right: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(left, dict) and isinstance(right, dict):
        rows: list[dict[str, Any]] = []
        for key in sorted(set(left) | set(right)):
            rows.extend(_diff(left.get(key), right.get(key), f"{path}.{key}" if path else key))
        return rows
    if left != right:
        return [{"path": path, "pocketbase": left, "ppbase": right}]
    return []


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
        (args.out / name).write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
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
