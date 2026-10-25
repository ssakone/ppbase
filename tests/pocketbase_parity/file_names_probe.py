"""Paired PocketBase 0.22.34 / PPBase probe for file field writes.

A record write may only keep file names that the record already references
(PocketBase 0.22.34 ``forms/record_upsert.go`` l.428-439, "The field contains
unknown filenames."; v0.40.6 ``core/field_file.go`` l.250-269, "Invalid new
files").  PPBase used to accept any well-formed name in a JSON or multipart
text value, so a client allowed by the ``updateRule`` could point a record at
another record's file, a ``.attrs`` side-car or an unreferenced object of the
record directory, then download it.

This probe sends the same requests to an official PocketBase 0.22.34 binary
and to PPBase in its 0.22 mode (``error_compat = "0.22"``, the BUMA setting):
JSON updates of a single and a multiple file field (kept, removed, reordered,
duplicated, unknown, ``.attrs``, another record's name, a name with a
separator, a removed name sent again, the ``+``/``-`` modifiers and the
legacy ``field.<name>`` / ``field.<index>`` deletions), multipart updates
(uploads, names sent with uploads, ``docs+`` uploads, two uploads on a
single field, too many files, a JSON array sent as one text value), creates,
the 403/404 precedence and the ``createData`` of ``auth-with-oauth2``
(0.22.34 ignores the ``LoadData`` error there, so the refused file field and
the schema fields after it are not loaded).  For each request it records the
status, the exact error bytes, the record value afterwards and the download
status of the files involved.  Stored file names are compared without their
random suffix (``alpha_<10 chars>.txt`` -> ``alpha.txt``).

OAuth2 never leaves the machine: PocketBase's GitLab provider points at a
local fake server (custom ``authUrl``/``tokenUrl``/``userApiUrl``), and
PPBase's provider lookup is replaced in-process by the same fake user.

Only synthetic records are created.  PocketBase runs in a temporary data
directory on a random loopback port and is always stopped; PPBase runs
in-process (ASGI) on ``--database-url``, which must point to an empty,
disposable PostgreSQL database.  Usage (from the repository root)::

    PYTHONPATH=. python tests/pocketbase_parity/file_names_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --out /tmp/file-names-probe
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

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
LOCKED = {
    "listRule": "",
    "viewRule": "",
    "createRule": None,
    "updateRule": None,
    "deleteRule": None,
}
MAX_DOCS = 4
# ``alpha_<random>.txt`` (PocketBase filesystem.normalizeName, PPBase
# _generate_storage_filename) -> ``alpha.txt``.
_STORED = re.compile(r"^([a-z0-9]+)_[A-Za-z0-9]{10}(\.txt)$")
_VOLATILE = {"id", "created", "updated", "collectionId"}


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


def file_field(name: str, max_select: int) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": "file",
        "required": False,
        "presentable": False,
        "unique": False,
        "options": {
            "mimeTypes": [],
            "thumbs": [],
            "maxSelect": max_select,
            "maxSize": 1048576,
            "protected": False,
        },
    }


def norm(value: Any) -> Any:
    """Drop volatile record keys and the random part of stored names."""
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [norm(v) for v in value]
    if isinstance(value, str):
        match = _STORED.match(value)
        return match.expand(r"\1\2") if match else value
    return value


def _upload(name: str) -> tuple[str, bytes, str]:
    return (name, f"synthetic {name}\n".encode(), "text/plain")


def _text(value: str) -> tuple[None, str]:
    """A multipart text part (httpx sends ``data=`` alone urlencoded)."""
    return (None, value)


@dataclass
class Side:
    """What differs between the two servers of the probe."""

    #: The directory holding ``<collectionId>/<recordId>/<file>``.
    storage: Path
    #: The auth collection options that enable OAuth2.
    oauth2_options: dict[str, Any]


def _plant(side: Side, collection_id: str, record_id: str, name: str) -> None:
    """A leftover object in the directory of a record id not created yet."""
    directory = side.storage / collection_id / record_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b"leftover object\n")


async def run_oauth2_scenarios(
    client: httpx.AsyncClient, admin: dict[str, str], side: Side
) -> dict:
    """``createData`` of ``auth-with-oauth2`` (each code is a new user)."""
    response = await client.post(
        "/api/collections",
        headers=admin,
        json={
            "name": "oprobe",
            "type": "auth",
            **PUBLIC,
            "options": side.oauth2_options,
            "schema": [
                text_field("title"),
                file_field("doc", 1),
                file_field("docs", MAX_DOCS),
                text_field("later"),
            ],
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"collection oprobe: {response.text[:300]}")
    collection_id = response.json()["id"]
    chosen, stray = "chosenoauth0001", "stray_abcdefghij.txt"
    _plant(side, collection_id, chosen, stray)
    out: dict[str, Any] = {}
    cases = {
        "oauth2_create_unknown_name": {
            "title": "t",
            "doc": "unknown.txt",
            "later": "l",
        },
        "oauth2_create_leftover_chosen_id": {"id": chosen, "docs": [stray]},
        "oauth2_create_second_file_field": {
            "title": "t3",
            "doc": "",
            "docs": ["unknown.txt"],
            "later": "l3",
        },
        "oauth2_create_attrs_modifier": {
            "title": "t5",
            "docs-": ["x"],
            "doc": "a.attrs",
        },
        "oauth2_create_clean": {"title": "t4", "doc": "", "docs": [], "later": "l4"},
    }
    for index, (key, create_data) in enumerate(cases.items()):
        response = await client.post(
            "/api/collections/oprobe/auth-with-oauth2",
            json={
                "provider": "gitlab",
                "code": f"code{index}",
                "codeVerifier": "probe-verifier",
                "redirectUrl": "http://127.0.0.1/callback",
                "createData": create_data,
            },
        )
        row: dict[str, Any] = {"status": response.status_code}
        if response.status_code >= 400:
            row["error_bytes"] = response.text
        else:
            payload = response.json()
            record = payload.get("record") or {}
            row["record"] = {
                k: norm(record.get(k)) for k in ("title", "doc", "docs", "later")
            }
            row["isNew"] = (payload.get("meta") or {}).get("isNew")
            row["id_is_chosen"] = record.get("id") == chosen
        download = await client.get(f"/api/files/oprobe/{chosen}/{stray}")
        row["leftover_download"] = download.status_code
        out[key] = row
    return out


class _FakeGitLab(BaseHTTPRequestHandler):
    """Token and user endpoints of a GitLab provider, synthetic user only."""

    def log_message(self, *args: Any) -> None:  # silence the access log
        pass

    def _json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode())
        code = (form.get("code") or [""])[0]
        self._json(
            {
                "access_token": f"token-{code}",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        )

    def do_GET(self) -> None:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        code = token.removeprefix("token-")
        self._json(
            {
                "id": 1000 + int(code.removeprefix("code") or 0),
                "name": "Probe User",
                "username": "",
                "email": f"{code}@example.invalid",
                "avatar_url": "",
            }
        )


@contextlib.contextmanager
def fake_gitlab() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGitLab)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakePPBaseProvider:
    """The same synthetic user for PPBase's provider interface."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        pass

    async def exchange_code(self, code: str, redirect_url: str, verifier: str):
        return {"access_token": f"token-{code}"}

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        code = access_token.removeprefix("token-")
        return {
            "id": str(1000 + int(code.removeprefix("code") or 0)),
            "email": f"{code}@example.invalid",
            "name": "Probe User",
        }


async def run_scenarios(
    client: httpx.AsyncClient, admin: dict[str, str], side: Side
) -> dict:
    for name, rules in (("fprobe", PUBLIC), ("fprobe_locked", LOCKED)):
        response = await client.post(
            "/api/collections",
            headers=admin,
            json={
                "name": name,
                "type": "base",
                **rules,
                "schema": [
                    text_field("title"),
                    file_field("doc", 1),
                    file_field("docs", MAX_DOCS),
                ],
            },
        )
        if response.status_code != 200:
            raise RuntimeError(f"collection {name}: {response.text[:300]}")

    async def fresh(collection: str = "fprobe", stem: str = "alpha") -> dict:
        response = await client.post(
            f"/api/collections/{collection}/records",
            headers=admin,
            data={"title": "t"},
            files=[
                ("doc", _upload(f"{stem}.txt")),
                ("docs", _upload("beta1.txt")),
                ("docs", _upload("beta2.txt")),
                ("docs", _upload("beta3.txt")),
            ],
        )
        if response.status_code != 200:
            raise RuntimeError(f"fresh record: {response.text[:300]}")
        return response.json()

    other = await fresh(stem="omega")
    out: dict[str, Any] = {}

    async def downloads(collection: str, record_id: str, names: list[str]) -> dict:
        statuses = {}
        for name in names:
            response = await client.get(f"/api/files/{collection}/{record_id}/{name}")
            statuses[norm(name)] = response.status_code
        return statuses

    async def case(
        key: str,
        record: dict | None,
        method: str = "PATCH",
        *,
        collection: str = "fprobe",
        record_id: str | None = None,
        headers: dict[str, str] | None = None,
        check_files: list[str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        rid = record_id or (record or {}).get("id")
        url = f"/api/collections/{collection}/records"
        if method == "PATCH":
            url += f"/{rid}"
        response = await client.request(method, url, headers=headers, **kwargs)
        try:
            body: Any = response.json()
        except ValueError:
            body = {"_text": response.text[:200]}
        row: dict[str, Any] = {"status": response.status_code}
        if response.status_code >= 400:
            row["error_bytes"] = response.text
        else:
            row["body"] = {k: norm(body.get(k)) for k in ("title", "doc", "docs")}
        if rid and method == "PATCH":
            after = await client.get(f"{url}", headers=admin)
            if after.status_code == 200:
                payload = after.json()
                row["after"] = {
                    k: norm(payload.get(k)) for k in ("title", "doc", "docs")
                }
            else:
                row["after_status"] = after.status_code
        if check_files and rid:
            row["downloads"] = await downloads(collection, rid, check_files)
        out[key] = row
        return response

    # -- JSON updates -------------------------------------------------------
    a = await fresh()
    await case("json_doc_unknown", a, json={"doc": "unknown_name.txt"})
    await case("json_doc_attrs", a, json={"doc": a["doc"] + ".attrs"})
    await case("json_doc_other_record", a, json={"doc": other["doc"]})
    await case(
        "json_docs_other_record_added",
        a,
        json={"docs": [*a["docs"], other["docs"][0]]},
    )
    await case(
        "json_docs_separator",
        a,
        json={"docs": [a["docs"][0], "x/" + a["docs"][1]]},
    )
    await case(
        "json_docs_parent_path",
        a,
        json={"docs": ["../" + other["id"] + "/" + other["docs"][0]]},
    )
    await case("json_doc_last_unknown", a, json={"doc": [a["doc"], "unknown.txt"]})
    await case("json_doc_last_known", a, json={"doc": ["unknown.txt", a["doc"]]})
    await case("json_doc_list_keep", a, json={"doc": [a["doc"]]})
    await case(
        "json_unknown_two_fields",
        a,
        json={"title": "changed", "doc": "u1.txt", "docs": ["u2.txt"]},
    )
    await case(
        "json_mixed_atomic",
        a,
        json={"title": "changed", "docs": [a["docs"][0], "unknown.txt"]},
    )
    await case(
        "json_docs_reorder",
        a,
        json={"docs": [a["docs"][2], a["docs"][0], a["docs"][1]]},
    )
    await case("json_docs_append_unknown", a, json={"docs+": ["unknown.txt"]})
    await case("json_docs_prepend_unknown", a, json={"+docs": ["unknown.txt"]})
    await case("json_docs_append_known", a, json={"docs+": [a["docs"][0]]})
    await case(
        "json_docs_subset",
        a,
        json={"docs": [a["docs"][0], a["docs"][1]]},
        check_files=a["docs"],
    )
    await case("json_docs_reintroduce", a, json={"docs": a["docs"]})
    await case("json_docs_append_removed", a, json={"docs+": [a["docs"][2]]})
    await case("json_doc_clear", a, json={"doc": ""}, check_files=[a["doc"]])
    await case("json_doc_reintroduce", a, json={"doc": a["doc"]})

    b = await fresh()
    await case(
        "json_docs_duplicates",
        b,
        json={"docs": [b["docs"][0], b["docs"][0], b["docs"][1]]},
        check_files=b["docs"],
    )
    c = await fresh()
    await case(
        "json_docs_remove_modifier",
        c,
        json={"docs-": [c["docs"][0], "unknown.txt"]},
        check_files=c["docs"],
    )
    d = await fresh()
    await case("json_docs_legacy_name_delete", d, json={f"docs.{d['docs'][1]}": ""})
    await case("json_docs_legacy_index_delete", d, json={"docs.0": None})
    await case("json_docs_legacy_unknown_name", d, json={"docs.unknown.txt": ""})
    e = await fresh()
    await case("json_docs_null", e, json={"docs": None}, check_files=e["docs"])
    await case("json_docs_empty_names", e, json={"docs": ["", ""]})

    # -- Multipart updates --------------------------------------------------
    f = await fresh()
    await case("urlencoded_unknown", f, data={"doc": "unknown.txt"})
    await case("mp_text_unknown", f, files=[("doc", _text("unknown.txt"))])
    await case(
        "mp_text_other_record",
        f,
        files=[("docs", _text(f["docs"][0])), ("docs", _text(other["docs"][0]))],
    )
    await case(
        "mp_jsonpayload_unknown_with_upload",
        f,
        files=[
            ("@jsonPayload", _text(json.dumps({"docs": ["unknown.txt"]}))),
            ("docs", _upload("gamma.txt")),
        ],
    )
    await case("mp_upload_docs", f, files=[("docs", _upload("gamma.txt"))])
    await case(
        "mp_upload_too_many",
        f,
        files=[("docs", _upload("delta.txt")), ("docs", _upload("epsilon.txt"))],
    )
    await case("mp_upload_docs_plus", f, files=[("docs+", _upload("zeta.txt"))])
    g = await fresh()
    await case(
        "mp_names_with_upload",
        g,
        files=[("docs", _text(g["docs"][0])), ("docs", _upload("gamma.txt"))],
        check_files=g["docs"],
    )
    await case(
        "mp_upload_doc_replace",
        g,
        files=[("doc", _upload("delta.txt"))],
        check_files=[g["doc"]],
    )
    await case(
        "mp_upload_doc_two",
        g,
        files=[("doc", _upload("epsilon.txt")), ("doc", _upload("zeta.txt"))],
    )
    h = await fresh()
    await case(
        "mp_text_reorder",
        h,
        files=[("docs", _text(name)) for name in reversed(h["docs"])],
    )
    await case("mp_text_minus", h, files=[("docs-", _text(h["docs"][0]))])
    await case(
        "mp_text_legacy_name_delete", h, files=[(f"docs.{h['docs'][1]}", _text(""))]
    )
    await case(
        "mp_text_reintroduce",
        h,
        files=[("docs", _text(h["docs"][2])), ("docs", _text(h["docs"][1]))],
    )
    m = await fresh()
    await case(
        "mp_text_json_array",
        m,
        files=[("docs", _text(json.dumps([m["docs"][1], m["docs"][0], m["docs"][2]])))],
    )
    await case(
        "mp_text_json_array_minus",
        m,
        files=[("docs-", _text(json.dumps([m["docs"][0]])))],
        check_files=m["docs"],
    )

    # -- Creates ------------------------------------------------------------
    await case("create_json_unknown", None, "POST", json={"doc": "unknown.txt"})
    await case(
        "create_json_other_record", None, "POST", json={"docs": [other["docs"][0]]}
    )
    await case(
        "create_json_empty", None, "POST", json={"title": "n", "doc": "", "docs": []}
    )
    await case(
        "create_mp_name_and_upload",
        None,
        "POST",
        files=[("docs", _text("unknown.txt")), ("docs", _upload("gamma.txt"))],
    )
    await case(
        "create_admin_json_unknown",
        None,
        "POST",
        headers=admin,
        json={"doc": "unknown.txt"},
    )

    # -- Precedence ---------------------------------------------------------
    locked = await fresh("fprobe_locked")
    await case(
        "update_locked_unknown",
        locked,
        collection="fprobe_locked",
        json={"doc": "unknown.txt"},
    )
    await case(
        "create_locked_unknown",
        None,
        "POST",
        collection="fprobe_locked",
        json={"doc": "unknown.txt"},
    )
    await case(
        "update_missing_record_unknown",
        None,
        record_id="missingrecord00",
        json={"doc": "unknown.txt"},
    )
    out.update(await run_oauth2_scenarios(client, admin, side))
    return out


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def pocketbase_client(
    binary: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, Path]]:
    with tempfile.TemporaryDirectory(prefix="ppbase-pb-filenames-") as root:
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
                yield client, Path(root) / "pb_data"
        finally:
            if process.returncode is None:
                process.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=10)
                if process.returncode is None:
                    process.kill()
                    await process.wait()


async def run_pocketbase(binary: Path) -> dict[str, Any]:
    with fake_gitlab() as provider_url:
        async with pocketbase_client(binary) as (client, data_dir):
            response = await client.post(
                "/api/admins/auth-with-password",
                json={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            )
            response.raise_for_status()
            admin = {"Authorization": response.json()["token"]}
            response = await client.patch(
                "/api/settings",
                headers=admin,
                json={
                    "gitlabAuth": {
                        "enabled": True,
                        "clientId": "probe-client",
                        "clientSecret": "probe-secret",
                        "authUrl": f"{provider_url}/oauth/authorize",
                        "tokenUrl": f"{provider_url}/oauth/token",
                        "userApiUrl": f"{provider_url}/api/v4/user",
                    }
                },
            )
            response.raise_for_status()
            side = Side(
                storage=data_dir / "storage",
                oauth2_options={
                    "allowOAuth2Auth": True,
                    "allowEmailAuth": True,
                    "allowUsernameAuth": True,
                    "exceptEmailDomains": None,
                    "onlyEmailDomains": None,
                    "manageRule": None,
                    "minPasswordLength": 8,
                    "onlyVerified": False,
                    "requireEmail": False,
                },
            )
            return await run_scenarios(client, admin, side)


async def run_ppbase(database_url: str) -> dict[str, Any]:
    from httpx import ASGITransport
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ppbase.app import create_app
    from ppbase.config import Settings
    from ppbase.db.bootstrap import bootstrap_system_collections
    from ppbase.db.engine import close_engine, init_engine
    from ppbase.db.system_tables import CollectionRecord, create_system_tables
    from ppbase.services import oauth2_service
    from ppbase.services.admin_service import create_admin
    from ppbase.services.auth_service import create_admin_token

    secret = "probe-secret-key-for-file-names-0001"
    with tempfile.TemporaryDirectory(prefix="ppbase-filenames-data-") as data_dir:
        settings = Settings(
            database_url=database_url,
            jwt_secret=secret,
            auto_migrate=False,
            data_dir=data_dir,
            error_compat="0.22",
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
            side = Side(
                storage=Path(data_dir).resolve() / "storage",
                oauth2_options={"oauth2": {"enabled": True}},
            )
            oauth2_service.get_provider_credentials = lambda *a, **k: ("id", "secret")
            oauth2_service.get_provider_class = lambda name: _FakePPBaseProvider
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver", timeout=60
            ) as client:
                return await run_scenarios(client, {"Authorization": token}, side)
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
                "cases": len(pocketbase),
                "differences": len(differences),
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
