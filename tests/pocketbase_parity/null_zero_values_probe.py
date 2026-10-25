"""Paired PocketBase 0.22 / PPBase probe for NULL values served by records.

PocketBase reads a SQL NULL as ``nil`` and serves every schema field through
``PrepareValue`` (v0.22.34 ``models/record.go`` l.48-91, l.328-359 and
l.528-531, ``models/schema/schema_field.go`` l.304-390): a view column that a
LEFT JOIN leaves NULL is served as the zero value of its field type.  This
probe creates the same synthetic collections, records and views (one field
of each 0.22 type, joined rows with and without a match) on an official
PocketBase 0.22 binary and on PPBase, reads them back (list, view, fields,
expand from and into a view record, filters, sorts, totals, base and auth
records) and writes ``pocketbase.json``, ``ppbase.json`` and ``diff.json``
in ``--out``.  It backs the expected values of
``tests/integration/test_null_zero_values.py``.

Generated ids and stored file names differ between the two servers, so ids
are replaced by their labels and file names by ``"<file>"``.  Differences
listed in ``KNOWN_DIVERGENCES`` are reported separately.

Only synthetic records are created.  PocketBase runs in a temporary data
directory on a random loopback port and is always stopped; PPBase runs
in-process (ASGI) on ``--database-url``, which must point to an empty,
disposable PostgreSQL database.

Usage (from the repository root)::

    PYTHONPATH=. python tests/pocketbase_parity/null_zero_values_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --out /tmp/null-zero-values-probe
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
FILE_MARK = "<file>"

# Filters and sorts on a NULL column (a view column that a LEFT JOIN leaves
# NULL) and on a json null follow PocketBase since WS-A11 (empty operands
# match NULL, ``!=`` is NULL-safe, a JSON null is NULL for ``~``/``!~`` and
# multi-match, comparisons without any column are folded, NULL sorts first
# in ascending order); see ``docs/null-values.md``.  What remains differs
# for non-NULL values or cannot be told apart in a view:
# - PocketBase sorts a multi-value field by its stored JSON text ('[]' after
#   '["x"]'), PPBase by its array ('{}' first); the NULL row is placed like
#   PocketBase, the order of the non-NULL values is unchanged (open).
_MULTI_VALUE_SORT = "multi-value sort: JSON text vs array order of non-NULL values"
# - PPBase stores the empty date '' as NULL (D-0032): in a view, the empty
#   date of a matched row and the NULL of a missing row are both NULL and
#   tie, SQLite sorts NULL before ''.
_VIEW_EMPTY_DATE_SORT = "view date: empty date and LEFT JOIN NULL tie (D-0032)"
# - A json value compared with a non-null value: PocketBase compares
#   JSON_EXTRACT's SQLite value (a JSON string is text, true is 1, an object
#   its minified text), PPBase the stored jsonb.  On a jsonb column PostgreSQL
#   rejects ``jsonb = 5``, ``jsonb = 'x'``, ``jsonb > 1`` (HTTP 500), and
#   ``?=``/``?!=``/``?!~`` read a JSON array element by element (open; only
#   the NULL rows follow PocketBase: ``!=`` keeps them).
_JSON_VALUE = "json vs non-null value: stored jsonb vs JSON_EXTRACT value (open)"
KNOWN_DIVERGENCES: dict[str, str] = {
    **{
        f"null.view_sorts.{sign}{field},title": _MULTI_VALUE_SORT
        for field in ("sels", "docs", "rels", "relnull")
        for sign in ("", "-")
    },
    "null.view_sorts.day,title": _VIEW_EMPTY_DATE_SORT,
}

# The joined target fields: one of each PocketBase 0.22 type.  ``relnull`` is
# the 0.22 ``maxSelect: null`` relation (multiple, unlimited; D-0019).
TARGET_FIELDS = (
    "txt",
    "mail",
    "link",
    "rich",
    "num",
    "flag",
    "meta",
    "sel",
    "sels",
    "doc",
    "docs",
    "rel",
    "rels",
    "relnull",
    "day",
)
FILE_FIELDS = ("doc", "docs")


def _base(name: str, field_type: str, options: dict[str, Any]) -> dict[str, Any]:
    return {
        "system": False,
        "name": name,
        "type": field_type,
        "required": False,
        "presentable": False,
        "unique": False,
        "options": options,
    }


def text_field(name: str) -> dict[str, Any]:
    return _base(name, "text", {"min": None, "max": None, "pattern": ""})


def relation_field(name: str, collection_id: str, max_select: Any) -> dict[str, Any]:
    return _base(
        name,
        "relation",
        {
            "collectionId": collection_id,
            "cascadeDelete": False,
            "minSelect": None,
            "maxSelect": max_select,
            "displayFields": None,
        },
    )


def target_schema(refs_id: str) -> list[dict[str, Any]]:
    file_options = {
        "mimeTypes": [],
        "thumbs": [],
        "maxSize": 5242880,
        "protected": False,
    }
    return [
        text_field("txt"),
        _base("mail", "email", {"exceptDomains": None, "onlyDomains": None}),
        _base("link", "url", {"exceptDomains": None, "onlyDomains": None}),
        _base("rich", "editor", {"convertUrls": False}),
        _base("num", "number", {"min": None, "max": None, "noDecimal": False}),
        _base("flag", "bool", {}),
        _base("meta", "json", {"maxSize": 2000000}),
        _base("sel", "select", {"maxSelect": 1, "values": ["x", "y"]}),
        _base("sels", "select", {"maxSelect": 2, "values": ["x", "y"]}),
        _base("doc", "file", {**file_options, "maxSelect": 1}),
        _base("docs", "file", {**file_options, "maxSelect": 3}),
        relation_field("rel", refs_id, 1),
        relation_field("rels", refs_id, 5),
        relation_field("relnull", refs_id, None),
        _base("day", "date", {"min": "", "max": ""}),
    ]


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"_text": response.text[:200]}


async def _collection(
    client: httpx.AsyncClient, admin: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    payload = {**PUBLIC, "type": "base", **body}
    if payload["type"] == "view":
        payload.update(createRule=None, updateRule=None, deleteRule=None)
    response = await client.post("/api/collections", headers=admin, json=payload)
    if response.status_code != 200:
        raise RuntimeError(
            f"collection {body.get('name')} failed: {response.text[:400]}"
        )
    return response.json()


async def _create(
    client: httpx.AsyncClient,
    name: str,
    payload: dict[str, Any] | None = None,
    files: list[tuple[str, Any]] | None = None,
) -> dict[str, Any]:
    if files is not None:
        response = await client.post(f"/api/collections/{name}/records", files=files)
    else:
        response = await client.post(
            f"/api/collections/{name}/records", json=payload or {}
        )
    if response.status_code != 200:
        raise RuntimeError(f"create {name} failed: {response.text[:300]}")
    return response.json()


class Labels:
    """Replace generated ids by stable labels and file names by a mark."""

    def __init__(self) -> None:
        self.by_id: dict[str, str] = {}

    def add(self, record_id: str, label: str) -> str:
        self.by_id[record_id] = label
        return record_id

    def value(self, key: str, value: Any) -> Any:
        if key in FILE_FIELDS:
            if isinstance(value, list):
                return [FILE_MARK if item else item for item in value]
            return FILE_MARK if value else value
        if isinstance(value, str):
            return self.by_id.get(value, value)
        if isinstance(value, list):
            return [
                self.by_id.get(item, item) if isinstance(item, str) else item
                for item in value
            ]
        return value

    def record(self, item: dict[str, Any], keys: list[str] | None = None) -> Any:
        keys = keys if keys is not None else sorted(item)
        out: dict[str, Any] = {}
        for key in keys:
            if key in {"collectionId", "collectionName", "created", "updated"}:
                continue
            if key == "expand":
                out[key] = {
                    name: (
                        [self.by_id.get(r.get("id"), r.get("id")) for r in value]
                        if isinstance(value, list)
                        else self.by_id.get(value.get("id"), value.get("id"))
                    )
                    for name, value in sorted(item[key].items())
                }
                continue
            out[key] = self.value(key, item.get(key, "<absent>"))
        return out


async def setup(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, Any]:
    labels = Labels()
    refs = await _collection(
        client, admin, {"name": "zv_refs", "schema": [text_field("name")]}
    )
    targets = await _collection(
        client, admin, {"name": "zv_targets", "schema": target_schema(refs["id"])}
    )
    await _collection(
        client,
        admin,
        {
            "name": "zv_src",
            "schema": [text_field("title"), relation_field("target", targets["id"], 1)],
        },
    )
    ref_a = labels.add((await _create(client, "zv_refs", {"name": "ra"}))["id"], "ra")
    ref_b = labels.add((await _create(client, "zv_refs", {"name": "rb"}))["id"], "rb")
    full_fields: list[tuple[str, Any]] = [
        ("txt", (None, "tx")),
        ("mail", (None, "full@example.invalid")),
        ("link", (None, "https://example.invalid/x")),
        ("rich", (None, "<p>rich</p>")),
        ("num", (None, "4.5")),
        ("flag", (None, "true")),
        ("meta", (None, '{"k":1}')),
        ("sel", (None, "x")),
        ("sels", (None, "x")),
        ("sels", (None, "y")),
        ("doc", ("one.txt", b"one", "text/plain")),
        ("docs", ("two.txt", b"two", "text/plain")),
        ("docs", ("three.txt", b"three", "text/plain")),
        ("rel", (None, ref_a)),
        ("rels", (None, ref_a)),
        ("rels", (None, ref_b)),
        ("relnull", (None, ref_b)),
        ("day", (None, "2024-01-02 03:04:05.000Z")),
    ]
    t_full = labels.add(
        (await _create(client, "zv_targets", files=full_fields))["id"], "t_full"
    )
    t_empty = labels.add((await _create(client, "zv_targets", {}))["id"], "t_empty")
    sources = {}
    for title, target in (("a", t_full), ("b", t_empty), ("c", "")):
        created = await _create(client, "zv_src", {"title": title, "target": target})
        sources[title] = labels.add(created["id"], f"s_{title}")
    columns = ", ".join(f"t.{name}" for name in TARGET_FIELDS)
    view_schema = [
        text_field("title"),
        relation_field("tid", targets["id"], 1),
        *(
            dict(field)
            for field in target_schema(refs["id"])
            if field["name"] in TARGET_FIELDS
        ),
    ]
    view = await _collection(
        client,
        admin,
        {
            "name": "zv_view",
            "type": "view",
            "schema": view_schema,
            "options": {
                "query": (
                    f"SELECT s.id, s.title, t.id AS tid, {columns} FROM zv_src s "
                    "LEFT JOIN zv_targets t ON t.id = s.target"
                )
            },
        },
    )
    view_ref = await _collection(
        client,
        admin,
        {
            "name": "zv_view_ref",
            "type": "view",
            "schema": [text_field("title"), relation_field("vref", view["id"], 1)],
            "options": {
                "query": (
                    "SELECT s.id, s.title, v.id AS vref FROM zv_src s "
                    "LEFT JOIN zv_view v ON v.id = s.id"
                )
            },
        },
    )
    people = await _collection(
        client,
        admin,
        {
            "name": "zv_people",
            "type": "auth",
            "schema": [
                text_field("nick"),
                _base(
                    "score", "number", {"min": None, "max": None, "noDecimal": False}
                ),
                _base("tags", "select", {"maxSelect": 2, "values": ["x", "y"]}),
            ],
            "options": AUTH_OPTIONS,
        },
    )
    return {
        "labels": labels,
        "sources": sources,
        "schema": {
            coll["name"]: {
                field["name"]: [
                    field["type"],
                    (field.get("options") or {}).get("maxSelect", "<absent>"),
                ]
                for field in coll["schema"]
            }
            for coll in (view, view_ref)
        },
        "people": people["name"],
        "people_id": people["id"],
    }


async def _list(client: httpx.AsyncClient, name: str, **params: Any) -> dict[str, Any]:
    params.setdefault("perPage", 200)
    response = await client.get(f"/api/collections/{name}/records", params=params)
    body = _json(response)
    if response.status_code != 200:
        return {"status": response.status_code, "body": body}
    return body


async def read_scenarios(client: httpx.AsyncClient, env: dict[str, Any]) -> Any:
    labels: Labels = env["labels"]
    out: dict[str, Any] = {"schema": env["schema"]}
    body = await _list(client, "zv_view", sort="title")
    out["view_keys"] = sorted(body["items"][0])
    out["view_list"] = {item["title"]: labels.record(item) for item in body["items"]}
    out["view_one"] = {}
    for title, record_id in env["sources"].items():
        response = await client.get(f"/api/collections/zv_view/records/{record_id}")
        out["view_one"][title] = labels.record(_json(response))
    body = await _list(client, "zv_view", sort="title", fields="title,num,rels,meta")
    out["view_fields"] = [labels.record(item) for item in body["items"]]
    body = await _list(client, "zv_view", sort="title", expand="tid,rel,rels,relnull")
    out["view_expand"] = {
        item["title"]: labels.record(item, ["expand"]) if "expand" in item else {}
        for item in body["items"]
    }
    body = await _list(client, "zv_view_ref", sort="title", expand="vref")
    out["into_view_expand"] = {
        item["title"]: (
            labels.record(item["expand"]["vref"]) if "expand" in item else None
        )
        for item in body["items"]
    }
    out["filters"] = {}
    for key, expression in (
        ("txt_eq_empty", 'txt = ""'),
        ("txt_neq_empty", 'txt != ""'),
        ("num_eq_0", "num = 0"),
        ("flag_eq_false", "flag = false"),
        ("rel_eq_empty", 'rel = ""'),
        ("tid_eq_empty", 'tid = ""'),
        ("meta_eq_null", "meta = null"),
    ):
        body = await _list(client, "zv_view", sort="title", filter=expression)
        out["filters"][key] = (
            [body["totalItems"], [item["title"] for item in body["items"]]]
            if "items" in body
            else body
        )
    out["sort"] = {}
    for key, sort in (("txt_asc", "txt,title"), ("num_desc", "-num,title")):
        body = await _list(client, "zv_view", sort=sort)
        out["sort"][key] = [item["title"] for item in body["items"]]
    body = await _list(client, "zv_view", perPage=2, sort="title")
    out["totals"] = [body["totalItems"], body["totalPages"], len(body["items"])]
    body = await _list(client, "zv_targets", sort="txt")
    out["base_list"] = {
        labels.by_id.get(item["id"], item["id"]): labels.record(item)
        for item in body["items"]
    }
    out["base_filters"] = {}
    for key, expression in (
        ("txt_eq_empty", 'txt = ""'),
        ("meta_eq_null", "meta = null"),
        ("meta_neq_null", "meta != null"),
    ):
        body = await _list(client, "zv_targets", sort="txt", filter=expression)
        out["base_filters"][key] = (
            [labels.by_id.get(item["id"], item["id"]) for item in body["items"]]
            if "items" in body
            else body
        )
    return out


# Filters and sorts on the NULL columns of an unmatched LEFT JOIN row
# (rows a: full target, b: empty target, c: no target).  For each joined
# field: the empty operands ``""``/``null`` and a non-empty value that no row
# holds, with every equality operator, both operand orders and both sort
# directions.
NULL_VALUES: dict[str, str | None] = {
    "txt": '"zz"',
    "mail": '"zz@example.invalid"',
    "link": '"https://zz.invalid"',
    "rich": '"zz"',
    "num": "7",
    "flag": "true",
    # A json field compared with a non-null value: ``null.json_values`` and
    # ``null.jview_filters``.
    "meta": None,
    "sel": '"y"',
    "sels": '"y"',
    "doc": '"zz.txt"',
    "docs": '"zz.txt"',
    "rel": '"zzzzzzzzzzzzzzz"',
    "rels": '"zzzzzzzzzzzzzzz"',
    "relnull": '"zzzzzzzzzzzzzzz"',
    "day": '"2030-01-01 00:00:00.000Z"',
    "tid": '"zzzzzzzzzzzzzzz"',
}
NULL_OTHER_FILTERS = (
    'txt !~ "zz"',
    'txt ~ ""',
    'txt > ""',
    'txt < "zz"',
    "num > 1",
    "num < 9",
    "num <= 0",
    'sels !~ "zz"',
    'rel !~ "zz"',
    "txt = sel",
    "txt != title",
    "rel = tid",
    "sel != txt",
    "txt ?= sel",
    "meta.k = null",
    "meta.k != null",
)


def null_filter_expressions(field: str, value: str | None) -> list[str]:
    expressions = [
        f"{field} {op} {rhs}"
        for rhs in ('""', "null")
        for op in ("=", "!=", "?=", "?!=")
    ]
    expressions += [f'"" = {field}', f"null != {field}"]
    if value is not None:
        expressions += [
            f"{field} = {value}",
            f"{field} != {value}",
            f"{field} ?!= {value}",
            f"{value} != {field}",
        ]
    return expressions


async def _titles(
    client: httpx.AsyncClient,
    name: str,
    headers: dict[str, str] | None = None,
    **params: Any,
) -> Any:
    params.setdefault("perPage", 200)
    response = await client.get(
        f"/api/collections/{name}/records", params=params, headers=headers
    )
    body = _json(response)
    if response.status_code != 200:
        body = {"status": response.status_code, "body": body}
    if "items" not in body:
        return body
    return [body["totalItems"], [item.get("title") for item in body["items"]]]


async def null_scenarios(
    client: httpx.AsyncClient, admin: dict[str, str], env: dict[str, Any]
) -> Any:
    labels: Labels = env["labels"]
    out: dict[str, Any] = {"view_filters": {}, "view_sorts": {}}
    for field, value in NULL_VALUES.items():
        for expression in null_filter_expressions(field, value):
            out["view_filters"][expression] = await _titles(
                client, "zv_view", sort="title", filter=expression
            )
        if field == "tid":
            # Two generated ids: their order is arbitrary.
            continue
        for sort in (f"{field},title", f"-{field},title"):
            out["view_sorts"][sort] = await _titles(client, "zv_view", sort=sort)
    for expression in NULL_OTHER_FILTERS:
        out["view_filters"][expression] = await _titles(
            client, "zv_view", sort="title", filter=expression
        )

    # Relation paths into the view (zv_view_ref.vref -> zv_view) and
    # ``@collection`` reads of the view (admin only), whose row c holds the
    # NULLs.
    out["path_filters"] = {}
    for expression in (
        'vref.txt = ""',
        'vref.txt != "zz"',
        "vref.num != 7",
        'vref.rel = ""',
        'vref.sel ?= ""',
        'vref.sels ?= ""',
        "vref.flag != true",
        "vref.meta = null",
        "vref.meta != null",
        "vref.meta.k = null",
        'vref.tid = ""',
    ):
        out["path_filters"][expression] = await _titles(
            client, "zv_view_ref", sort="title", filter=expression
        )
    for expression in (
        # ``?`` operators: one joined view row must match both conditions
        # (without ``?`` every row of the collection must match).
        '@collection.zv_view.id ?= id && @collection.zv_view.txt ?= ""',
        '@collection.zv_view.id ?= id && @collection.zv_view.txt ?!= "zz"',
        "@collection.zv_view.id ?= id && @collection.zv_view.num ?!= 7",
        "@collection.zv_view.id ?= id && @collection.zv_view.meta ?= null",
        "@collection.zv_view.id ?= id && @collection.zv_view.meta ?!= null",
        '@collection.zv_view.id ?= id && @collection.zv_view.rels ?= ""',
        '@collection.zv_view.id ?= id && @collection.zv_view.sel = ""',
    ):
        out["path_filters"][expression] = await _titles(
            client, "zv_src", admin, sort="title", filter=expression
        )

    # Auth records through a LEFT JOIN: users with an email (rows a and b;
    # PPBase requires an email even with ``requireEmail: false``, which is
    # outside this probe), no user (row c).
    people = env["people"]
    user_ids = {}
    for nick in ("a", "b"):
        user_ids[nick] = await _create(
            client,
            people,
            {
                "username": f"zvnull{nick}",
                "email": f"zvnull{nick}@example.invalid",
                "nick": nick,
                "score": 3,
                "password": USER_PASSWORD,
                "passwordConfirm": USER_PASSWORD,
            },
        )
    await _collection(
        client,
        admin,
        {
            "name": "zv_uview",
            "type": "view",
            "schema": [
                text_field("title"),
                relation_field("uid", env["people_id"], 1),
                _base("uemail", "email", {"exceptDomains": None, "onlyDomains": None}),
                _base("uver", "bool", {}),
                _base(
                    "uscore", "number", {"min": None, "max": None, "noDecimal": False}
                ),
                _base("utags", "select", {"maxSelect": 2, "values": ["x", "y"]}),
            ],
            "options": {
                "query": (
                    "SELECT s.id, s.title, u.id AS uid, u.email AS uemail, "
                    "u.verified AS uver, u.score AS uscore, "
                    f"u.tags AS utags FROM zv_src s LEFT JOIN {people} u "
                    "ON u.nick = s.title"
                )
            },
        },
    )
    out["auth_view_filters"] = {}
    for expression in (
        'uemail = ""',
        'uemail != ""',
        'uemail != "zz@example.invalid"',
        "uver = false",
        "uver != true",
        'uid = ""',
        "uscore != 1",
        'utags = ""',
        'utags ?= ""',
    ):
        out["auth_view_filters"][expression] = await _titles(
            client, "zv_uview", sort="title", filter=expression
        )
    out["auth_view_sorts"] = {}
    for sort in ("uemail,title", "-uemail,title", "uver,title", "-uscore,title"):
        out["auth_view_sorts"][sort] = await _titles(client, "zv_uview", sort=sort)

    # A base record whose json holds a null key (stored value, not SQL NULL).
    labels.add(
        (await _create(client, "zv_targets", {"txt": "jn", "meta": {"k": None}}))["id"],
        "t_jnull",
    )
    out["base_filters"] = {}
    for expression in (
        "meta.k = null",
        "meta.k != null",
        "meta = null",
        "meta != null",
        'meta = ""',
        'meta != ""',
        # A stored '' is an empty operand too: rule-visible since WS-A11.
        "txt = null",
        "txt != null",
        "null = txt",
    ):
        body = await _list(client, "zv_targets", sort="txt", filter=expression)
        out["base_filters"][expression] = (
            [labels.by_id.get(item["id"], item["id"]) for item in body["items"]]
            if "items" in body
            else body
        )
    out.update(await review_scenarios(client, admin, env, user_ids))
    return out


# Comparisons without any column: PocketBase renders '' = v / v = w, compared
# by SQLite without affinity (filter.go l.226-281 and l.318-338).
CONSTANT_FILTERS = (
    "1 = null",
    "1 != null",
    "0 != null",
    "null = 0",
    "true = null",
    "true != null",
    "false != null",
    '1 = ""',
    '1 != ""',
    '"" = 0',
    'true = ""',
    'true != ""',
    "null = null",
    '"" != null',
    '"x" = null',
    '"x" != ""',
    "1 = 1",
    '1 = "1"',
    "true = 1",
    '"a" = "b"',
    "1 ?= null",
    "1 ?!= null",
    "null ?= null",
    '"a" ?= "a"',
    'title = "a" || 1 = null',
    'title = "a" && 1 != null',
)
# A base json field holding a SQL NULL in PocketBase (no value) and a JSON
# null in PPBase (``n``), JSON nulls in objects and other JSON values.
JSON_ROWS: tuple[tuple[str, Any], ...] = (
    ("n", None),
    ("kn", {"k": None}),
    ("knu", {"k": "nu"}),
    ("k1", {"k": 1}),
    ("sx", "x"),
    ("es", ""),
    ("num5", 5),
    ("tr", True),
    ("arr", ["nu", "x"]),
    ("o", {"a": 1}),
)
# ``~``/``!~`` read a JSON null as NULL: it never matches (JSON_EXTRACT).
JSON_LIKE_FILTERS = (
    'meta ~ "nu"',
    'meta ~ "x"',
    'meta !~ "x"',
    'meta !~ "zz"',
    'meta.k ~ "nu"',
    'meta.k !~ "x"',
    'meta.k !~ "zz"',
    'meta ?~ "nu"',
    'meta ?!~ "zz"',
)
# Comparisons of a json value with a non-null value: PocketBase compares
# JSON_EXTRACT's SQLite value, PPBase the stored jsonb (open, see
# ``KNOWN_DIVERGENCES``).
JSON_VALUE_FILTERS = (
    "meta = 5",
    "meta != 5",
    'meta = "x"',
    'meta != "x"',
    "meta = true",
    "meta != true",
    'meta ?= "x"',
    'meta ?!= "x"',
    "meta.k = 1",
    "meta.k != 1",
    'meta.k = "nu"',
    "meta > 1",
    'meta ?!~ "x"',
)
KNOWN_DIVERGENCES.update(
    {f"null.json_values.{expression}": _JSON_VALUE for expression in JSON_VALUE_FILTERS}
)
# A multiple relation to the json field (multi-match subquery).
MULTI_ROWS: tuple[tuple[str, tuple[str, ...], Any], ...] = (
    ("ma", ("kn", "knu"), {"k": 1}),
    ("mb", ("n",), None),
    ("mc", (), None),
    ("md", ("sx",), "x"),
    ("me", ("knu",), {"k": "nu"}),
    ("mf", ("n", "knu"), None),
)
MULTI_FILTERS = (
    'ts.meta != ""',
    'ts.meta = ""',
    "ts.meta = null",
    "ts.meta != null",
    'ts.meta ~ "nu"',
    'ts.meta !~ "zz"',
    "ts.meta.k = null",
    "ts.meta.k != null",
    'ts.meta.k ~ "nu"',
    'ts.meta.k !~ "zz"',
    'ts.meta ?!= ""',
    'ts.meta ?~ "nu"',
    "ts.meta = meta",
    "ts.meta != meta",
    "ts.meta ?= meta",
    '@collection.zv_json.meta != ""',
    # Only the JSON null of t_empty fails (t_full and t_jnull are objects).
    '@collection.zv_targets.meta != ""',
    '@collection.zv_json.meta !~ "zz"',
    "@collection.zv_json.meta ?= null",
    '@collection.zv_json.title ?= "kn" && @collection.zv_json.meta.k ?= null',
)
# json columns computed by a view (PocketBase 0.22 infers ``json`` for an
# expression), NULL for the unmatched LEFT JOIN row c: the BUMA case.
JVIEW_FILTERS = (
    'jtxt = ""',
    'jtxt != ""',
    "jtxt = null",
    "jtxt != null",
    'jtxt = "tx"',
    'jtxt != "tx"',
    '"tx" != jtxt',
    'jtxt ?= "tx"',
    'jtxt ?!= "tx"',
    'jtxt ~ "t"',
    'jtxt !~ "zz"',
    "jnum = 0",
    "jnum != 0",
    "jnum != 4.5",
    "jnum > 1",
    "jnum = null",
    "jtxt = txt",
    "jtxt != txt",
)
JVIEW_SORTS = ("jtxt,title", "-jtxt,title", "jnum,title", "-jnum,title")
# List rules seen by an anonymous caller (``@request.auth.id`` is NULL) and
# by the owner of record ``oa``.
RULES = (
    "@request.auth.id ?= owner",
    "@request.auth.id = owner",
    "owner ?= @request.auth.id",
    "@request.auth.id ?!= owner",
    "@request.auth.id != owner",
    '@request.auth.id ?= ""',
    '@request.auth.id ?!= ""',
    "@request.auth.id ?= null",
)
# Anonymous only: generic PPBase resolves ``@request.auth.*`` from the token
# claims, not from the auth record (``verified`` is absent for the owner;
# BUMA loads the record fields), which is outside this probe.
ANONYMOUS_RULES = ("@request.auth.verified ?= false",)


async def review_scenarios(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    env: dict[str, Any],
    user_ids: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    out: dict[str, Any] = {"constants": {}}
    for expression in CONSTANT_FILTERS:
        out["constants"][expression] = await _titles(
            client, "zv_src", sort="title", filter=expression
        )

    json_coll = await _collection(
        client,
        admin,
        {
            "name": "zv_json",
            "schema": [
                text_field("title"),
                _base("meta", "json", {"maxSize": 2000000}),
            ],
        },
    )
    json_ids = {}
    for title, meta in JSON_ROWS:
        payload: dict[str, Any] = {"title": title}
        if title != "n":
            payload["meta"] = meta
        json_ids[title] = (await _create(client, "zv_json", payload))["id"]
    for key, expressions in (
        ("json_like", JSON_LIKE_FILTERS),
        ("json_values", JSON_VALUE_FILTERS),
    ):
        out[key] = {}
        for expression in expressions:
            out[key][expression] = await _titles(
                client, "zv_json", sort="title", filter=expression
            )

    await _collection(
        client,
        admin,
        {
            "name": "zv_multi",
            "schema": [
                text_field("title"),
                _base("meta", "json", {"maxSize": 2000000}),
                relation_field("ts", json_coll["id"], 5),
            ],
        },
    )
    for title, targets, meta in MULTI_ROWS:
        payload = {"title": title, "ts": [json_ids[t] for t in targets]}
        if meta is not None:
            payload["meta"] = meta
        await _create(client, "zv_multi", payload)
    out["multi_json"] = {}
    for expression in MULTI_FILTERS:
        out["multi_json"][expression] = await _titles(
            client, "zv_multi", admin, sort="title", filter=expression
        )

    await _collection(
        client,
        admin,
        {
            "name": "zv_jview",
            "type": "view",
            # The schema PocketBase 0.22.34 infers: json for an expression.
            "schema": [
                text_field("title"),
                _base("jtxt", "json", {"maxSize": 2000000}),
                _base("jnum", "json", {"maxSize": 2000000}),
                text_field("txt"),
            ],
            "options": {
                "query": (
                    "SELECT s.id, s.title, (t.txt || '') AS jtxt, "
                    "(t.num + 0) AS jnum, t.txt FROM zv_src s "
                    "LEFT JOIN zv_targets t ON t.id = s.target"
                )
            },
        },
    )
    out["jview_filters"] = {}
    for expression in JVIEW_FILTERS:
        out["jview_filters"][expression] = await _titles(
            client, "zv_jview", sort="title", filter=expression
        )
    out["jview_sorts"] = {}
    for sort in JVIEW_SORTS:
        out["jview_sorts"][sort] = await _titles(client, "zv_jview", sort=sort)

    owned = await _collection(
        client,
        admin,
        {
            "name": "zv_owned",
            "schema": [
                text_field("title"),
                relation_field("owner", env["people_id"], 1),
            ],
        },
    )
    await _create(client, "zv_owned", {"title": "oa", "owner": user_ids["a"]["id"]})
    await _create(client, "zv_owned", {"title": "ob", "owner": ""})
    response = await client.post(
        f"/api/collections/{env['people']}/auth-with-password",
        json={"identity": "zvnulla@example.invalid", "password": USER_PASSWORD},
    )
    user = {"Authorization": _json(response).get("token", "")}
    out["rules_anonymous"] = {}
    out["rules_owner"] = {}
    for rule in (*RULES, *ANONYMOUS_RULES):
        response = await client.patch(
            f"/api/collections/{owned['id']}", headers=admin, json={"listRule": rule}
        )
        if response.status_code != 200:
            out["rules_anonymous"][rule] = {"status": response.status_code}
            continue
        out["rules_anonymous"][rule] = await _titles(client, "zv_owned", sort="title")
        if rule in RULES:
            out["rules_owner"][rule] = await _titles(
                client, "zv_owned", user, sort="title"
            )
    return out


async def auth_scenarios(client: httpx.AsyncClient, env: dict[str, Any]) -> Any:
    name = env["people"]
    email = "zvperson@example.invalid"
    created = await _create(
        client,
        name,
        {
            "username": "zvperson",
            "email": email,
            "password": USER_PASSWORD,
            "passwordConfirm": USER_PASSWORD,
        },
    )
    keys = ["nick", "score", "tags", "emailVisibility", "verified"]
    out: dict[str, Any] = {"create": {k: created.get(k, "<absent>") for k in keys}}
    response = await client.post(
        f"/api/collections/{name}/auth-with-password",
        json={"identity": email, "password": USER_PASSWORD},
    )
    record = _json(response).get("record") or {}
    out["auth_with_password"] = {k: record.get(k, "<absent>") for k in keys}
    return out


async def run_scenarios(client: httpx.AsyncClient, admin: dict[str, str]) -> Any:
    env = await setup(client, admin)
    return {
        "read": await read_scenarios(client, env),
        "null": await null_scenarios(client, admin, env),
        "auth": await auth_scenarios(client, env),
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def pocketbase_client(binary: Path) -> AsyncIterator[httpx.AsyncClient]:
    with tempfile.TemporaryDirectory(prefix="ppbase-pb-null-probe-") as root:
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

    secret = "probe-secret-key-for-null-zero-values-000000001"
    with tempfile.TemporaryDirectory(prefix="ppbase-null-probe-data-") as data_dir:
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
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    pocketbase = asyncio.run(run_pocketbase(args.pocketbase))
    (args.out / "pocketbase.json").write_text(
        json.dumps(pocketbase, indent=1, sort_keys=True) + "\n"
    )
    ppbase = asyncio.run(run_ppbase(args.database_url))
    differences = _diff(pocketbase, ppbase)
    for row in differences:
        row["known"] = KNOWN_DIVERGENCES.get(row["path"])
    for name, payload in (("ppbase.json", ppbase), ("diff.json", differences)):
        (args.out / name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n"
        )
    unexpected = [row for row in differences if not row["known"]]
    print(
        json.dumps(
            {
                "differences": len(differences),
                "known": len(differences) - len(unexpected),
                "unexpected": len(unexpected),
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
