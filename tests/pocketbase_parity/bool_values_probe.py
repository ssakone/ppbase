"""Paired PocketBase 0.22 / PPBase probe for written bool field values.

PocketBase 0.22.34 converts a written bool field value with ``cast.ToBool``
(``models/schema/schema_field.go`` l.346-347, ``forms/record_upsert.go``
l.366-417; spf13/cast v1.7.1 ``caste.go`` l.103-148): ``strconv.ParseBool``
for a string, false for a parse error, a list or an object.  This probe
sends the same JSON, multipart (with and without a file, as the JS SDK does
once a file is attached), ``@jsonPayload``, repeated-key, update and
required-field writes to an official PocketBase 0.22 binary and to PPBase,
then reads each written value three times: in the write response, through
``GET`` and in the stored column (SQLite ``data.db`` for PocketBase,
PostgreSQL for PPBase).  It writes ``pocketbase.json``, ``ppbase.json`` and
``diff.json`` in ``--out`` and backs ``tests/test_bool_field_cast.py`` and
``tests/integration/test_bool_field_writes.py``.  Batch writes do not exist
in 0.22 and are covered by the integration test only.

Differences matched by ``known_divergence`` are reported separately.

Only synthetic records are created.  PocketBase runs in a temporary data
directory on a random loopback port and is always stopped; PPBase runs
in-process (ASGI) on ``--database-url``, which must point to an empty,
disposable PostgreSQL database (one per run), in the ``--compat`` mode.

Usage (from the repository root)::

    PYTHONPATH=. python tests/pocketbase_parity/bool_values_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --compat 0.22 --out /tmp/bool-values-probe
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
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
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
FLAGS = "probe_flags"
REQUIRED = "probe_required"

GO_TRUE = ("1", "t", "T", "TRUE", "true", "True")
GO_FALSE = ("0", "f", "F", "FALSE", "false", "False")
OTHER_STRINGS = (
    "",
    " true",
    "true ",
    "\ttrue",
    "tRUE",
    "yes",
    "on",
    "no",
    "off",
    "null",
    "2",
    "-1",
    "0.5",
    "10",
    "1.0",
    "01",
    "-0",
)
STRINGS = GO_TRUE + GO_FALSE + OTHER_STRINGS
JSON_VALUES: tuple[Any, ...] = (
    True,
    False,
    None,
    0,
    1,
    -1,
    0.5,
    2,
    *STRINGS,
    [],
    [True],
    ["true"],
    {},
    {"a": True},
)
REPEATED = (("true", "false"), ("false", "true"), ("true", "true"), ("false", "false"))
PAYLOAD_VALUES: tuple[Any, ...] = (
    "false",
    "0",
    False,
    0,
    "true",
    "1",
    True,
    1,
    [True],
    None,
)
# (initial value, write kind, written value); "untouched" writes ``label`` only.
UPDATES: tuple[tuple[bool, str, Any], ...] = (
    (True, "json", "false"),
    (True, "json", "0"),
    (True, "json", ""),
    (True, "json", "yes"),
    (True, "json", None),
    (True, "form", "false"),
    (True, "form", "0"),
    (True, "form_file", "false"),
    (True, "payload", "false"),
    (True, "payload", False),
    (True, "untouched", None),
    (False, "json", "true"),
    (False, "json", "1"),
    (False, "form", "true"),
    (False, "form", "T"),
    (False, "form_file", "1"),
    (False, "payload", "true"),
    (False, "payload", True),
    (False, "untouched", None),
)
REQUIRED_WRITES: tuple[tuple[str, Any], ...] = (
    ("json", "false"),
    ("json", "0"),
    ("json", ""),
    ("json", False),
    ("json", "true"),
    ("json", "1"),
    ("json", True),
    ("form", "false"),
    ("form", "0"),
    ("form", "true"),
    ("form_file", "false"),
    ("form_file", "true"),
)

StoredReader = Callable[[str, str, str], Awaitable[Any]]


def known_divergence(path: str, compat: str) -> str | None:
    """Return why a difference is expected, ``None`` when it is not."""
    if ".repeated:" in path:
        # 0.22.34 keeps the first value of a non-arrayable key
        # (forms/record_upsert.go l.191-196); PPBase keeps every value and a
        # list is false, as in v0.40.6 (tools/router/unmarshal_request_data.go
        # l.84-90, core/field_bool.go l.103-106).
        return "repeated multipart key: 0.22.34 reads the first value (D-XXXX)"
    if compat != "0.22" and path.startswith("required.") and path.endswith(".message"):
        return "default mode wording of a required bool (0.22 mode: 0.22.34 wording)"
    return None


def _field(
    name: str, field_type: str, options: dict[str, Any], *, required: bool = False
) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": field_type,
        "required": required,
        "presentable": False,
        "unique": False,
        "options": options,
    }


def _schema(flag: str, *, required: bool) -> list[dict[str, Any]]:
    return [
        _field("label", "text", {"min": None, "max": None, "pattern": ""}),
        _field(flag, "bool", {}, required=required),
        _field(
            "doc",
            "file",
            {
                "mimeTypes": [],
                "thumbs": [],
                "maxSelect": 1,
                "maxSize": 5242880,
                "protected": False,
            },
        ),
    ]


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"_text": response.text[:200]}


def _parts(kind: str, column: str, raw: Any) -> list[tuple[str, Any]]:
    """Multipart parts of a write: a form value, ``@jsonPayload`` or a file."""
    if kind == "payload":
        parts = [("@jsonPayload", (None, json.dumps({column: raw})))]
    elif kind == "untouched":
        parts = [("label", (None, "untouched"))]
    elif kind == "repeated":
        parts = [(column, (None, value)) for value in raw]
    else:
        parts = [(column, (None, raw))]
    if kind in ("form_file", "payload", "repeated"):
        parts.append(("doc", ("pixel.png", PNG, "image/png")))
    return parts


async def _write(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    kind: str,
    column: str,
    raw: Any,
) -> httpx.Response:
    if kind == "json":
        return await client.request(method, url, json={column: raw})
    return await client.request(method, url, files=_parts(kind, column, raw))


async def _observe(
    client: httpx.AsyncClient,
    stored: StoredReader,
    collection: str,
    response: httpx.Response,
    column: str,
) -> dict[str, Any]:
    body = _json(response)
    if response.status_code != 200:
        data = body.get("data", {}) if isinstance(body, dict) else {}
        return {"status": response.status_code, "error": data.get(column, data)}
    record_id = body["id"]
    read = await client.get(f"/api/collections/{collection}/records/{record_id}")
    return {
        "status": 200,
        "response": body.get(column),
        "read": _json(read).get(column),
        "stored": await stored(collection, record_id, column),
    }


async def run_scenarios(
    client: httpx.AsyncClient, admin: dict[str, str], stored: StoredReader
) -> dict[str, Any]:
    for name, flag, required in ((FLAGS, "flag", False), (REQUIRED, "req", True)):
        response = await client.post(
            "/api/collections",
            headers=admin,
            json={
                "name": name,
                "type": "base",
                "schema": _schema(flag, required=required),
                **PUBLIC,
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"collection {name} failed: {response.text[:400]}")

    flags_url = f"/api/collections/{FLAGS}/records"
    results: dict[str, Any] = {"create": {}, "update": {}, "required": {}}
    create = results["create"]
    for raw in JSON_VALUES:
        response = await _write(client, "POST", flags_url, "json", "flag", raw)
        key = f"json:{json.dumps(raw)}"
        create[key] = await _observe(client, stored, FLAGS, response, "flag")
    for kind in ("form", "form_file"):
        for raw in STRINGS:
            response = await _write(client, "POST", flags_url, kind, "flag", raw)
            key = f"{kind}:{json.dumps(raw)}"
            create[key] = await _observe(client, stored, FLAGS, response, "flag")
    for values in REPEATED:
        response = await _write(client, "POST", flags_url, "repeated", "flag", values)
        key = f"form_file.repeated:{json.dumps(list(values))}"
        create[key] = await _observe(client, stored, FLAGS, response, "flag")
    for raw in PAYLOAD_VALUES:
        response = await _write(client, "POST", flags_url, "payload", "flag", raw)
        key = f"payload:{json.dumps(raw)}"
        create[key] = await _observe(client, stored, FLAGS, response, "flag")

    for initial, kind, raw in UPDATES:
        created = await client.post(flags_url, json={"flag": initial})
        if created.status_code != 200:
            raise RuntimeError(f"update seed failed: {created.text[:300]}")
        url = f"{flags_url}/{created.json()['id']}"
        response = await _write(client, "PATCH", url, kind, "flag", raw)
        key = f"{json.dumps(initial)}->{kind}:{json.dumps(raw)}"
        results["update"][key] = await _observe(client, stored, FLAGS, response, "flag")

    required_url = f"/api/collections/{REQUIRED}/records"
    for kind, raw in REQUIRED_WRITES:
        response = await _write(client, "POST", required_url, kind, "req", raw)
        key = f"{kind}:{json.dumps(raw)}"
        results["required"][key] = await _observe(
            client, stored, REQUIRED, response, "req"
        )
    return results


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def pocketbase_client(
    binary: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, Path]]:
    with tempfile.TemporaryDirectory(prefix="ppbase-pb-bool-probe-") as root:
        data_dir = Path(root) / "pb_data"
        flags = [
            "--dir",
            str(data_dir),
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
                yield client, data_dir
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()


async def run_pocketbase(binary: Path) -> dict[str, Any]:
    storage_types: collections.Counter[str] = collections.Counter()
    async with pocketbase_client(binary) as (client, data_dir):
        database = f"file:{data_dir / 'data.db'}?mode=ro"

        async def stored(collection: str, record_id: str, column: str) -> Any:
            with contextlib.closing(sqlite3.connect(database, uri=True)) as conn:
                value, kind = conn.execute(
                    f'SELECT "{column}", typeof("{column}") FROM "{collection}" '
                    "WHERE id = ?",
                    (record_id,),
                ).fetchone()
            storage_types[f"{kind}:{value}"] += 1
            # SQLite has no boolean type: PocketBase stores 0 or 1.
            return bool(value) if kind == "integer" and value in (0, 1) else value

        response = await client.post(
            "/api/admins/auth-with-password",
            json={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        response.raise_for_status()
        results = await run_scenarios(
            client, {"Authorization": response.json()["token"]}, stored
        )
    results["pocketbase_storage"] = dict(sorted(storage_types.items()))
    return results


async def run_ppbase(database_url: str, compat: str) -> dict[str, Any]:
    from httpx import ASGITransport
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ppbase.app import create_app
    from ppbase.config import Settings
    from ppbase.db.bootstrap import bootstrap_system_collections
    from ppbase.db.engine import close_engine, init_engine
    from ppbase.db.system_tables import CollectionRecord, create_system_tables
    from ppbase.middleware.error_compat import set_error_compat
    from ppbase.services.admin_service import create_admin
    from ppbase.services.auth_service import create_admin_token

    secret = "probe-secret-key-for-bool-values-0000000000001"
    with tempfile.TemporaryDirectory(prefix="ppbase-bool-probe-data-") as data_dir:
        settings = Settings(
            database_url=database_url,
            jwt_secret=secret,
            auto_migrate=False,
            data_dir=data_dir,
        )
        app = create_app(settings)
        if compat == "0.22":
            set_error_compat(app, "0.22")
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

            async def stored(collection: str, record_id: str, column: str) -> Any:
                async with engine.connect() as conn:
                    return (
                        await conn.execute(
                            text(
                                f'SELECT "{column}" FROM "{collection}" '
                                'WHERE "id" = :id'
                            ),
                            {"id": record_id},
                        )
                    ).scalar_one()

            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver", timeout=60
            ) as client:
                return await run_scenarios(client, {"Authorization": token}, stored)
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
    # ``0 == False`` and ``1 == 1.0`` in Python: compare the JSON texts.
    if json.dumps(left, sort_keys=True) != json.dumps(right, sort_keys=True):
        return [{"path": path, "pocketbase": left, "ppbase": right}]
    return []


def _ppbase_module() -> str:
    import ppbase

    return str(Path(ppbase.__file__).resolve().parent)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--compat", choices=("0.22", "default"), default="0.22")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    pocketbase = asyncio.run(run_pocketbase(args.pocketbase))
    (args.out / "pocketbase.json").write_text(
        json.dumps(pocketbase, indent=1, sort_keys=True) + "\n"
    )
    compared = {
        key: value for key, value in pocketbase.items() if key != "pocketbase_storage"
    }
    ppbase = asyncio.run(run_ppbase(args.database_url, args.compat))
    differences = _diff(compared, ppbase)
    for row in differences:
        row["known"] = known_divergence(row["path"], args.compat)
    for name, payload in (("ppbase.json", ppbase), ("diff.json", differences)):
        (args.out / name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n"
        )
    unexpected = [row for row in differences if not row["known"]]
    cases = sum(len(group) for group in compared.values())
    print(
        json.dumps(
            {
                "compat": args.compat,
                "cases": cases,
                "differences": len(differences),
                "known": len(differences) - len(unexpected),
                "unexpected": len(unexpected),
                "pocketbase_storage": pocketbase["pocketbase_storage"],
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
