"""Paired PocketBase 0.22 / PPBase probe for file downloads.

PocketBase serves a record file with ``apis/file.go`` ``download`` and
``tools/filesystem`` ``Serve`` (v0.22.34 ``filesystem.go`` l.367-526):
content type from the ``fileblob`` ``.attrs`` side-car, ``Content-
Disposition``, security and cache headers, thumbnails.  This probe uploads
synthetic files to an official PocketBase 0.22 binary, lets it generate
some thumbnails, copies its ``storage`` directory (side-cars and thumbnails
included, modification times preserved) under a PPBase data directory,
recreates the same collections and records in PPBase, then sends the same
requests to both and writes ``pocketbase.json``, ``ppbase.json`` and
``diff.json`` in ``--out``.

Thumbnails present in the copied storage are compared byte for byte.
Thumbnails that each server generates itself are compared on format,
dimensions and sampled colors (Go's ``imaging`` and Pillow do not encode the
same bytes); their ``Content-Length`` and ``Last-Modified`` are not
compared.  Error responses, and responses whose status differs, are
compared on their status only: the JSON envelope belongs to the error
compatibility mode.  Some side-cars are replaced by crafted ones (in both
storages) to compare the JSON decoding of ``fileblob``.  ``--compat 0.22``
(the default) runs PPBase in the PocketBase 0.22 compatibility mode,
``--compat default`` in its default (PocketBase v0.40.6) mode.  Every
difference that :func:`known_divergence` recognizes is documented in
``docs/file-serving.md``.

Only synthetic files are used.  PocketBase runs in a temporary directory on
a random loopback port and is always stopped; PPBase runs in-process (ASGI)
on ``--database-url``, which must point to an empty, disposable PostgreSQL
database.

Usage (from the repository root)::

    PYTHONPATH=. python tests/pocketbase_parity/files_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --out /tmp/files-probe [--compat default]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

ADMIN_EMAIL = "probe-admin@example.invalid"
ADMIN_PASSWORD = "probe-Password-123"
FILES_ID = "probefiles00001"
PLAIN_ID = "probeplain00001"
PROTECTED_PUBLIC_ID = "probeprotpub001"
PROTECTED_ADMIN_ID = "probeprotadm001"
THUMBS = ["50x50", "20x0", "0x20", "30x20t", "30x20b", "30x20f", "40x40f"]
COMPARED_HEADERS = (
    "content-type",
    "content-disposition",
    "content-length",
    "content-security-policy",
    "cache-control",
    "x-content-type-options",
    "x-xss-protection",
    "x-frame-options",
    "accept-ranges",
    "last-modified",
    "content-range",
    "cross-origin-opener-policy",
)
RED = (255, 0, 0)
BLUE = (0, 0, 255)

# Side-cars replaced in both storages after the upload (``fileblob``
# decoding: gocloud v0.40.0 json v1 in v0.22.34, json/v2 in v0.40.6).
CRAFTED_ATTRS = {
    "c_trailing": b'{"user.content_type":"image/png"} trailing',
    "c_dup": b'{"user.content_type":"image/png","user.content_type":"image/gif"}',
    "c_case": b'{"USER.CONTENT_TYPE":"image/gif"}',
    "c_longs": '{"u\u017fer.content_type":"image/gif"}'.encode(),
    "c_dotless": (
        '{"user.content_d\u0131sposition":1,"user.content_type":"image/gif"}'.encode()
    ),
    "c_surrogate": b'{"user.content_type":"image/png\\ud800"}',
    "c_badutf8": b'{"user.content_type":"image/png\xff"}',
    "c_nullmeta": b'{"user.content_type":"image/png","user.metadata":{"a":null}}',
    "c_md5nl": (
        b'{"user.content_type":"image/png","md5":"kAFQmDzST7DWlj99\\nKOF/cg=="}'
    ),
    "c_md5arr": b'{"user.content_type":"image/png","md5":[1,2,255]}',
    # A number that is no byte, beyond Python's 4300-digit int() limit.
    "c_md5huge": b'{"user.content_type":"image/png","md5":[1,' + b"9" * 5000 + b"]}",
    "c_md5bad": b'{"user.content_type":"image/png","md5":"%%%"}',
    "c_nan": b'{"x":NaN,"user.content_type":"image/png"}',
    "c_null": b"null",
    "c_ctnull": b'{"user.content_type":"a/b","user.content_type":null}',
}

# fileblob serves a blob without side-car as application/octet-stream (an
# attachment, never a thumbnail); PPBase keeps its extension-based type for
# such files, which only its own uploads lack.  Both modes.
NOATTRS_DIVERGENCES = {
    "A.noattrs.plain.headers.content-disposition": "no .attrs: extension type",
    "A.noattrs.plain.headers.content-type": "no .attrs: extension type",
    "A.noattrs.thumb_100x100.headers.content-disposition": "no .attrs: thumbnail",
    "A.noattrs.thumb_100x100.headers.content-type": "no .attrs: thumbnail",
    "A.noattrs.thumb_100x100.image": "no .attrs: thumbnail",
}

# Forms of a valid protected-file token.  v0.22.34 trims it with
# strings.TrimSpace (apis/file.go l.221-225), v0.40.6 does not (l.115-116);
# golang-jwt decodes each segment with base64.RawURLEncoding, which skips
# CR/LF and refuses any other character (padding included).
TOKEN_FORMS = {
    "token_valid": lambda token: token,
    "token_spaces": lambda token: f"  {token}  ",
    "token_go_spaces": lambda token: f"\u3000\xa0\x85\t{token}\u2029\u205f\v\u2000\n",
    # Python's str.strip() would remove U+001C; Go's TrimSpace keeps it.
    "token_not_go_spaces": lambda token: f"{token}\x1c\x1c",
    "token_zero_width": lambda token: f"{token}\u200b",
    "token_lf_in_signature": lambda token: f"{token[:-10]}\n{token[-10:]}",
    "token_padding": lambda token: f"{token}=",
    "token_blank": lambda token: " \t\u3000 ",
}

# Protected files: both versions evaluate the viewRule as a guest without a
# valid token (v0.22.34 apis/file.go l.118-138, v0.40.6 l.109-134) and
# v0.22.34 refuses with a 403 (l.136-138); PPBase refuses before reading the
# rule, with a 404 (v0.40.6 l.132-133).  Both modes, not implemented.
# path -> (reason, PocketBase value, PPBase value).
PROTECTED_DIVERGENCES = {
    **{
        f"C.png.{name}.status": ("protected: guest viewRule not evaluated", 200, 404)
        for name in ("protected_no_token", "protected_bad_token", "token_blank")
    },
    **{
        f"D.png.{name}.status": ("protected: 0.22.34 refuses with 403", 403, 404)
        for name in (
            "protected_no_token",
            "protected_bad_token",
            "token_blank",
            "token_not_go_spaces",
            "token_zero_width",
            "token_padding",
        )
    },
}

# v0.40.6 verifies the token untrimmed (the default mode).
DEFAULT_MODE_TOKEN_DIVERGENCES = {
    f"D.png.{name}.status": ("v0.40.6: file token not trimmed", 200, 404)
    for name in ("token_spaces", "token_go_spaces")
}

# The PocketBase v0.40.6 behavior PPBase applies outside the 0.22 mode.
DEFAULT_MODE_DIVERGENCES = {
    "A.webp.thumb_100x100.headers.content-disposition": "v0.40.6: webp thumbnail",
    "A.webp.thumb_100x100.headers.content-type": "v0.40.6: webp thumbnail",
    "A.webp.thumb_100x100.image": "v0.40.6: webp thumbnail",
    "A.fakejpg.thumb_100x100.image": "v0.40.6: format from the original type",
    "A.js.plain.headers.content-type": "v0.40.6: .js override",
    "A.c_case.plain.headers.content-disposition": "json/v2: case-sensitive names",
    "A.c_case.plain.headers.content-type": "json/v2: case-sensitive names",
    "A.c_longs.plain.headers.content-disposition": "json/v2: case-sensitive names",
    "A.c_longs.plain.headers.content-type": "json/v2: case-sensitive names",
    "sidecar.thumb_20x0": "v0.40.6: json.MarshalWrite side-car form",
}
for _key in (
    "c_trailing",
    "c_dup",
    "c_surrogate",
    "c_badutf8",
    "c_md5nl",
    "c_md5arr",
    "c_ctnull",
):
    DEFAULT_MODE_DIVERGENCES[f"A.{_key}.plain.status"] = "json/v2: refused side-car"


def _go_quoted(name: str) -> str:
    """``strconv.Quote`` of the probe's ASCII names."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _expected(entry: tuple[str, Any, Any], row: dict[str, Any]) -> str | None:
    """The reason of ``entry`` when both sides answered the documented values."""
    reason, pocketbase, ppbase = entry
    if (row["pocketbase"], row["ppbase"]) == (pocketbase, ppbase):
        return reason
    return None


def known_divergence(row: dict[str, Any], compat: str) -> str | None:
    """The documented reason of one difference, ``None`` if unexpected."""
    path = row["path"]
    reason = NOATTRS_DIVERGENCES.get(path)
    if reason:
        return reason
    if path in PROTECTED_DIVERGENCES:
        return _expected(PROTECTED_DIVERGENCES[path], row)
    if compat == "0.22":
        return None
    if path in DEFAULT_MODE_TOKEN_DIVERGENCES:
        return _expected(DEFAULT_MODE_TOKEN_DIVERGENCES[path], row)
    reason = DEFAULT_MODE_DIVERGENCES.get(path)
    if reason:
        return reason
    if (
        path.endswith(".headers.cross-origin-opener-policy")
        and row["pocketbase"] is None
        and row["ppbase"] == "same-origin"
    ):
        return "v0.40.6: Cross-Origin-Opener-Policy"
    if path.endswith(".headers.content-disposition"):
        disposition, _sep, name = str(row["pocketbase"]).partition("; filename=")
        if row["ppbase"] == f"{disposition}; filename={_go_quoted(name)}":
            return "v0.40.6: quoted file name"
    return None


def _image(size: tuple[int, int], fmt: str, **save: Any) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (0, 160, 60)).save(output, format=fmt, **save)
    return output.getvalue()


def _halves(size: tuple[int, int], *, vertical: bool, fmt: str, **save: Any) -> bytes:
    """Red then blue halves (top/bottom when ``vertical``, else left/right)."""
    width, height = size
    image = Image.new("RGB", size, BLUE)
    box = (0, 0, width, height // 2) if vertical else (0, 0, width // 2, height)
    image.paste(RED, box)
    output = BytesIO()
    image.save(output, format=fmt, **save)
    return output.getvalue()


def synthetic_files() -> dict[str, tuple[str, bytes]]:
    """``key -> (upload name, bytes)``, all generated here."""
    exif = Image.Exif()
    exif[0x0112] = 6  # displayed rotated 90 degrees clockwise
    frames = [Image.new("RGB", (40, 40), color) for color in (RED, BLUE)]
    gif = BytesIO()
    frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:])
    png = _image((120, 80), "PNG")
    return {
        "png": ("photo.png", png),
        "small": ("small.png", _image((50, 30), "PNG")),
        "tall": ("tall.png", _halves((100, 200), vertical=True, fmt="PNG")),
        "jpg": (
            "camera.jpg",
            _halves((160, 90), vertical=False, fmt="JPEG", exif=exif, quality=95),
        ),
        "gif": ("anim.gif", gif.getvalue()),
        "webp": ("pic.webp", _image((60, 40), "WEBP")),
        "bin": ("scan.bin", png),
        "svg": ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
        "css": ("theme.css", b"body { color: red; }\n"),
        "pdf": ("manual.pdf", b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"),
        "txt": ("notes.txt", b"plain synthetic text\n"),
        "html": ("page.html", b"<!doctype html><html><body>x</body></html>\n"),
        "noattrs": ("noattrs.png", _image((70, 50), "PNG")),
        "missing": ("gone.png", _image((30, 30), "PNG")),
        # PocketBase keeps "=" in a stored extension (file.go l.169).
        "eq": ("report.v=1", b"synthetic text, = in the extension\n"),
        # PNG bytes under a .jpg name (recorded as image/png).
        "fakejpg": ("fake.jpg", png),
        "js": ("app.js", b"var probe = 1;\n"),
        **{key: (f"{key}.png", _image((20, 10), "PNG")) for key in CRAFTED_ATTRS},
    }


# Thumbnails generated by PocketBase before its storage is copied.
PREGENERATED = [
    ("A", "png", "100x100"),
    ("A", "png", "50x50"),
    ("A", "jpg", "100x100"),
    ("A", "small", "30x20t"),
    ("B", "png", "100x100"),
]


def scenarios() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(record: str, key: str, name: str, **kwargs: Any) -> None:
        rows.append({"record": record, "file": key, "name": name, **kwargs})

    for key in synthetic_files():
        if key != "missing":
            add("A", key, "plain")
    for value in ("1", "0", "true", "T", "yes", "", "custom.pdf"):
        add("A", "png", f"download_{value or 'empty'}", params=[("download", value)])
        add("A", "pdf", f"download_{value or 'empty'}", params=[("download", value)])
    add("A", "png", "download_repeated", params=[("download", "1"), ("download", "0")])
    add("A", "png", "head", method="HEAD")
    add("A", "png", "range_head", headers={"Range": "bytes=0-9"})
    add("A", "png", "range_suffix", headers={"Range": "bytes=-5"})
    add(
        "A",
        "png",
        "range_unsatisfiable",
        headers={"Range": "bytes=99999-"},
        kind="range_error",
    )
    for key, sizes in (
        (
            "png",
            [
                "100x100",
                "50x50",
                "20x0",
                "0x20",
                "30x20t",
                "30x20b",
                "30x20f",
                "40x40f",
            ],
        ),
        ("small", ["30x20t", "100x100", "30x20f"]),
        ("tall", ["30x20t", "30x20b", "100x100"]),
        ("jpg", ["100x100", "0x20"]),
        ("gif", ["100x100", "20x0"]),
        ("webp", ["100x100"]),
        ("bin", ["100x100"]),
        ("txt", ["100x100"]),
        ("svg", ["100x100"]),
        ("noattrs", ["100x100"]),
        ("fakejpg", ["100x100"]),
    ):
        for size in sizes:
            add("A", key, f"thumb_{size}", params=[("thumb", size)], kind="thumb")
    for label, size in (
        ("not_allowed", "40x40"),
        ("uppercase", "100X100"),
        ("leading_space", " 100x100"),
        ("empty", ""),
        ("traversal", "../100x100"),
    ):
        add("A", "png", f"thumb_refused_{label}", params=[("thumb", size)])
    add(
        "A",
        "png",
        "thumb_head",
        params=[("thumb", "100x100")],
        method="HEAD",
        kind="thumb",
    )
    add(
        "A",
        "png",
        "thumb_download",
        params=[("thumb", "50x50"), ("download", "1")],
        kind="thumb",
    )
    add(
        "A",
        "png",
        "thumb_range",
        params=[("thumb", "100x100")],
        headers={"Range": "bytes=0-7"},
        kind="thumb",
    )
    add("A", "missing", "plain", kind="error")
    add("A", "missing", "thumb_100x100", params=[("thumb", "100x100")], kind="error")
    add("A", "png", "attrs_sidecar", suffix=".attrs", kind="error")
    # A name the record references that ends in .attrs (another file's
    # side-car): fileblob refuses the key in both versions.
    add("A", "attrs_ref", "plain", kind="error")
    add("A", "attrs_ref", "head", method="HEAD", kind="error")
    add("A", "attrs_ref", "thumb_100x100", params=[("thumb", "100x100")], kind="error")
    add("A", "attrs_ref", "download_1", params=[("download", "1")], kind="error")
    add("A", "png", "thumbs_dir", prefix="thumbs_", kind="error")
    add("A", "png", "unknown_record", record_id="zzzzzzzzzzzzzzz", kind="error")
    add("A", "png", "unknown_collection", collection="nope_collection", kind="error")
    # A collection without configured thumbs, like every BUMA file field.
    add("B", "png", "plain")
    add("B", "png", "thumb_100x100", params=[("thumb", "100x100")], kind="thumb")
    add("B", "png", "thumb_50x50", params=[("thumb", "50x50")])
    add("B", "jpg", "thumb_100x100", params=[("thumb", "100x100")], kind="thumb")
    # Protected files without a valid token: C has a public viewRule, D none.
    for label in ("C", "D"):
        add(label, "png", "protected_no_token", kind="error")
        add(
            label,
            "png",
            "protected_bad_token",
            params=[("token", "invalid.token.value")],
            kind="error",
        )
    for name, form in TOKEN_FORMS.items():
        add("D", "png", name, token=form)
    add("C", "png", "token_blank", token=TOKEN_FORMS["token_blank"])
    return rows


def _scenario_key(row: dict[str, Any]) -> str:
    return f"{row['record']}.{row['file']}.{row['name']}"


def _image_summary(content: bytes) -> dict[str, Any] | None:
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            rgb = image.convert("RGB")
            width, height = rgb.size
            samples = {}
            for label, (fx, fy) in {
                "top": (0.5, 0.1),
                "bottom": (0.5, 0.9),
                "left": (0.1, 0.5),
                "right": (0.9, 0.5),
            }.items():
                pixel = rgb.getpixel((int(fx * (width - 1)), int(fy * (height - 1))))
                samples[label] = "".join(
                    "1" if channel > 127 else "0" for channel in pixel
                )
            return {"format": image.format, "size": [width, height], "samples": samples}
    except Exception:  # noqa: BLE001 - not an image
        return None


async def run_scenarios(
    client: httpx.AsyncClient,
    targets: dict[str, dict[str, Any]],
    storage_root: Path,
    file_token: str,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for row in scenarios():
        target = targets[row["record"]]
        filename = target["files"][row["file"]]
        filename = f"{row.get('prefix', '')}{filename}{row.get('suffix', '')}"
        collection = row.get("collection", target["collection"])
        record_id = row.get("record_id", target["record"])
        params = list(row.get("params") or [])
        if "token" in row:
            params.append(("token", row["token"](file_token)))
        response = await client.request(
            row.get("method", "GET"),
            f"/api/files/{collection}/{record_id}/{filename}",
            params=params or None,
            headers=row.get("headers"),
        )
        kind = row.get("kind", "file")
        entry: dict[str, Any] = {"status": response.status_code, "kind": kind}
        if kind not in {"error", "range_error"}:
            entry["headers"] = {
                name: response.headers.get(name) for name in COMPARED_HEADERS
            }
            entry["sha256"] = hashlib.sha256(response.content).hexdigest()
            entry["body_bytes"] = len(response.content)
            if (
                kind == "thumb"
                and row.get("method") != "HEAD"
                and response.status_code == 200
            ):
                entry["image"] = _image_summary(response.content)
        elif kind == "range_error":
            entry["headers"] = {"content-range": response.headers.get("content-range")}
        results[_scenario_key(row)] = entry
    # The side-car of a thumbnail each server generated itself.
    png = targets["A"]["files"]["png"]
    side_car = (
        storage_root
        / FILES_ID
        / targets["A"]["record"]
        / f"thumbs_{png}"
        / f"20x0_{png}.attrs"
    )
    thumb = side_car.with_suffix("")
    if side_car.exists():
        decoded = json.loads(side_car.read_bytes())
        md5 = hashlib.md5(thumb.read_bytes(), usedforsecurity=False).digest()
        results["sidecar.thumb_20x0"] = {
            "keys": list(decoded),
            "content_type": decoded.get("user.content_type"),
            "metadata": decoded.get("user.metadata"),
            "md5_matches_bytes": decoded.get("md5") == base64.b64encode(md5).decode(),
            "trailing_newline": side_car.read_bytes().endswith(b"\n"),
        }
    else:
        results["sidecar.thumb_20x0"] = None
    return results


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def pocketbase_server(
    binary: Path, root: Path
) -> AsyncIterator[httpx.AsyncClient]:
    flags = [
        "--dir",
        str(root / "pb_data"),
        "--migrationsDir",
        str(root / "pb_migrations"),
        "--hooksDir",
        str(root / "pb_hooks"),
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
        str(root / "pb_public"),
        *flags,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=60
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
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()


def _collection_payload(
    collection_id: str,
    name: str,
    thumbs: list[str],
    *,
    protected: bool = False,
    view_rule: str | None = "",
) -> dict[str, Any]:
    return {
        "id": collection_id,
        "name": name,
        "type": "base",
        "schema": [
            {
                "name": "docs",
                "type": "file",
                "required": False,
                "options": {
                    "maxSelect": 50,
                    "maxSize": 5242880,
                    "mimeTypes": [],
                    "thumbs": thumbs,
                    "protected": protected,
                },
            }
        ],
        "listRule": view_rule,
        "viewRule": view_rule,
    }


# label, collection id, name, thumbs, protected, viewRule, uploaded keys.
COLLECTIONS = (
    ("A", FILES_ID, "probe_files", THUMBS, False, "", None),
    ("B", PLAIN_ID, "probe_plain", [], False, "", ["png", "jpg"]),
    ("C", PROTECTED_PUBLIC_ID, "probe_prot_public", [], True, "", ["png"]),
    ("D", PROTECTED_ADMIN_ID, "probe_prot_admin", [], True, None, ["png"]),
)
SCENARIO_PREFIXES = tuple(f"{label}." for label, *_rest in COLLECTIONS)


def _reference_in_pocketbase(
    data_db: Path, table: str, record_id: str, name: str
) -> None:
    """Append ``name`` to a record's file list in PocketBase's SQLite.

    The API only stores uploads, and fileblob refuses to write a key ending
    in ``.attrs``: only an import or a direct edit can reference one.
    """
    with contextlib.closing(sqlite3.connect(data_db, timeout=30)) as connection:
        (raw,) = connection.execute(
            f'SELECT "docs" FROM "{table}" WHERE "id" = ?', (record_id,)
        ).fetchone()
        docs = [*json.loads(raw), name]
        connection.execute(
            f'UPDATE "{table}" SET "docs" = ? WHERE "id" = ?',
            (json.dumps(docs), record_id),
        )
        connection.commit()


def _stored_names(
    uploaded: list[str], record_files: list[str], keys: list[str]
) -> dict[str, str]:
    names: dict[str, str] = {}
    for key, upload in zip(keys, uploaded):
        stem = Path(upload).stem
        matches = [value for value in record_files if value.startswith(f"{stem}_")]
        if len(matches) != 1:
            raise RuntimeError(f"cannot map {upload}: {record_files}")
        names[key] = matches[0]
    return names


async def run_pocketbase(
    binary: Path, work: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    files = synthetic_files()
    async with pocketbase_server(binary, work / "pocketbase") as client:
        login = await client.post(
            "/api/admins/auth-with-password",
            json={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        login.raise_for_status()
        admin = {"Authorization": login.json()["token"]}
        targets: dict[str, dict[str, Any]] = {}
        for label, collection_id, name, thumbs, protected, rule, keys in COLLECTIONS:
            keys = keys or list(files)
            response = await client.post(
                "/api/collections",
                headers=admin,
                json=_collection_payload(
                    collection_id, name, thumbs, protected=protected, view_rule=rule
                ),
            )
            response.raise_for_status()
            upload = [("docs", (files[key][0], files[key][1])) for key in keys]
            response = await client.post(
                f"/api/collections/{name}/records", headers=admin, files=upload
            )
            response.raise_for_status()
            record = response.json()
            targets[label] = {
                "collection": collection_id,
                "record": record["id"],
                "files": _stored_names(
                    [files[key][0] for key in keys], record["docs"], keys
                ),
            }
        storage = work / "pocketbase" / "pb_data" / "storage"
        for label, key, size in PREGENERATED:
            target = targets[label]
            response = await client.get(
                f"/api/files/{target['collection']}/{target['record']}/{target['files'][key]}",
                params={"thumb": size},
            )
            response.raise_for_status()
        # Files the probe takes away in both storages: a side-car (PPBase's
        # own uploads have none) and a referenced file (D-0021 imports
        # references to absent files as they are).
        record_dir = storage / FILES_ID / targets["A"]["record"]
        (record_dir / f"{targets['A']['files']['noattrs']}.attrs").unlink()
        (record_dir / targets["A"]["files"]["missing"]).unlink()
        (record_dir / f"{targets['A']['files']['missing']}.attrs").unlink()
        for key, payload in CRAFTED_ATTRS.items():
            (record_dir / f"{targets['A']['files'][key]}.attrs").write_bytes(payload)
        attrs_ref = f"{targets['A']['files']['txt']}.attrs"
        _reference_in_pocketbase(
            work / "pocketbase" / "pb_data" / "data.db",
            "probe_files",
            targets["A"]["record"],
            attrs_ref,
        )
        targets["A"]["files"]["attrs_ref"] = attrs_ref
        shutil.copytree(storage, work / "storage-copy", copy_function=shutil.copy2)
        response = await client.post("/api/files/token", headers=admin)
        response.raise_for_status()
        results = await run_scenarios(
            client, targets, storage, response.json()["token"]
        )
        results["sidecar.upload_keys"] = list(
            json.loads(
                (record_dir / f"{targets['A']['files']['png']}.attrs").read_bytes()
            )
        )
    return results, targets


async def run_ppbase(
    database_url: str, work: Path, targets: dict[str, Any], compat: str
) -> dict[str, Any]:
    from httpx import ASGITransport
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ppbase.app import create_app
    from ppbase.config import Settings
    from ppbase.db.bootstrap import bootstrap_system_collections
    from ppbase.db.engine import close_engine, init_engine
    from ppbase.db.system_tables import CollectionRecord, create_system_tables
    from ppbase.services.admin_service import create_admin
    from ppbase.services.auth_service import create_admin_token

    secret = "probe-secret-key-for-file-downloads-0000000001"
    data_dir = work / "ppbase_data"
    data_dir.mkdir()
    shutil.copytree(
        work / "storage-copy", data_dir / "storage", copy_function=shutil.copy2
    )
    settings = Settings(
        database_url=database_url,
        jwt_secret=secret,
        auto_migrate=False,
        data_dir=str(data_dir),
    )
    app = create_app(settings)
    if compat == "0.22":
        # The PocketBase 0.22 compatibility mode (ppbase.storage.serving
        # pocketbase_compat, the switch of the 0.22 error mode).
        app.state.error_compat = "0.22"
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
        admin = {"Authorization": token}
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=120
        ) as client:
            for (
                label,
                collection_id,
                name,
                thumbs,
                protected,
                rule,
                _keys,
            ) in COLLECTIONS:
                response = await client.post(
                    "/api/collections",
                    headers=admin,
                    json=_collection_payload(
                        collection_id, name, thumbs, protected=protected, view_rule=rule
                    ),
                )
                response.raise_for_status()
                target = targets[label]
                # The records an import would write: same IDs, same names.
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            f'INSERT INTO "{name}" ("id", "docs") VALUES (:id, :docs)'
                        ),
                        {
                            "id": target["record"],
                            "docs": list(target["files"].values()),
                        },
                    )
            response = await client.post("/api/files/token", headers=admin)
            response.raise_for_status()
            return await run_scenarios(
                client, targets, data_dir / "storage", response.json()["token"]
            )
    finally:
        await close_engine()


def _compare(
    pocketbase: dict[str, Any], ppbase: dict[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    pregenerated = {f"{label}.{key}.thumb_{size}" for label, key, size in PREGENERATED}
    pregenerated |= {"A.png.thumb_head", "A.png.thumb_range", "A.png.thumb_download"}
    for key in sorted(set(pocketbase) | set(ppbase)):
        left, right = pocketbase.get(key), ppbase.get(key)
        if not key.startswith(SCENARIO_PREFIXES):
            if key == "sidecar.upload_keys":
                continue
            if left != right:
                rows.append({"path": key, "pocketbase": left, "ppbase": right})
            continue
        if left is None or right is None:
            rows.append({"path": key, "pocketbase": left, "ppbase": right})
            continue
        fields = ["status"]
        if left.get("status") != right.get("status"):
            pass  # compared on the status only
        elif left.get("kind") == "range_error":
            fields.append("headers.content-range")
        elif left.get("kind") != "error" and int(left.get("status") or 0) < 400:
            generated_here = left.get("kind") == "thumb" and key not in pregenerated
            for header in COMPARED_HEADERS:
                if generated_here and header in {"content-length", "last-modified"}:
                    continue
                fields.append(f"headers.{header}")
            if not generated_here:
                fields.append("sha256")
            fields.append("image")
        for field in fields:
            a, b = left, right
            for part in field.split("."):
                a = a.get(part) if isinstance(a, dict) else None
                b = b.get(part) if isinstance(b, dict) else None
            if a != b:
                rows.append({"path": f"{key}.{field}", "pocketbase": a, "ppbase": b})
    return rows


def _ppbase_module() -> str:
    import ppbase

    return str(Path(ppbase.__file__).resolve().parent)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compat", choices=("0.22", "default"), default="0.22")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ppbase-files-probe-") as root:
        work = Path(root)
        pocketbase, targets = asyncio.run(run_pocketbase(args.pocketbase, work))
        ppbase = asyncio.run(run_ppbase(args.database_url, work, targets, args.compat))
    differences = _compare(pocketbase, ppbase)
    for row in differences:
        row["known"] = known_divergence(row, args.compat)
    for name, payload in (
        ("pocketbase.json", pocketbase),
        ("ppbase.json", ppbase),
        ("diff.json", differences),
    ):
        (args.out / name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n"
        )
    unexpected = [row for row in differences if not row["known"]]
    scenario_count = sum(1 for key in pocketbase if key.startswith(SCENARIO_PREFIXES))
    print(
        json.dumps(
            {
                "compat": args.compat,
                "scenarios": scenario_count,
                "differences": len(differences),
                "known": len(differences) - len(unexpected),
                "unexpected": len(unexpected),
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
