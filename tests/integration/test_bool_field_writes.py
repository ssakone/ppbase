"""Bool values written through the records API are stored like PocketBase.

PocketBase 0.22.34 converts every bool field value of a record write with
``cast.ToBool`` (``models/schema/schema_field.go`` l.346-347,
``forms/record_upsert.go`` l.366-417), as v0.40.6 does (``core/field_bool.go``
l.103-106): ``strconv.ParseBool`` for a string, false for any parse error.
The JS SDK sends booleans as ``"true"``/``"false"`` strings in a FormData as
soon as a file is attached; ``bool("false")`` stored them as true.  The
expected values are those measured on the official 0.22.34 binary by
``tests/pocketbase_parity/bool_values_probe.py``.  Every case checks the
response, the record read back and the PostgreSQL column.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text

from ppbase.db.engine import get_engine
from ppbase.middleware.error_compat import set_error_compat

pytestmark = pytest.mark.asyncio

PUBLIC = {
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

TRUE_STRINGS = ("1", "t", "T", "TRUE", "true", "True")
FALSE_STRINGS = ("0", "f", "F", "FALSE", "false", "False")
# strconv.ParseBool errors, false on both 0.22.34 and v0.40.6 (multipart
# numbers other than 0 and 1, true only in v0.40.6, are kept apart below).
ERROR_STRINGS = ("", " true", "true ", "tRUE", "yes", "on", "no", "null", "1.0", "-0")
# Canonical numbers of a multipart value: ParseBool error, false in 0.22.34;
# v0.40.6 infers a number first (tools/router/unmarshal_request_data.go
# l.310-342), true.  PPBase follows 0.22.34 (D-XXXX).
MULTIPART_NUMBERS = ("2", "-1", "0.5", "10")


def _app(client: AsyncClient) -> Any:
    return client._transport.app  # ASGITransport(app=...)


@pytest_asyncio.fixture(scope="module")
async def bool_env(app_client: AsyncClient, admin_token: str) -> AsyncIterator[dict]:
    sfx = uuid.uuid4().hex[:8]
    admin = {"Authorization": admin_token}
    names = {"flags": f"boolw_flags_{sfx}", "required": f"boolw_req_{sfx}"}
    ids: dict[str, str] = {}
    schemas = {
        "flags": [
            {"name": "label", "type": "text", "options": {}},
            {"name": "flag", "type": "bool", "options": {}},
            {
                "name": "doc",
                "type": "file",
                "options": {"maxSelect": 1, "maxSize": 5242880},
            },
        ],
        "required": [
            {"name": "label", "type": "text", "options": {}},
            {"name": "req", "type": "bool", "required": True, "options": {}},
            {
                "name": "doc",
                "type": "file",
                "options": {"maxSelect": 1, "maxSize": 5242880},
            },
        ],
    }
    for key, name in names.items():
        response = await app_client.post(
            "/api/collections",
            headers=admin,
            json={"name": name, "type": "base", "schema": schemas[key], **PUBLIC},
        )
        assert response.status_code == 200, response.text
        ids[key] = response.json()["id"]
    try:
        yield {"admin": admin, **names}
    finally:
        for collection_id in ids.values():
            await app_client.delete(f"/api/collections/{collection_id}", headers=admin)


@pytest_asyncio.fixture
async def compat_022(app_client: AsyncClient) -> AsyncIterator[None]:
    app = _app(app_client)
    set_error_compat(app, "0.22")
    try:
        yield
    finally:
        set_error_compat(app, "")


async def _stored(collection: str, record_id: str, column: str = "flag") -> Any:
    async with get_engine().connect() as conn:
        return (
            await conn.execute(
                text(f'SELECT "{column}" FROM "{collection}" WHERE "id" = :id'),
                {"id": record_id},
            )
        ).scalar_one()


async def _check(
    client: AsyncClient, collection: str, body: dict, column: str = "flag"
) -> bool:
    """The written value, identical in the response, a read and the column."""
    value = body[column]
    assert isinstance(value, bool), body
    read = await client.get(f"/api/collections/{collection}/records/{body['id']}")
    assert read.status_code == 200, read.text
    assert read.json()[column] is value
    assert await _stored(collection, body["id"], column) is value
    return value


async def _create_json(client: AsyncClient, collection: str, payload: dict) -> bool:
    response = await client.post(f"/api/collections/{collection}/records", json=payload)
    assert response.status_code == 200, response.text
    return await _check(client, collection, response.json())


async def _create_form(
    client: AsyncClient,
    collection: str,
    fields: list[tuple[str, str]],
    *,
    with_file: bool,
) -> bool:
    files: list[tuple[str, Any]] = [(key, (None, value)) for key, value in fields]
    if with_file:
        files.append(("doc", ("pixel.png", PNG, "image/png")))
    response = await client.post(f"/api/collections/{collection}/records", files=files)
    assert response.status_code == 200, response.text
    return await _check(client, collection, response.json())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), (None, False), (0, False), (1, True), (0.5, True)]
    + [(value, True) for value in TRUE_STRINGS]
    + [(value, False) for value in FALSE_STRINGS + ERROR_STRINGS + MULTIPART_NUMBERS]
    + [([], False), ([True], False), (["true"], False), ({}, False), ({"a": 1}, False)],
)
async def test_json_create_stores_the_cast_value(
    app_client: AsyncClient, bool_env: dict, raw: Any, expected: bool
) -> None:
    payload = {"label": "json", "flag": raw}
    assert await _create_json(app_client, bool_env["flags"], payload) is expected


@pytest.mark.parametrize("with_file", [False, True], ids=["form", "form+file"])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(value, True) for value in TRUE_STRINGS]
    + [(value, False) for value in FALSE_STRINGS + ERROR_STRINGS + MULTIPART_NUMBERS],
)
async def test_multipart_create_stores_the_cast_value(
    app_client: AsyncClient,
    bool_env: dict,
    raw: str,
    expected: bool,
    with_file: bool,
) -> None:
    fields = [("label", "form"), ("flag", raw)]
    stored = await _create_form(
        app_client, bool_env["flags"], fields, with_file=with_file
    )
    assert stored is expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"flag": "false"}, False),
        ({"flag": "0"}, False),
        ({"flag": False}, False),
        ({"flag": 0}, False),
        ({"flag": "true"}, True),
        ({"flag": "1"}, True),
        ({"flag": True}, True),
        ({"flag": [True]}, False),
    ],
)
async def test_multipart_json_payload_stores_the_cast_value(
    app_client: AsyncClient, bool_env: dict, payload: dict, expected: bool
) -> None:
    fields = [("label", "payload"), ("@jsonPayload", json.dumps(payload))]
    stored = await _create_form(app_client, bool_env["flags"], fields, with_file=True)
    assert stored is expected


@pytest.mark.parametrize(
    "values",
    [["true", "false"], ["false", "true"], ["true", "true"]],
    ids=["true-false", "false-true", "true-true"],
)
async def test_repeated_multipart_key_is_a_list_hence_false(
    app_client: AsyncClient, bool_env: dict, values: list[str]
) -> None:
    # A repeated key arrives as a list: false, like v0.40.6 (a []any value,
    # unmarshal_request_data.go l.84-90).  0.22.34 reads its first value
    # (forms/record_upsert.go l.191-196): true for "true", ... (D-XXXX).
    fields = [("label", "repeated"), *(("flag", value) for value in values)]
    stored = await _create_form(app_client, bool_env["flags"], fields, with_file=True)
    assert stored is False


async def test_update_converts_json_and_multipart_values(
    app_client: AsyncClient, bool_env: dict
) -> None:
    collection = bool_env["flags"]
    created = await app_client.post(
        f"/api/collections/{collection}/records", json={"label": "upd", "flag": True}
    )
    assert created.status_code == 200, created.text
    record_id = created.json()["id"]
    url = f"/api/collections/{collection}/records/{record_id}"
    steps: list[tuple[str, Any, bool]] = [
        ("json", "false", False),
        ("json", "true", True),
        ("json", "0", False),
        ("json", "1", True),
        ("form", "false", False),
        ("form", "True", True),
        ("form+file", "0", False),
        ("form+file", "true", True),
        ("payload", "false", False),
        ("payload", True, True),
        ("json", "yes", False),
        ("json", True, True),
        ("json", None, False),
    ]
    for kind, raw, expected in steps:
        if kind == "json":
            response = await app_client.patch(url, json={"flag": raw})
        elif kind == "payload":
            response = await app_client.patch(
                url,
                files=[
                    ("@jsonPayload", (None, json.dumps({"flag": raw}))),
                    ("doc", ("pixel.png", PNG, "image/png")),
                ],
            )
        else:
            files: list[tuple[str, Any]] = [("flag", (None, raw))]
            if kind == "form+file":
                files.append(("doc", ("pixel.png", PNG, "image/png")))
            response = await app_client.patch(url, files=files)
        assert response.status_code == 200, (kind, raw, response.text)
        assert await _check(app_client, collection, response.json()) is expected, (
            kind,
            raw,
        )


async def test_batch_converts_json_and_multipart_values(
    app_client: AsyncClient, bool_env: dict
) -> None:
    collection = bool_env["flags"]
    admin = bool_env["admin"]
    records_url = f"/api/collections/{collection}/records"
    response = await app_client.post(
        "/api/batch",
        headers=admin,
        json={
            "requests": [
                {"method": "POST", "url": records_url, "body": {"flag": "false"}},
                {"method": "POST", "url": records_url, "body": {"flag": "0"}},
                {"method": "POST", "url": records_url, "body": {"flag": "true"}},
                {"method": "POST", "url": records_url, "body": {"flag": False}},
            ]
        },
    )
    assert response.status_code == 200, response.text
    bodies = [item["body"] for item in response.json()]
    stored = [await _check(app_client, collection, body) for body in bodies]
    assert stored == [False, False, True, False]

    record_id = bodies[2]["id"]
    payload = {
        "requests": [
            {
                "method": "PATCH",
                "url": f"{records_url}/{record_id}",
                "body": {"flag": "false"},
            },
            {"method": "POST", "url": records_url, "body": {"flag": "0"}},
        ]
    }
    response = await app_client.post(
        "/api/batch",
        headers=admin,
        files=[
            ("@jsonPayload", (None, json.dumps(payload))),
            ("requests.1.doc", ("pixel.png", PNG, "image/png")),
        ],
    )
    assert response.status_code == 200, response.text
    bodies = [item["body"] for item in response.json()]
    assert bodies[0]["id"] == record_id
    assert bodies[1]["doc"]
    stored = [await _check(app_client, collection, body) for body in bodies]
    assert stored == [False, False]


async def test_required_bool_still_must_be_true(
    app_client: AsyncClient, bool_env: dict
) -> None:
    collection = bool_env["required"]
    url = f"/api/collections/{collection}/records"
    refused = [
        await app_client.post(url, json={"req": "false"}),
        await app_client.post(url, json={"req": "0"}),
        await app_client.post(url, files=[("req", (None, "false"))]),
        await app_client.post(
            url,
            files=[("req", (None, "false")), ("doc", ("pixel.png", PNG, "image/png"))],
        ),
    ]
    for response in refused:
        assert response.status_code == 400, response.text
        assert response.json()["data"]["req"] == {
            "code": "validation_required",
            "message": "Must be true.",
        }
    accepted = await app_client.post(
        url, files=[("req", (None, "true")), ("doc", ("pixel.png", PNG, "image/png"))]
    )
    assert accepted.status_code == 200, accepted.text
    assert await _check(app_client, collection, accepted.json(), "req") is True

    record_url = f"{url}/{accepted.json()['id']}"
    response = await app_client.patch(record_url, files=[("req", (None, "0"))])
    assert response.status_code == 400, response.text
    assert response.json()["data"]["req"]["code"] == "validation_required"
    assert await _stored(collection, accepted.json()["id"], "req") is True


async def test_022_mode_multipart_values_are_cast_too(
    app_client: AsyncClient, bool_env: dict, compat_022: None
) -> None:
    # The 0.22 mode reads multipart text as MultipartText (records.py).
    collection = bool_env["flags"]
    for raw, expected in (("false", False), ("0", False), ("true", True), ("2", False)):
        stored = await _create_form(
            app_client,
            collection,
            [("label", "compat"), ("flag", raw)],
            with_file=True,
        )
        assert stored is expected, raw

    response = await app_client.post(
        f"/api/collections/{bool_env['required']}/records",
        files=[("req", (None, "false")), ("doc", ("pixel.png", PNG, "image/png"))],
    )
    assert response.status_code == 400, response.text
    assert response.json()["data"]["req"] == {
        "code": "validation_required",
        "message": "Missing required value.",
    }
