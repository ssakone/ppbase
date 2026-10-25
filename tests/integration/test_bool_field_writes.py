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

A repeated multipart key keeps its first value in the 0.22 mode, as 0.22.34
does for a non-arrayable key (``forms/record_upsert.go`` l.191-196), and is a
list, false, in the default mode, as in v0.40.6
(``tools/router/unmarshal_request_data.go`` l.84-90).  The modifier keys
``flag+``, ``flag-`` and ``+flag`` leave a bool field unchanged in both
versions (0.22.34 ``models/schema/schema_field.go`` l.394-429; v0.40.6
``core/record_model.go`` l.869-889).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
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
# l.310-342), true.  PPBase follows 0.22.34 in both modes: neither has an
# inferValue step, for any field type (D-XXXX).
MULTIPART_NUMBERS = ("2", "-1", "0.5", "10")


def _app(client: AsyncClient) -> Any:
    return client._transport.app  # ASGITransport(app=...)


@pytest_asyncio.fixture(scope="module")
async def bool_env(app_client: AsyncClient, admin_token: str) -> AsyncIterator[dict]:
    sfx = uuid.uuid4().hex[:8]
    admin = {"Authorization": admin_token}
    names = {
        "flags": f"boolw_flags_{sfx}",
        "required": f"boolw_req_{sfx}",
        "related": f"boolw_rel_{sfx}",
        "users": f"boolw_users_{sfx}",
    }
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
    for key in ("flags", "required"):
        response = await app_client.post(
            "/api/collections",
            headers=admin,
            json={"name": names[key], "type": "base", "schema": schemas[key], **PUBLIC},
        )
        assert response.status_code == 200, response.text
        ids[key] = response.json()["id"]
    related = [
        {"name": "label", "type": "text", "options": {}},
        {"name": "req", "type": "bool", "required": True, "options": {}},
        {
            "name": "rel",
            "type": "relation",
            "options": {"collectionId": ids["flags"], "maxSelect": 1},
        },
    ]
    for key, kind, schema in (("related", "base", related), ("users", "auth", [])):
        response = await app_client.post(
            "/api/collections",
            headers=admin,
            json={"name": names[key], "type": kind, "schema": schema, **PUBLIC},
        )
        assert response.status_code == 200, response.text
        ids[key] = response.json()["id"]
    try:
        yield {"admin": admin, **names}
    finally:
        for collection_id in reversed(list(ids.values())):
            await app_client.delete(f"/api/collections/{collection_id}", headers=admin)


@pytest_asyncio.fixture
async def compat_022(app_client: AsyncClient) -> AsyncIterator[None]:
    app = _app(app_client)
    set_error_compat(app, "0.22")
    try:
        yield
    finally:
        set_error_compat(app, "")


@contextlib.contextmanager
def _mode(client: AsyncClient, mode: str) -> Iterator[None]:
    app = _app(client)
    set_error_compat(app, "0.22" if mode == "0.22" else "")
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


REPEATED = (
    ["true", "false"],
    ["false", "true"],
    ["true", "true"],
    ["false", "false"],
    ["T", ""],
    ["", "true"],
    ["1", "0"],
)


@pytest.mark.parametrize("with_file", [False, True], ids=["form", "form+file"])
@pytest.mark.parametrize("values", REPEATED, ids="-".join)
async def test_repeated_multipart_key_is_a_list_hence_false(
    app_client: AsyncClient, bool_env: dict, values: list[str], with_file: bool
) -> None:
    # Default mode: a repeated key arrives as a list, false like v0.40.6
    # (a []any value, unmarshal_request_data.go l.84-90).
    fields = [("label", "repeated"), *(("flag", value) for value in values)]
    stored = await _create_form(
        app_client, bool_env["flags"], fields, with_file=with_file
    )
    assert stored is False


@pytest.mark.parametrize("with_file", [False, True], ids=["form", "form+file"])
@pytest.mark.parametrize("values", REPEATED, ids="-".join)
async def test_022_mode_repeated_multipart_key_keeps_its_first_value(
    app_client: AsyncClient,
    bool_env: dict,
    compat_022: None,
    values: list[str],
    with_file: bool,
) -> None:
    # 0.22.34 extractMultipartFormData: values[0] of a non-arrayable key
    # (forms/record_upsert.go l.191-196), then cast.ToBool.
    fields = [("label", "repeated"), *(("flag", value) for value in values)]
    stored = await _create_form(
        app_client, bool_env["flags"], fields, with_file=with_file
    )
    assert stored is (values[0] in TRUE_STRINGS)


@pytest.mark.parametrize("mode", ["default", "0.22"])
async def test_update_with_a_repeated_multipart_key(
    app_client: AsyncClient, bool_env: dict, mode: str
) -> None:
    collection = bool_env["flags"]
    with _mode(app_client, mode):
        for initial, values in ((False, ["true", "false"]), (True, ["0", "1"])):
            created = await app_client.post(
                f"/api/collections/{collection}/records", json={"flag": initial}
            )
            assert created.status_code == 200, created.text
            url = f"/api/collections/{collection}/records/{created.json()['id']}"
            response = await app_client.patch(
                url, files=[("flag", (None, value)) for value in values]
            )
            assert response.status_code == 200, response.text
            expected = values[0] in TRUE_STRINGS if mode == "0.22" else False
            assert await _check(app_client, collection, response.json()) is expected


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
    # A JSON array of @jsonPayload is no repeated multipart key: false.
    for payload in ({"flag": ["true"]}, {"flag": [True, False]}):
        fields = [("label", "payload"), ("@jsonPayload", json.dumps(payload))]
        stored = await _create_form(app_client, collection, fields, with_file=True)
        assert stored is False, payload

    response = await app_client.post(
        f"/api/collections/{bool_env['required']}/records",
        files=[("req", (None, "false")), ("doc", ("pixel.png", PNG, "image/png"))],
    )
    assert response.status_code == 400, response.text
    assert response.json()["data"]["req"] == {
        "code": "validation_required",
        "message": "Missing required value.",
    }


MODIFIER_KEYS = ("flag+", "flag-", "+flag")


async def _patch(client: AsyncClient, url: str, write: tuple[str, Any]) -> Any:
    kind, body = write
    if kind == "json":
        return await client.patch(url, json=body)
    return await client.patch(url, files=[(key, (None, raw)) for key, raw in body])


@pytest.mark.parametrize("mode", ["default", "0.22"])
@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("key", MODIFIER_KEYS)
async def test_bool_modifier_keys_leave_the_field_unchanged_on_update(
    app_client: AsyncClient, bool_env: dict, mode: str, initial: bool, key: str
) -> None:
    # 0.22.34 PrepareValueWithModifier has no bool case and returns
    # PrepareValue(base), base being the current value (models/record.go
    # l.616-629, models/schema/schema_field.go l.394-429); "+flag" is no
    # 0.22.34 modifier.  v0.40.6 ignores the three keys (BoolField has no
    # FindSetter, core/record_model.go l.869-889).
    collection = bool_env["flags"]
    writes: list[tuple[str, Any]] = [
        ("json", {key: "x"}),
        ("json", {key: True}),
        ("json", {key: "true"}),
        ("json", {key: "false"}),
        ("json", {key: ["true"]}),
        ("form", [(key, "x")]),
        ("form", [(key, "true")]),
        ("form", [(key, "false")]),
    ]
    with _mode(app_client, mode):
        created = await app_client.post(
            f"/api/collections/{collection}/records", json={"flag": initial}
        )
        assert created.status_code == 200, created.text
        url = f"/api/collections/{collection}/records/{created.json()['id']}"
        for write in writes:
            response = await _patch(app_client, url, write)
            assert response.status_code == 200, (write, response.text)
            stored = await _check(app_client, collection, response.json())
            assert stored is initial, write


@pytest.mark.parametrize("mode", ["default", "0.22"])
@pytest.mark.parametrize("key", MODIFIER_KEYS)
@pytest.mark.parametrize("explicit", ["true", "false"])
async def test_bool_modifier_keys_keep_the_request_value_on_update(
    app_client: AsyncClient, bool_env: dict, mode: str, key: str, explicit: str
) -> None:
    # The explicit "flag" value is the base of the modifier (0.22.34) or the
    # only key set (v0.40.6), whatever the key order.
    collection = bool_env["flags"]
    expected = explicit == "true"
    writes: list[tuple[str, Any]] = [
        ("json", {"flag": explicit, key: "x"}),
        ("json", {key: "x", "flag": explicit}),
        ("json", {key: explicit == "false", "flag": explicit}),
        ("form", [("flag", explicit), (key, "x")]),
        ("form", [(key, "true"), ("flag", explicit)]),
    ]
    with _mode(app_client, mode):
        for write in writes:
            created = await app_client.post(
                f"/api/collections/{collection}/records", json={"flag": not expected}
            )
            assert created.status_code == 200, created.text
            url = f"/api/collections/{collection}/records/{created.json()['id']}"
            response = await _patch(app_client, url, write)
            assert response.status_code == 200, (write, response.text)
            stored = await _check(app_client, collection, response.json())
            assert stored is expected, write


@pytest.mark.parametrize("mode", ["default", "0.22"])
async def test_bool_modifier_keys_are_ignored_on_create(
    app_client: AsyncClient, bool_env: dict, mode: str
) -> None:
    collection = bool_env["flags"]
    cases: list[tuple[dict[str, Any], bool]] = [
        ({"flag+": "true"}, False),
        ({"flag-": True}, False),
        ({"+flag": "1"}, False),
        ({"flag": "true", "flag+": "x"}, True),
        ({"flag-": "x", "flag": "1"}, True),
        ({"flag": "false", "+flag": "true"}, False),
    ]
    with _mode(app_client, mode):
        for payload, expected in cases:
            stored = await _create_json(app_client, collection, payload)
            assert stored is expected, payload


@pytest.mark.parametrize("mode", ["default", "0.22"])
async def test_bool_modifiers_add_no_error_when_a_relation_target_is_missing(
    app_client: AsyncClient, bool_env: dict, mode: str
) -> None:
    # When a relation id is missing, the other fields of the merged form are
    # validated too (relation_target_errors): a bool modifier must not turn
    # the required ``req`` of a true record (or of the request) into false.
    collection = bool_env["related"]
    url = f"/api/collections/{collection}/records"
    missing = "missingrecord01"
    with _mode(app_client, mode):
        created = await app_client.post(url, json={"req": True})
        assert created.status_code == 200, created.text
        record_id = created.json()["id"]
        for key in ("req+", "req-", "+req"):
            responses = [
                await app_client.patch(
                    f"{url}/{record_id}", json={"rel": missing, key: "x"}
                ),
                await app_client.post(
                    url, json={"rel": missing, "req": "true", key: "false"}
                ),
            ]
            for response in responses:
                assert response.status_code == 400, response.text
                data = response.json()["data"]
                assert set(data) == {"rel"}, (key, data)
                assert data["rel"]["code"] == "validation_missing_rel_records"
        # The required bool is still reported when it is false.
        response = await app_client.post(url, json={"rel": missing, "req+": "true"})
        assert response.status_code == 400, response.text
        assert set(response.json()["data"]) == {"rel", "req"}
        assert response.json()["data"]["req"]["code"] == "validation_required"
        assert await _stored(collection, record_id, "req") is True


@pytest.mark.parametrize("mode", ["default", "0.22"])
async def test_repeated_auth_bool_keys_follow_the_mode(
    app_client: AsyncClient, bool_env: dict, mode: str
) -> None:
    # emailVisibility and verified are no 0.22.34 schema fields: a repeated
    # key keeps its first value (forms/record_upsert.go l.191-196), then
    # cast.ToBool (l.380-385).  The default mode keeps a list, false.
    collection = bool_env["users"]
    admin = bool_env["admin"]
    url = f"/api/collections/{collection}/records"
    expected = mode == "0.22"
    with _mode(app_client, mode):
        password = "securepass123"
        response = await app_client.post(
            url,
            headers=admin,
            files=[
                ("email", (None, f"u{uuid.uuid4().hex[:10]}@example.com")),
                ("password", (None, password)),
                ("passwordConfirm", (None, password)),
                ("emailVisibility", (None, "true")),
                ("emailVisibility", (None, "false")),
                ("verified", (None, "1")),
                ("verified", (None, "0")),
            ],
        )
        assert response.status_code == 200, response.text
        record_id = response.json()["id"]
        for column, name in (
            ("email_visibility", "emailVisibility"),
            ("verified", "verified"),
        ):
            assert response.json()[name] is expected, name
            assert await _stored(collection, record_id, column) is expected, column

        reset = await app_client.patch(
            f"{url}/{record_id}",
            headers=admin,
            json={"emailVisibility": False, "verified": False},
        )
        assert reset.status_code == 200, reset.text
        response = await app_client.patch(
            f"{url}/{record_id}",
            headers=admin,
            files=[
                ("emailVisibility", (None, "T")),
                ("emailVisibility", (None, "")),
                ("verified", (None, "true")),
                ("verified", (None, "false")),
            ],
        )
        assert response.status_code == 200, response.text
        for column, name in (
            ("email_visibility", "emailVisibility"),
            ("verified", "verified"),
        ):
            assert response.json()[name] is expected, name
            assert await _stored(collection, record_id, column) is expected, column
