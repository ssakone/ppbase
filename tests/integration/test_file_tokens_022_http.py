"""Protected files open with the file tokens of PocketBase 0.22.34 (HTTP).

PocketBase 0.22.34 file tokens carry no ``for`` claim: ``exp``, ``id`` and
``type`` (``admin``), plus ``collectionId`` for records (``authRecord``), signed
in HS256 with the token key plus the file secret (tokens/admin.go:28-35,
tokens/record.go:81-95, apis/file.go:221-251).  Measured on the 0.22.34
binary: exactly these claim keys, and the signature checks with
``tokenKey + adminFileToken.secret`` / ``tokenKey + recordFileToken.secret``.

The always-on tests below build the tokens that way from the stored values.
``test_tokens_issued_by_the_pocketbase_binary`` presents tokens issued by the
real binary; it runs only when ``PPBASE_TEST_POCKETBASE_022`` names a
PocketBase 0.22.x executable (synthetic data in a temporary directory, random
loopback port, process always stopped).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = pytest.mark.asyncio

PASSWORD = "securepass123"
CONTENT = b"protected 0.22 body"


async def _one(sql: str, **params: Any) -> Any:
    from ppbase.db.engine import get_engine

    async with get_engine().connect() as conn:
        return (await conn.execute(text(sql), params)).mappings().first()


async def _execute(sql: str, **params: Any) -> None:
    from ppbase.db.engine import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(text(sql), params)


async def _options(name: str) -> dict[str, Any]:
    return (await _one("SELECT options FROM _collections WHERE name = :n", n=name))["options"]


async def _set_file_secret(name: str, secret: str) -> None:
    await _execute(
        "UPDATE _collections SET options = jsonb_set(options, '{fileToken,secret}', "
        "to_jsonb(CAST(:secret AS text))) WHERE name = :n",
        secret=secret,
        n=name,
    )


def _token(claims: dict[str, Any], key: str, seconds: int = 120) -> str:
    return pyjwt.encode({"exp": int(time.time()) + seconds, **claims}, key, algorithm="HS256")


async def _protected_file(app_client: AsyncClient, admin_token: str) -> tuple[str, str]:
    """A protected file whose record is visible to any authenticated caller."""
    name = f"files022_{uuid.uuid4().hex[:8]}"
    created = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": name,
            "type": "base",
            "schema": [
                {
                    "name": "doc",
                    "type": "file",
                    "required": False,
                    "options": {"maxSelect": 1, "protected": True},
                }
            ],
            "listRule": '@request.auth.id != ""',
            "viewRule": '@request.auth.id != ""',
        },
    )
    assert created.status_code == 200, created.text
    record = await app_client.post(
        f"/api/collections/{name}/records",
        headers={"Authorization": admin_token},
        files={"doc": ("doc.txt", CONTENT, "text/plain")},
    )
    assert record.status_code == 200, record.text
    body = record.json()
    return created.json()["id"], f"/api/files/{created.json()['id']}/{body['id']}/{body['doc']}"


async def _user(app_client: AsyncClient, record_id: str | None = None) -> dict[str, Any]:
    email = f"file022_{uuid.uuid4().hex[:10]}@example.com"
    payload: dict[str, Any] = {"email": email, "password": PASSWORD, "passwordConfirm": PASSWORD}
    if record_id:
        payload["id"] = record_id
    resp = await app_client.post("/api/collections/users/records", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_pocketbase_022_record_and_admin_file_tokens_open_protected_files(
    app_client: AsyncClient, admin_token: str, auth_collection: dict
) -> None:
    collection_id, path = await _protected_file(app_client, admin_token)
    try:
        user = await _user(app_client)
        users = await _one("SELECT id, options FROM _collections WHERE name = 'users'")
        user_key = (await _one("SELECT token_key FROM users WHERE id = :id", id=user["id"]))[
            "token_key"
        ]
        record_claims = {"id": user["id"], "type": "authRecord", "collectionId": users["id"]}
        record_file = _token(record_claims, user_key + users["options"]["fileToken"]["secret"])

        admin_id = pyjwt.decode(admin_token, options={"verify_signature": False})["id"]
        admin_key = (await _one("SELECT token_key FROM _superusers WHERE id = :id", id=admin_id))[
            "token_key"
        ]
        superusers = await _options("_superusers")
        admin_claims = {"id": admin_id, "type": "admin"}
        admin_file = _token(admin_claims, admin_key + superusers["fileToken"]["secret"])

        for token in (record_file, admin_file):
            resp = await app_client.get(path, params={"token": token})
            assert resp.status_code == 200, resp.text
            assert resp.content == CONTENT

        # Refused, as by PocketBase: no token, the 0.22 session tokens (same
        # claims, session secret) and expired file tokens.  The status is
        # PPBase's 404, that of current PocketBase (v0.40.6 apis/file.go:132-133);
        # PocketBase 0.22.34 answers 403 "Insufficient permissions to access
        # the file resource." (v0.22.34 apis/file.go:136-138), a difference
        # older than this branch (api/files.py).
        refused = [
            None,
            _token(record_claims, user_key + users["options"]["authToken"]["secret"]),
            _token(admin_claims, admin_key + superusers["authToken"]["secret"]),
            _token(record_claims, user_key + users["options"]["fileToken"]["secret"], -10),
        ]
        for token in refused:
            params = {"token": token} if token else {}
            assert (await app_client.get(path, params=params)).status_code == 404
    finally:
        await app_client.delete(
            f"/api/collections/{collection_id}", headers={"Authorization": admin_token}
        )


# ---------------------------------------------------------------------------
# Tokens issued by the official PocketBase 0.22 binary (opt-in)
# ---------------------------------------------------------------------------

POCKETBASE_022 = os.environ.get("PPBASE_TEST_POCKETBASE_022", "")
PB_ADMIN_EMAIL = "admin.file022@example.com"
PB_ADMIN_PASSWORD = "admin-password-123"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _PocketBase022:
    """An owned PocketBase 0.22 server with synthetic data, always stopped."""

    def __init__(self, binary: str) -> None:
        self.binary = binary
        self.root = Path(tempfile.mkdtemp(prefix="ppbase-pb022-"))
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None

    def _args(self) -> list[str]:
        for name in ("hooks", "public", "migrations"):
            (self.root / name).mkdir(exist_ok=True)
        return [
            "--dir", str(self.root / "pb_data"),
            "--hooksDir", str(self.root / "hooks"),
            "--publicDir", str(self.root / "public"),
            "--migrationsDir", str(self.root / "migrations"),
            "--automigrate=false",
        ]  # fmt: skip

    async def start(self) -> None:
        args = self._args()
        for command in (["migrate", "up"], ["admin", "create", PB_ADMIN_EMAIL, PB_ADMIN_PASSWORD]):
            subprocess.run(
                [self.binary, *command, *args],
                check=True,
                capture_output=True,
                cwd=self.root,
                timeout=60,
            )
        self.process = subprocess.Popen(
            [self.binary, "serve", "--http", f"127.0.0.1:{self.port}", *args],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        async with httpx.AsyncClient() as client:
            for _ in range(300):
                if self.process.poll() is not None:
                    raise RuntimeError("PocketBase did not start")
                try:
                    if (await client.get(f"{self.base}/api/health")).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
        raise TimeoutError("PocketBase did not answer")

    def read(self, sql: str, *params: Any) -> Any:
        db = sqlite3.connect(f"file:{self.root / 'pb_data' / 'data.db'}?mode=ro", uri=True)
        try:
            return db.execute(sql, params).fetchone()
        finally:
            db.close()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.mark.skipif(
    not POCKETBASE_022,
    reason="set PPBASE_TEST_POCKETBASE_022 to a PocketBase 0.22.x executable",
)
async def test_tokens_issued_by_the_pocketbase_binary(
    app_client: AsyncClient, admin_token: str, auth_collection: dict
) -> None:
    """The binary issues the tokens; PPBase gets the token keys and secrets.

    This is what a migration copies: the record and admin ``tokenKey`` and the
    0.22 ``adminFileToken`` / ``recordFileToken`` secrets.
    """
    collection_id, path = await _protected_file(app_client, admin_token)
    superusers_before = await _options("_superusers")
    users_before = await _options("users")
    pocketbase = _PocketBase022(POCKETBASE_022)
    imported_admin: str | None = None
    imported_user: str | None = None
    try:
        await pocketbase.start()
        async with httpx.AsyncClient(base_url=pocketbase.base, timeout=30) as pb:
            admin = await pb.post(
                "/api/admins/auth-with-password",
                json={"identity": PB_ADMIN_EMAIL, "password": PB_ADMIN_PASSWORD},
            )
            assert admin.status_code == 200, admin.text
            admin_auth = {"Authorization": admin.json()["token"]}
            email = f"pb022_{uuid.uuid4().hex[:10]}@example.com"
            made = await pb.post(
                "/api/collections/users/records",
                headers=admin_auth,
                json={"email": email, "password": PASSWORD, "passwordConfirm": PASSWORD},
            )
            assert made.status_code == 200, made.text
            user_auth = await pb.post(
                "/api/collections/users/auth-with-password",
                json={"identity": email, "password": PASSWORD},
            )
            assert user_auth.status_code == 200, user_auth.text
            issued = {
                "admin": (await pb.post("/api/files/token", headers=admin_auth)).json()["token"],
                "record": (
                    await pb.post(
                        "/api/files/token",
                        headers={"Authorization": user_auth.json()["token"]},
                    )
                ).json()["token"],
            }
            session_token = user_auth.json()["token"]

        admin_id, admin_key = pocketbase.read(
            "SELECT id, tokenKey FROM _admins WHERE email = ?", PB_ADMIN_EMAIL
        )
        user_id, user_key = pocketbase.read("SELECT id, tokenKey FROM users WHERE email = ?", email)
        settings = json.loads(
            pocketbase.read("SELECT value FROM _params WHERE key = 'settings'")[0]
        )
        assert sorted(pyjwt.decode(issued["admin"], options={"verify_signature": False})) == [
            "exp",
            "id",
            "type",
        ]
        assert sorted(pyjwt.decode(issued["record"], options={"verify_signature": False})) == [
            "collectionId",
            "exp",
            "id",
            "type",
        ]

        # Import what a migration copies.
        await _execute(
            "INSERT INTO _superusers (id, email, token_key, password_hash) "
            "VALUES (:id, :email, :key, :hash)",
            id=admin_id,
            email=PB_ADMIN_EMAIL,
            key=admin_key,
            hash="$2a$10$" + "x" * 53,
        )
        imported_admin = admin_id
        await _user(app_client, record_id=user_id)
        imported_user = user_id
        await _execute("UPDATE users SET token_key = :key WHERE id = :id", key=user_key, id=user_id)
        await _set_file_secret("_superusers", settings["adminFileToken"]["secret"])
        await _set_file_secret("users", settings["recordFileToken"]["secret"])

        for kind, token in issued.items():
            resp = await app_client.get(path, params={"token": token})
            assert resp.status_code == 200, (kind, resp.text)
            assert resp.content == CONTENT
        # A session token of the binary is not a file token.
        assert (await app_client.get(path, params={"token": session_token})).status_code == 404
    finally:
        pocketbase.stop()
        if imported_user:
            await _execute("DELETE FROM users WHERE id = :id", id=imported_user)
        if imported_admin:
            await _execute("DELETE FROM _superusers WHERE id = :id", id=imported_admin)
        await _set_file_secret("_superusers", superusers_before["fileToken"]["secret"])
        await _set_file_secret("users", users_before["fileToken"]["secret"])
        await app_client.delete(
            f"/api/collections/{collection_id}", headers={"Authorization": admin_token}
        )
