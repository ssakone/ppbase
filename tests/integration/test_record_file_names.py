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
from ppbase.middleware.error_compat import set_error_compat
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
