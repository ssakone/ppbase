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
into dotted keys (app/auth/context.py); PocketBase nests it under the auth
record's expanded ``role`` relation.  To compare the *resolution* of an absent
key without rebuilding BUMA's schema, the probe uses an anonymous caller for
whom the whole ``@request.auth`` subtree is absent in both engines, which is
the production 500 scenario.

Only synthetic records are created.  Usage (from the repository root)::

    python tests/pocketbase_parity/request_static_rules_probe.py \\
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


async def _list(client: httpx.AsyncClient, name: str, **params: Any) -> Any:
    response = await client.get(
        f"/api/collections/{name}/records", params={"perPage": 200, **params}
    )
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = {"_text": response.text[:200]}
    if response.status_code != 200:
        return {"status": response.status_code}
    return body


async def run_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
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
    for name, payload in (("ppbase.json", ppbase), ("diff.json", differences)):
        (args.out / name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n"
        )
    print(json.dumps({"differences": len(differences)}, indent=1))
    for row in differences:
        print(json.dumps(row, sort_keys=True))
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
