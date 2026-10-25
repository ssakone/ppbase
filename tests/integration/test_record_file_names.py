"""File names of record writes are checked like PocketBase.

A write may keep, remove or reorder the names a record already has and add
files only by uploading them (v0.22.34 forms/record_upsert.go l.428-439,
v0.40.6 core/field_file.go l.250-269).  PPBase used to accept any
well-formed name, so a client allowed by the updateRule could reference,
then download, another record's file, a ``.attrs`` side-car or an
unreferenced object of the record directory.  The 0.22 mode answers like
PocketBase 0.22.34 (measured by tests/pocketbase_parity/file_names_probe.py).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient

from ppbase.db.engine import get_engine
from ppbase.ext.events import RecordRequestEvent
from ppbase.ext.record_repository import RecordRepository
from ppbase.ext.registry import (
    HOOK_RECORD_CREATE_REQUEST,
    HOOK_RECORD_UPDATE_REQUEST,
    ExtensionRegistry,
)
from ppbase.middleware.error_compat import set_error_compat
from ppbase.services import oauth2_service
from ppbase.services.record_file_fields import CURRENT
from ppbase.services.record_service import (
    FileNamesLoadError,
    _ValidationErrors,
    resolve_collection,
    update_record,
)

pytestmark = pytest.mark.asyncio

LOAD_ERROR = "Failed to load the submitted data due to invalid formatting."
UNKNOWN = {
    "code": "validation_unknown_filenames",
    "message": "The field contains unknown filenames.",
}
RULES = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}


def _app(client: AsyncClient) -> Any:
    return client._transport.app  # ASGITransport(app=...)


def _file(name: str) -> tuple[str, bytes, str]:
    return (name, f"synthetic {name}\n".encode(), "text/plain")


def _stem(name: str) -> str:
    """``alpha_<random>.txt`` -> ``alpha``."""
    return name.rsplit("_", 1)[0]


@pytest_asyncio.fixture(scope="module")
async def env(app_client: AsyncClient, admin_token: str) -> AsyncIterator[dict]:
    admin = {"Authorization": admin_token}
    names = {}
    ids = []
    for key, rules in (
        ("open", RULES),
        ("locked", {**RULES, "createRule": None, "updateRule": None}),
    ):
        name = f"fnames_{key}_{uuid.uuid4().hex[:8]}"
        response = await app_client.post(
            "/api/collections",
            headers=admin,
            json={
                "name": name,
                "type": "base",
                **rules,
                "schema": [
                    {"name": "title", "type": "text"},
                    {"name": "doc", "type": "file", "options": {"maxSelect": 1}},
                    {"name": "docs", "type": "file", "options": {"maxSelect": 4}},
                ],
            },
        )
        assert response.status_code == 200, response.text
        names[key] = name
        ids.append(response.json()["id"])
    try:
        yield {"admin": admin, **names}
    finally:
        set_error_compat(_app(app_client), "")
        for collection_id in ids:
            await app_client.delete(f"/api/collections/{collection_id}", headers=admin)


@pytest_asyncio.fixture
async def compat_022(app_client: AsyncClient) -> AsyncIterator[None]:
    set_error_compat(_app(app_client), "0.22")
    try:
        yield
    finally:
        set_error_compat(_app(app_client), "")


async def _fresh(client: AsyncClient, env: dict, collection: str = "open") -> dict:
    response = await client.post(
        f"/api/collections/{env[collection]}/records",
        headers=env["admin"],
        data={"title": "t"},
        files=[
            ("doc", _file("alpha.txt")),
            ("docs", _file("beta1.txt")),
            ("docs", _file("beta2.txt")),
            ("docs", _file("beta3.txt")),
        ],
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _get(client: AsyncClient, env: dict, record: dict) -> dict:
    response = await client.get(
        f"/api/collections/{env['open']}/records/{record['id']}", headers=env["admin"]
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _patch(client: AsyncClient, env: dict, record: dict, **kwargs: Any):
    return await client.patch(
        f"/api/collections/{env['open']}/records/{record['id']}", **kwargs
    )


def _record_dir(client: AsyncClient, record: dict) -> Path:
    settings = _app(client).state.settings
    root = Path(settings.data_dir).expanduser().resolve() / "storage"
    path = root / record["collectionId"] / record["id"]
    assert (path / record["doc"]).is_file(), path
    return path


def _same_files(after: dict, before: dict) -> None:
    for key in ("title", "doc", "docs"):
        assert after[key] == before[key], key


# ---------------------------------------------------------------------------
# Current PocketBase semantics (the default)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["unknown", "attrs", "other_record", "separator"])
async def test_json_update_refuses_names_the_record_does_not_have(
    app_client: AsyncClient, env: dict, case: str
) -> None:
    record = await _fresh(app_client, env)
    other = await _fresh(app_client, env)
    names = {
        "unknown": "unknown_name.txt",
        "attrs": record["doc"] + ".attrs",
        "other_record": other["doc"],
        "separator": f"{other['id']}/{other['doc']}",
    }
    for body in ({"doc": names[case]}, {"docs": [*record["docs"], names[case]]}):
        field = next(iter(body))
        response = await _patch(app_client, env, record, json=body)
        assert response.status_code == 400, response.text
        error = response.json()["data"][field]
        # core/field_file.go l.257-269 (v0.40.6).
        assert error == {
            "code": "validation_invalid_file",
            "message": f"Invalid new files: [{names[case]}].",
            "params": {"invalidFiles": [names[case]]},
        }
        _same_files(await _get(app_client, env, record), record)


async def test_json_update_keeps_reorders_and_removes_known_names(
    app_client: AsyncClient, env: dict
) -> None:
    record = await _fresh(app_client, env)
    b1, b2, b3 = record["docs"]

    response = await _patch(app_client, env, record, json={"docs": [b3, b1, b2]})
    assert response.status_code == 200, response.text
    assert response.json()["docs"] == [b3, b1, b2]

    # Duplicates collapse (toSliceValue -> uniqueFiles); b3 is deleted.
    response = await _patch(app_client, env, record, json={"docs": [b1, b1, b2]})
    assert response.status_code == 200, response.text
    assert response.json()["docs"] == [b1, b2]
    files_url = f"/api/files/{env['open']}/{record['id']}"
    assert (await app_client.get(f"{files_url}/{b3}")).status_code == 404
    assert (await app_client.get(f"{files_url}/{b1}")).status_code == 200

    # A removed name cannot come back, by value or by the append modifier.
    for body in ({"docs": [b1, b2, b3]}, {"docs+": [b3]}, {"+docs": b3}):
        response = await _patch(app_client, env, record, json=body)
        assert response.status_code == 400, (body, response.text)
        assert response.json()["data"]["docs"]["code"] == "validation_invalid_file"
    assert (await _get(app_client, env, record))["docs"] == [b1, b2]

    response = await _patch(app_client, env, record, json={"docs-": [b1, "nope"]})
    assert response.status_code == 200, response.text
    assert response.json()["docs"] == [b2]
    response = await _patch(app_client, env, record, json={"doc": ""})
    assert response.status_code == 200, response.text
    assert response.json()["doc"] == ""
    assert (await app_client.get(f"{files_url}/{record['doc']}")).status_code == 404


async def test_unreferenced_storage_objects_stay_unreachable(
    app_client: AsyncClient, env: dict
) -> None:
    """The exploit: reference an object of the record directory, download it."""
    record = await _fresh(app_client, env)
    directory = _record_dir(app_client, record)
    stray = "stray_abcdefghij.txt"
    attrs = record["doc"] + ".attrs"
    (directory / stray).write_bytes(b"not referenced\n")
    (directory / attrs).write_bytes(b'{"user.metadata": {}}\n')
    files_url = f"/api/files/{env['open']}/{record['id']}"
    for name in (stray, attrs):
        assert (await app_client.get(f"{files_url}/{name}")).status_code == 404
    attempts = (
        {"json": {"doc": stray}},
        {"json": {"docs": [*record["docs"], attrs]}},
        {"data": {"doc": stray}, "files": [("docs", _file("gamma.txt"))]},
        {"files": [("@jsonPayload", (None, json.dumps({"docs+": [attrs]})))]},
    )
    for attempt in attempts:
        response = await _patch(app_client, env, record, **attempt)
        assert response.status_code == 400, (attempt, response.text)
    _same_files(await _get(app_client, env, record), record)
    for name in (stray, attrs):
        assert (await app_client.get(f"{files_url}/{name}")).status_code == 404
    # Nothing of the refused multipart attempt stays in storage.
    assert not [path for path in directory.iterdir() if path.name.startswith("gamma")]


async def test_create_refuses_submitted_names(
    app_client: AsyncClient, env: dict
) -> None:
    other = await _fresh(app_client, env)
    url = f"/api/collections/{env['open']}/records"
    for kwargs in (
        {"json": {"doc": "unknown.txt"}},
        {"json": {"docs": [other["docs"][0]]}},
        {"data": {"docs": other["docs"][0]}, "files": [("docs", _file("gamma.txt"))]},
    ):
        response = await app_client.post(url, **kwargs)
        assert response.status_code == 400, (kwargs, response.text)
        (field,) = response.json()["data"]
        assert response.json()["data"][field]["code"] == "validation_invalid_file"
    response = await app_client.post(url, json={"title": "n", "doc": "", "docs": []})
    assert response.status_code == 200, response.text


async def test_multipart_uploads_follow_current_pocketbase(
    app_client: AsyncClient, env: dict
) -> None:
    """apis/record_crud.go l.631-727 (v0.40.6)."""
    record = await _fresh(app_client, env)
    b1, b2, _b3 = record["docs"]

    response = await _patch(
        app_client, env, record, files=[("docs+", _file("gamma.txt"))]
    )
    assert response.status_code == 200, response.text
    assert [_stem(n) for n in response.json()["docs"]] == [
        "beta1",
        "beta2",
        "beta3",
        "gamma",
    ]
    response = await _patch(
        app_client,
        env,
        record,
        files=[
            ("docs", (None, b1)),
            ("docs", (None, b2)),
            ("+docs", _file("delta.txt")),
        ],
    )
    assert response.status_code == 200, response.text
    assert [_stem(n) for n in response.json()["docs"]] == ["delta", "beta1", "beta2"]
    # Uploads under the field itself replace the value, after the names sent
    # with them.
    response = await _patch(
        app_client,
        env,
        record,
        files=[("docs", (None, b1)), ("docs", _file("epsilon.txt"))],
    )
    assert response.status_code == 200, response.text
    assert [_stem(n) for n in response.json()["docs"]] == ["beta1", "epsilon"]
    response = await _patch(app_client, env, record, files=[("docs", _file("z.txt"))])
    assert response.status_code == 200, response.text
    assert [_stem(n) for n in response.json()["docs"]] == ["z"]
    # A single field keeps the last upload.
    response = await _patch(
        app_client,
        env,
        record,
        files=[("doc", _file("first.txt")), ("doc", _file("last.txt"))],
    )
    assert response.status_code == 200, response.text
    assert _stem(response.json()["doc"]) == "last"
    response = await _patch(
        app_client,
        env,
        record,
        files=[("docs+", _file(f"more{index}.txt")) for index in range(4)],
    )
    assert response.status_code == 400, response.text
    assert response.json()["data"]["docs"] == {
        "code": "validation_too_many_files",
        "message": "The maximum allowed files is 4.",
        "params": {"maxSelect": 4},
    }


async def test_batch_refuses_unknown_names(app_client: AsyncClient, env: dict) -> None:
    record = await _fresh(app_client, env)
    response = await app_client.post(
        "/api/batch",
        headers=env["admin"],
        json={
            "requests": [
                {
                    "method": "PATCH",
                    "url": f"/api/collections/{env['open']}/records/{record['id']}",
                    "body": {"title": "changed", "doc": "unknown.txt"},
                },
                {
                    "method": "POST",
                    "url": f"/api/collections/{env['open']}/records",
                    "body": {"docs": record["docs"][:1]},
                },
            ]
        },
    )
    assert response.status_code == 400, response.text
    nested = response.json()["data"]["requests"]["0"]["response"]
    assert nested["data"]["doc"]["code"] == "validation_invalid_file"
    _same_files(await _get(app_client, env, record), record)


async def test_service_checks_names_only_for_api_semantics(
    app_client: AsyncClient, env: dict
) -> None:
    """The record API passes ``file_semantics``; server-side callers do not."""
    response = await app_client.post(
        f"/api/collections/{env['open']}/records", json={"title": "s"}
    )
    assert response.status_code == 200, response.text
    record_id = response.json()["id"]
    engine = get_engine()
    collection = await resolve_collection(engine, env["open"])
    with pytest.raises(FileNamesLoadError):
        await update_record(
            engine, collection, record_id, {"doc": "x.txt"}, file_semantics="0.22"
        )
    with pytest.raises(_ValidationErrors) as refused:
        await update_record(
            engine, collection, record_id, {"doc": "x.txt"}, file_semantics="current"
        )
    assert refused.value.errors["doc"]["code"] == "validation_invalid_file"
    updated = await update_record(engine, collection, record_id, {"doc": "x.txt"})
    assert updated is not None and updated["doc"] == "x.txt"


# ---------------------------------------------------------------------------
# PocketBase 0.22.34 semantics (error_compat "0.22")
# ---------------------------------------------------------------------------


async def test_022_unknown_name_is_a_load_error(
    app_client: AsyncClient, env: dict, compat_022: None
) -> None:
    record = await _fresh(app_client, env)
    response = await _patch(
        app_client,
        env,
        record,
        json={"title": "changed", "doc": "u1.txt", "docs": ["u2.txt"]},
    )
    assert response.status_code == 400
    # Only the first refused field, in schema order (record_upsert.go l.415-439).
    assert response.content == (
        b'{"code":400,"message":"' + LOAD_ERROR.encode() + b'","data":{"doc":'
        b'{"code":"validation_unknown_filenames","message":"The field contains '
        b'unknown filenames."}}}\n'
    )
    _same_files(await _get(app_client, env, record), record)
    response = await app_client.post(
        f"/api/collections/{env['open']}/records", json={"doc": record["doc"]}
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": 400,
        "message": LOAD_ERROR,
        "data": {"doc": UNKNOWN},
    }


async def test_022_multipart_and_modifiers(
    app_client: AsyncClient, env: dict, compat_022: None
) -> None:
    record = await _fresh(app_client, env)
    b1, b2, b3 = record["docs"]
    # ``docs+`` leaves a file field as is; ``+docs`` is not a modifier.
    for body in ({"docs+": ["unknown.txt"]}, {"+docs": ["unknown.txt"]}):
        response = await _patch(app_client, env, record, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["docs"] == [b1, b2, b3]
    # Uploads under the field are appended; ``docs+`` uploads are ignored.
    response = await _patch(
        app_client,
        env,
        record,
        files=[("docs", _file("gamma.txt")), ("docs+", _file("zeta.txt"))],
    )
    assert response.status_code == 200, response.text
    docs = response.json()["docs"]
    assert [_stem(n) for n in docs] == ["beta1", "beta2", "beta3", "gamma"]
    response = await _patch(
        app_client,
        env,
        record,
        files=[("docs", _file("delta.txt"))],
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": 400,
        "message": "Something went wrong while processing your request.",
        "data": {
            "docs": {
                "code": "validation_too_many_values",
                "message": "Select no more than 4.",
            }
        },
    }
    # Legacy deletions: ``docs.<name>`` and ``docs.<index>`` set to empty.
    response = await _patch(
        app_client, env, record, json={f"docs.{b2}": "", "docs.0": None}
    )
    assert response.status_code == 200, response.text
    assert response.json()["docs"] == [b3, docs[3]]
    # A single field keeps the first upload (AddFiles).
    response = await _patch(
        app_client,
        env,
        record,
        files=[("doc", _file("first.txt")), ("doc", _file("last.txt"))],
    )
    assert response.status_code == 200, response.text
    assert _stem(response.json()["doc"]) == "first"


async def test_022_access_errors_come_first(
    app_client: AsyncClient, env: dict, compat_022: None
) -> None:
    locked = await _fresh(app_client, env, "locked")
    url = f"/api/collections/{env['locked']}/records"
    for method, target in (("PATCH", f"{url}/{locked['id']}"), ("POST", url)):
        response = await app_client.request(method, target, json={"doc": "x.txt"})
        assert response.status_code == 403, response.text
        assert response.json()["message"] == "Only admins can perform this action."
    response = await app_client.patch(
        f"/api/collections/{env['open']}/records/missingrecord00",
        json={"doc": "x.txt"},
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Other paths into a record: request hooks, OAuth2 createData, multipart
# text values, server-side repositories
# ---------------------------------------------------------------------------


def _load_error_bytes(field: str) -> bytes:
    return (
        b'{"code":400,"message":"'
        + LOAD_ERROR.encode()
        + b'","data":{"'
        + field.encode()
        + b'":{"code":"validation_unknown_filenames","message":"The field contains '
        b'unknown filenames."}}}\n'
    )


@pytest.mark.parametrize("mode", ["", "0.22"])
async def test_request_hooks_cannot_inject_names(
    app_client: AsyncClient, env: dict, mode: str
) -> None:
    """A name that a request hook writes in ``e.data`` is checked after it.

    v0.40.6 saves after the hooks with ``app.Save``, which validates
    (apis/record_crud.go l.346-352, l.485-491).  0.22.34 validates the form
    before the ``OnRecordBefore*Request`` hooks and ``dao.SaveRecord`` saves
    what they set on ``e.Record`` (forms/record_upsert.go l.744-749, l.809;
    apis/record_crud.go l.280-284, l.372-376): PPBase refuses that name in
    the 0.22 mode too, with the 0.22 load error (deliberate divergence).
    """
    other = await _fresh(app_client, env)
    record = await _fresh(app_client, env)
    app = _app(app_client)
    previous = getattr(app.state, "extension_registry", None)
    registry = previous or ExtensionRegistry()
    app.state.extension_registry = registry
    injected = {
        HOOK_RECORD_CREATE_REQUEST: ("doc", other["doc"]),
        HOOK_RECORD_UPDATE_REQUEST: ("docs", [*record["docs"], other["docs"][0]]),
    }
    bound = []
    for hook_name, (field, value) in injected.items():

        async def inject(e: Any, field: str = field, value: Any = value) -> Any:
            if getattr(e.collection, "name", None) == env["open"]:
                e.data = {**e.data, field: value}
            return await e.next()

        bound.append((hook_name, registry.get_hook(hook_name).bind_func(inject)))
    set_error_compat(app, mode)
    url = f"/api/collections/{env['open']}/records"
    try:
        created = await app_client.post(url, json={"title": "hook-injected"})
        updated = await _patch(app_client, env, record, json={"title": "hooked"})
    finally:
        set_error_compat(app, "")
        for hook_name, binding in bound:
            registry.get_hook(hook_name).unbind(binding)
        app.state.extension_registry = previous
    for response, (field, value) in zip(
        (created, updated), injected.values(), strict=True
    ):
        assert response.status_code == 400, response.text
        if mode == "0.22":
            assert response.content == _load_error_bytes(field)
        else:
            name = value if isinstance(value, str) else value[-1]
            assert response.json()["data"][field] == {
                "code": "validation_invalid_file",
                "message": f"Invalid new files: [{name}].",
                "params": {"invalidFiles": [name]},
            }
    after = await _get(app_client, env, record)
    _same_files(after, record)
    assert after["title"] == record["title"]
    listed = await app_client.get(
        url, params={"filter": 'title="hook-injected"'}, headers=env["admin"]
    )
    assert listed.status_code == 200 and listed.json()["totalItems"] == 0


class _FakeOAuth2Provider:
    """In-process provider: the code names the provider user (no network)."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        pass

    async def exchange_code(self, code: str, redirect_url: str, verifier: str):
        return {"access_token": f"token-{code}"}

    async def get_user_info(self, access_token: str) -> dict:
        user = access_token.removeprefix("token-")
        return {
            "id": f"provider-{user}",
            "email": f"{user}@example.invalid",
            "name": "OAuth2 User",
            "avatarURL": "https://avatars.example.invalid/a.png",
        }


@pytest_asyncio.fixture
async def oauth2_env(
    app_client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict]:
    monkeypatch.setattr(
        oauth2_service, "get_provider_credentials", lambda *a, **k: ("id", "secret")
    )
    monkeypatch.setattr(
        oauth2_service, "get_provider_class", lambda name: _FakeOAuth2Provider
    )
    admin = {"Authorization": admin_token}
    name = f"fnames_oauth_{uuid.uuid4().hex[:8]}"
    response = await app_client.post(
        "/api/collections",
        headers=admin,
        json={
            "name": name,
            "type": "auth",
            **RULES,
            "options": {
                "oauth2": {"enabled": True, "mappedFields": {"avatarURL": "avatar"}}
            },
            "schema": [
                {"name": "title", "type": "text"},
                {"name": "avatar", "type": "file", "options": {"maxSelect": 1}},
                {"name": "docs", "type": "file", "options": {"maxSelect": 4}},
                {"name": "later", "type": "text"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    collection = response.json()
    root = Path(_app(app_client).state.settings.data_dir).expanduser().resolve()
    try:
        yield {"admin": admin, "name": name, "id": collection["id"], "root": root}
    finally:
        set_error_compat(_app(app_client), "")
        await app_client.delete(f"/api/collections/{collection['id']}", headers=admin)


async def _oauth2(client: AsyncClient, oauth2_env: dict, code: str, data: dict):
    return await client.post(
        f"/api/collections/{oauth2_env['name']}/auth-with-oauth2",
        json={
            "provider": "google",
            "code": code,
            "codeVerifier": "verifier",
            "redirectUrl": "http://localhost/callback",
            "createData": data,
        },
    )


def _leftover(oauth2_env: dict, record_id: str) -> str:
    """An unreferenced object in the directory of a record id still unused."""
    directory = oauth2_env["root"] / "storage" / oauth2_env["id"] / record_id
    directory.mkdir(parents=True)
    (directory / "stray_abcdefghij.txt").write_bytes(b"leftover object\n")
    return "stray_abcdefghij.txt"


async def test_oauth2_create_data_cannot_reference_files(
    app_client: AsyncClient, oauth2_env: dict
) -> None:
    """Current semantics: createData file names are refused (v0.40.6).

    v0.40.6 creates the OAuth2 record by an internal ``POST /records``
    (apis/record_auth_with_oauth2.go l.319, l.403-425), where
    ``FileField.ValidateValue`` refuses them (core/field_file.go l.250-269).
    The provider avatar URL that PPBase maps into a file field is a value of
    the server, still stored.
    """
    files_url = f"/api/files/{oauth2_env['name']}"
    records_url = f"/api/collections/{oauth2_env['name']}/records"
    chosen = "chosenrecord001"
    stray = _leftover(oauth2_env, chosen)
    for code, data in (
        ("u1", {"id": chosen, "avatar": stray}),
        ("u2", {"id": chosen, "docs": [stray]}),
        ("u3", {"id": chosen, "docs+": [stray]}),
    ):
        response = await _oauth2(app_client, oauth2_env, code, data)
        assert response.status_code == 400, response.text
        assert "validation_invalid_file" in response.json()["message"], response.text
        assert stray in response.json()["message"]
    view = await app_client.get(f"{records_url}/{chosen}", headers=oauth2_env["admin"])
    assert view.status_code == 404, view.text
    assert (await app_client.get(f"{files_url}/{chosen}/{stray}")).status_code == 404

    response = await _oauth2(app_client, oauth2_env, "u4", {"title": "t", "docs": []})
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["avatar"] == "https://avatars.example.invalid/a.png"
    assert (record["title"], record["docs"]) == ("t", [])


async def test_022_oauth2_create_data_file_names_are_not_loaded(
    app_client: AsyncClient, oauth2_env: dict
) -> None:
    """0.22 mode: like 0.22.34 ``LoadData``, whose error OAuth2 ignores.

    forms/record_oauth2_login.go l.227 drops the error of
    ``createForm.LoadData(form.CreateData)``, which returns at the first
    file field naming a file the new record does not have
    (forms/record_upsert.go l.415-439): that field and the schema fields
    after it are not loaded, the record is created (measured by
    tests/pocketbase_parity/file_names_probe.py).
    """
    set_error_compat(_app(app_client), "0.22")
    files_url = f"/api/files/{oauth2_env['name']}"
    chosen = "chosenrecord002"
    stray = _leftover(oauth2_env, chosen)
    response = await _oauth2(
        app_client,
        oauth2_env,
        "v1",
        {"id": chosen, "title": "t", "avatar": stray, "later": "l"},
    )
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["id"] == chosen
    # The client addressed avatar: no provider URL, and nothing loaded after.
    assert (record["title"], record["avatar"], record["later"]) == ("t", "", "")
    assert (await app_client.get(f"{files_url}/{chosen}/{stray}")).status_code == 404

    response = await _oauth2(
        app_client,
        oauth2_env,
        "v2",
        {"title": "t2", "avatar": "", "docs": ["x.txt"], "later": "l2"},
    )
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert (record["title"], record["avatar"], record["docs"]) == ("t2", "", [])
    assert record["later"] == ""

    response = await _oauth2(
        app_client, oauth2_env, "v3", {"title": "t3", "docs": [], "later": "l3"}
    )
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert (record["title"], record["later"]) == ("t3", "l3")
    assert record["avatar"] == "https://avatars.example.invalid/a.png"


async def test_multipart_text_json_array_follows_each_version(
    app_client: AsyncClient, env: dict
) -> None:
    """A multipart text ``docs='["b2","b1"]'``: names in v0.40.6, one name in 0.22.34.

    0.22.34 keeps a file field's text values as ``[]string`` (forms/
    record_upsert.go l.191-196), never JSON-decoded (tools/list/list.go
    l.115-116); other keys keep their first value as a string (l.195), that
    ``docs-`` decodes.  v0.40.6 decodes the text (core/field_file.go
    l.728-762).
    """
    record = await _fresh(app_client, env)
    b1, b2, b3 = record["docs"]
    reordered = [("docs", (None, json.dumps([b3, b1, b2])))]
    response = await _patch(app_client, env, record, files=reordered)
    assert response.status_code == 200, response.text
    assert response.json()["docs"] == [b3, b1, b2]

    set_error_compat(_app(app_client), "0.22")
    try:
        response = await _patch(
            app_client, env, record, files=[("docs", (None, json.dumps([b1, b2])))]
        )
        assert response.status_code == 400
        assert response.content == _load_error_bytes("docs")
        assert (await _get(app_client, env, record))["docs"] == [b3, b1, b2]
        response = await _patch(
            app_client, env, record, files=[("docs-", (None, json.dumps([b1])))]
        )
        assert response.status_code == 200, response.text
        assert response.json()["docs"] == [b3, b2]
    finally:
        set_error_compat(_app(app_client), "")


async def test_repositories_check_names_like_app_save(
    app_client: AsyncClient, env: dict
) -> None:
    """``pb.records()`` / ``e.records()``: v0.40.6 ``app.Save`` checks names.

    core/db.go l.178-187, l.280-286 and core/record_model.go l.1427-1433
    (v0.40.6); 0.22.34 ``dao.SaveRecord`` does not validate (daos/record.go
    l.586-610), so the 0.22 mode keeps accepting them.
    """
    record = await _fresh(app_client, env)
    engine = get_engine()
    stray = "stray_abcdefghij.txt"
    repository = RecordRepository(env["open"], engine=engine)
    assert repository.file_semantics == CURRENT
    with pytest.raises(_ValidationErrors) as refused:
        await repository.update(record["id"], {"doc": stray})
    assert refused.value.errors["doc"]["params"] == {"invalidFiles": [stray]}
    with pytest.raises(_ValidationErrors):
        await repository.create({"docs": [record["docs"][0]]})
    updated = await repository.update(record["id"], {"docs": record["docs"][::-1]})
    assert updated is not None and updated["docs"] == record["docs"][::-1]

    app = _app(app_client)
    event = RecordRequestEvent(app=app, engine=engine)
    assert event.records(env["open"]).file_semantics == CURRENT
    set_error_compat(app, "0.22")
    try:
        assert event.records(env["open"]).file_semantics is None
        stored = await event.records(env["open"]).update(record["id"], {"doc": stray})
    finally:
        set_error_compat(app, "")
    assert stored is not None and stored["doc"] == stray
