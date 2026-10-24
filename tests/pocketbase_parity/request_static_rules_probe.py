"""Paired PocketBase 0.22 / PPBase probe for @request.* static rules.

An absent ``@request.*`` value (anonymous caller, or a role-permission key the
flattened BUMA auth context does not declare) must compare like PocketBase and
never raise: PocketBase binds it as SQL ``NULL`` and resolves the comparison
with ``resolveEqualExpr`` (resolvers/record_field_resolver.go l.162-223,
tools/search/filter.go l.290-368), so the list returns 200 with the allowed
records.  PPBase used to bind ``''`` and emit ``$n IS NOT NULL`` / ``'' = TRUE``,
which asyncpg/PostgreSQL reject (IndeterminateDatatypeError, DataError).

This probe exercises the exact BUMA rule shapes
(``@request.auth.id != '' && @request.auth.id != null`` and
``(@request.auth.role.permission.isAdmin = true || ....read = true) &&
@request.auth.agent = agent``) as an anonymous caller, plus two general list
shape checks (totalPages of an empty list, and the omitted ``expand`` key),
against an official PocketBase 0.22.34 binary and PPBase.

The BUMA ``@request.auth.role.permission.*`` context flattens the role JSON
into dotted keys (app/auth/context.py); PocketBase resolves the same path
through the auth record's ``role`` relation and ``json_extract`` (a missing
key or role is NULL).  The anonymous caller is the production 500 scenario;
the authenticated callers (``ROLE_USERS``) cover a missing key, a missing
role and non-boolean values (``1``, ``0``, ``"true"``) against ``= true`` and
``!= true``.  For PPBase the probe emulates BUMA's flattening by patching
``ppbase.api.records._prepare_rule_context`` (without BUMA's declared-key
defaults, so an undeclared key stays absent).

Only synthetic records are created.  Usage (from the repository root)::

    PYTHONPATH=. python tests/pocketbase_parity/request_static_rules_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --out /tmp/request-static-probe
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

ADMIN_EMAIL = "probe-admin@example.invalid"
ADMIN_PASSWORD = "probe-Password-123"
PUBLIC = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}

# The exact rule strings observed failing on the real backup (WS-G).
RULE_AUTH_ID = "@request.auth.id != '' && @request.auth.id != null"
RULE_PERM = (
    "(@request.auth.role.permission.isAdmin = true"
    " || @request.auth.role.permission.pdv.read = true)"
    " && @request.auth.agent = agent"
)


RULE_PERM_NEG = (
    "@request.auth.role.permission.pdv.read != true && @request.auth.agent = agent"
)
USER_PASSWORD = "probe-User-Password-1"
# Authenticated callers: the role permission JSON of each one (PocketBase
# resolves ``@request.auth.role.permission.pdv.read`` through the role
# relation and ``json_extract``; a missing key or role is NULL).
ROLE_USERS: dict[str, tuple[Any, str]] = {
    "u_true": ({"pdv": {"read": True}}, "A1"),
    "u_one": ({"pdv": {"read": 1}}, "A1"),
    "u_zero": ({"pdv": {"read": 0}}, "A1"),
    "u_str": ({"pdv": {"read": "true"}}, "A1"),
    "u_missing_key": ({"pdv": {}}, "A1"),
    "u_no_permission": ({}, "A1"),
    "u_no_role": (None, "A1"),
    "u_admin": ({"isAdmin": True}, "A2"),
    "u_admin_str": ({"isAdmin": "true"}, "A2"),
}


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """BUMA app/auth/context.py ``_flatten``: nested JSON to dotted keys."""
    if not isinstance(value, dict):
        return {prefix: value} if prefix else {}
    result: dict[str, Any] = {}
    for key, child in value.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, dict):
            result.update(flatten(child, name))
        else:
            result[name] = child
    return result


def flat_auth_context(permission: Any, agent: str) -> dict[str, Any]:
    """The flattened ``@request.auth`` keys BUMA adds for a caller.

    Unlike BUMA (which declares the keys of ``buma_apps_roles`` as False),
    an undeclared key stays absent, i.e. NULL like PocketBase's
    ``json_extract`` of a missing key.
    """
    context: dict[str, Any] = {"agent": agent}
    if permission is not None:
        context.update(
            {
                f"role.{key}": value
                for key, value in flatten({"permission": permission}).items()
            }
        )
    return context


def json_field(name: str) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "json",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {"maxSize": 2000000},
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


def relation_field(name: str, collection_id: str) -> dict[str, Any]:
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
            "maxSelect": 1,
            "displayFields": None,
        },
    }


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


async def _create(
    client: httpx.AsyncClient, name: str, payload: dict[str, Any], admin: dict[str, str]
) -> dict[str, Any]:
    response = await client.post(
        f"/api/collections/{name}/records", headers=admin, json=payload
    )
    if response.status_code != 200:
        raise RuntimeError(f"create {name} failed: {response.text[:300]}")
    return response.json()


async def _list(
    client: httpx.AsyncClient,
    name: str,
    headers: dict[str, str] | None = None,
    **params: Any,
) -> Any:
    response = await client.get(
        f"/api/collections/{name}/records",
        params={"perPage": 200, **params},
        headers=headers,
    )
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = {"_text": response.text[:200]}
    if response.status_code != 200:
        return {"status": response.status_code}
    return body


async def authenticated_scenarios(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    on_user: Any = None,
) -> dict[str, Any]:
    """The buma_pdv rule for authenticated callers with absent/non-boolean keys."""
    roles = await _collection(
        client, admin, {"name": "rs_roles", "schema": [json_field("permission")]}
    )
    users = await _collection(
        client,
        admin,
        {
            "name": "rs_users",
            "type": "auth",
            "schema": [text_field("agent"), relation_field("role", roles["id"])],
            "options": {
                "allowEmailAuth": True,
                "allowOAuth2Auth": False,
                "allowUsernameAuth": False,
                "exceptEmailDomains": None,
                "manageRule": None,
                "minPasswordLength": 8,
                "onlyEmailDomains": None,
                "onlyVerified": False,
                "requireEmail": False,
            },
        },
    )
    await _collection(
        client,
        admin,
        {
            "name": "rs_pdv_neg",
            "schema": [text_field("name"), text_field("agent")],
            "listRule": RULE_PERM_NEG,
        },
    )
    for name, agent in (("n_empty", ""), ("n_a1", "A1"), ("n_a2", "A2")):
        await _create(client, "rs_pdv_neg", {"name": name, "agent": agent}, admin)
    out: dict[str, Any] = {}
    for user, (permission, agent) in ROLE_USERS.items():
        payload: dict[str, Any] = {
            "email": f"{user.replace('_', '')}@example.invalid",
            "password": USER_PASSWORD,
            "passwordConfirm": USER_PASSWORD,
            "agent": agent,
        }
        if permission is not None:
            role = await _create(client, "rs_roles", {"permission": permission}, admin)
            payload["role"] = role["id"]
        record = await _create(client, "rs_users", payload, admin)
        if on_user is not None:
            on_user(record["id"], flat_auth_context(permission, agent))
        response = await client.post(
            "/api/collections/rs_users/auth-with-password",
            json={"identity": payload["email"], "password": USER_PASSWORD},
        )
        response.raise_for_status()
        headers = {"Authorization": response.json()["token"]}
        row: dict[str, Any] = {}
        for key, name in (
            ("pdv", "rs_pdv"),
            ("pdv_neg", "rs_pdv_neg"),
            ("stk", "rs_stockage"),
        ):
            body = await _list(client, name, headers, sort="name")
            row[key] = [body.get("status", 200)] + [
                item["name"] for item in body.get("items", [])
            ]
        out[user] = row
    del users
    return out


async def run_scenarios(
    client: httpx.AsyncClient, admin: dict[str, str], on_user: Any = None
) -> Any:
    # buma_article_stockage-like: the @request.auth.id rule.
    stk = await _collection(
        client,
        admin,
        {
            "name": "rs_stockage",
            "schema": [text_field("name")],
            "listRule": RULE_AUTH_ID,
        },
    )
    for i in range(3):
        await _create(client, "rs_stockage", {"name": f"s{i}"}, admin)

    # buma_pdv-like: the role permission + agent rule.
    pdv = await _collection(
        client,
        admin,
        {
            "name": "rs_pdv",
            "schema": [text_field("name"), text_field("agent")],
            "listRule": RULE_PERM,
        },
    )
    for name, agent in (("p_empty", ""), ("p_a1", "A1"), ("p_a2", "A2")):
        await _create(client, "rs_pdv", {"name": name, "agent": agent}, admin)

    # Empty list (totalPages) + expand key omission.
    rels = await _collection(
        client, admin, {"name": "rs_rel", "schema": [text_field("name")]}
    )
    posts = await _collection(
        client,
        admin,
        {
            "name": "rs_posts",
            "schema": [text_field("name"), relation_field("rel", rels["id"])],
        },
    )
    linked = await _create(client, "rs_rel", {"name": "r1"}, admin)
    await _create(client, "rs_posts", {"name": "with_rel", "rel": linked["id"]}, admin)
    await _create(client, "rs_posts", {"name": "no_rel"}, admin)

    out: dict[str, Any] = {}

    # Anonymous list against the two BUMA rules: must be 200, authorized rows.
    stk_anon = await _list(client, "rs_stockage")
    out["stockage_anon_status"] = stk_anon.get("status", 200)
    out["stockage_anon_names"] = sorted(
        item["name"] for item in stk_anon.get("items", [])
    )
    pdv_anon = await _list(client, "rs_pdv", sort="name")
    out["pdv_anon_status"] = pdv_anon.get("status", 200)
    out["pdv_anon_names"] = sorted(item["name"] for item in pdv_anon.get("items", []))

    # totalPages of an empty result set.
    empty = await _list(client, "rs_pdv", filter='name = "nope"')
    out["empty_total_items"] = empty.get("totalItems")
    out["empty_total_pages"] = empty.get("totalPages")

    # expand key: present with a linked record, omitted without one.
    expanded = await _list(client, "rs_posts", expand="rel", sort="name")
    out["expand_keys"] = {
        item["name"]: (
            "expand" in item,
            (item.get("expand") or {}).get("rel", {}).get("name"),
        )
        for item in expanded.get("items", [])
    }
    out["authenticated"] = await authenticated_scenarios(client, admin, on_user)
    del stk, pdv, rels, posts
    return out


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def pocketbase_client(binary: Path) -> AsyncIterator[httpx.AsyncClient]:
    with tempfile.TemporaryDirectory(prefix="ppbase-pb-reqstatic-") as root:
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
                deadline = time.monotonic() + 30
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
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=10)
                if process.returncode is None:
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

    secret = "probe-secret-key-for-request-static-rules-0001"
    with tempfile.TemporaryDirectory(prefix="ppbase-reqstatic-data-") as data_dir:
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
            # Emulate BUMA's flattened ``@request.auth.role.permission.*``
            # context (app/auth/context.py) for the authenticated callers.
            import ppbase.api.records as records_api

            live: dict[str, dict[str, Any]] = {}
            original = records_api._prepare_rule_context

            def patched(request: Any, token_payload: Any, data: Any = None) -> Any:
                auth_ctx, request_ctx = original(request, token_payload, data=data)
                extra = live.get(str((token_payload or {}).get("id") or ""))
                if extra:
                    request_ctx.setdefault("auth", {}).update(extra)
                return auth_ctx, request_ctx

            records_api._prepare_rule_context = patched
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            try:
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver", timeout=60
                ) as client:
                    return await run_scenarios(
                        client, {"Authorization": token}, live.__setitem__
                    )
            finally:
                records_api._prepare_rule_context = original
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
    for name, payload in (("ppbase.json", ppbase), ("diff.json", differences)):
        (args.out / name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n"
        )
    import ppbase

    print(
        json.dumps(
            {
                "differences": len(differences),
                # The PPBase actually exercised (run with PYTHONPATH=<checkout>,
                # otherwise the installed package is imported).
                "ppbase_module": str(Path(ppbase.__file__).resolve().parent),
            },
            indent=1,
        )
    )
    for row in differences:
        print(json.dumps(row, sort_keys=True))
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
