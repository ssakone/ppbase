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
compared.  Error responses are compared on their status only: the JSON
envelope belongs to the error compatibility mode.  Differences listed in
``KNOWN_DIVERGENCES`` are documented in ``docs/file-serving.md``.

Only synthetic files are used.  PocketBase runs in a temporary directory on
a random loopback port and is always stopped; PPBase runs in-process (ASGI)
on ``--database-url``, which must point to an empty, disposable PostgreSQL
database.

Usage (from the repository root)::

    PYTHONPATH=. python tests/pocketbase_parity/files_probe.py \\
        --pocketbase /path/to/pocketbase-0.22.34 \\
        --database-url postgresql+asyncpg://user:pass@127.0.0.1:5432/probe \\
        --out /tmp/files-probe
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
)
RED = (255, 0, 0)
BLUE = (0, 0, 255)

KNOWN_DIVERGENCES = {
    # fileblob serves a blob without side-car as application/octet-stream
    # (an attachment, never a thumbnail); PPBase keeps its extension-based
    # type for such files, which only its own uploads lack.
    "A.noattrs.plain.headers.content-disposition": "no .attrs: extension type",
    "A.noattrs.plain.headers.content-type": "no .attrs: extension type",
    "A.noattrs.thumb_100x100.headers.content-disposition": "no .attrs: thumbnail",
    "A.noattrs.thumb_100x100.headers.content-type": "no .attrs: thumbnail",
    "A.noattrs.thumb_100x100.image": "no .attrs: thumbnail",
    # PocketBase 0.22.34 makes no webp thumbnail (apis/file.go l.26); v0.40.6
    # and PPBase do.
    "A.webp.thumb_100x100.headers.content-disposition": "webp thumbnail",
    "A.webp.thumb_100x100.image": "webp thumbnail",
}


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
    add("A", "png", "thumbs_dir", prefix="thumbs_", kind="error")
    add("A", "png", "unknown_record", record_id="zzzzzzzzzzzzzzz", kind="error")
    add("A", "png", "unknown_collection", collection="nope_collection", kind="error")
    # A collection without configured thumbs, like every BUMA file field.
    add("B", "png", "plain")
    add("B", "png", "thumb_100x100", params=[("thumb", "100x100")], kind="thumb")
    add("B", "png", "thumb_50x50", params=[("thumb", "50x50")])
    add("B", "jpg", "thumb_100x100", params=[("thumb", "100x100")], kind="thumb")
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
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for row in scenarios():
        target = targets[row["record"]]
        filename = target["files"][row["file"]]
        filename = f"{row.get('prefix', '')}{filename}{row.get('suffix', '')}"
        collection = row.get("collection", target["collection"])
        record_id = row.get("record_id", target["record"])
        response = await client.request(
            row.get("method", "GET"),
            f"/api/files/{collection}/{record_id}/{filename}",
            params=row.get("params"),
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
    collection_id: str, name: str, thumbs: list[str]
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
                    "maxSelect": 20,
                    "maxSize": 5242880,
                    "mimeTypes": [],
                    "thumbs": thumbs,
                    "protected": False,
                },
            }
        ],
        "listRule": "",
        "viewRule": "",
    }


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
        for label, collection_id, name, thumbs, keys in (
            ("A", FILES_ID, "probe_files", THUMBS, list(files)),
            ("B", PLAIN_ID, "probe_plain", [], ["png", "jpg"]),
        ):
            response = await client.post(
                "/api/collections",
                headers=admin,
                json=_collection_payload(collection_id, name, thumbs),
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
        shutil.copytree(storage, work / "storage-copy", copy_function=shutil.copy2)
        results = await run_scenarios(client, targets, storage)
        results["sidecar.upload_keys"] = list(
            json.loads(
                (record_dir / f"{targets['A']['files']['png']}.attrs").read_bytes()
            )
        )
    return results, targets


async def run_ppbase(
    database_url: str, work: Path, targets: dict[str, Any]
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
            for label, collection_id, name, thumbs in (
                ("A", FILES_ID, "probe_files", THUMBS),
                ("B", PLAIN_ID, "probe_plain", []),
            ):
                response = await client.post(
                    "/api/collections",
                    headers=admin,
                    json=_collection_payload(collection_id, name, thumbs),
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
            return await run_scenarios(client, targets, data_dir / "storage")
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
        if not key.startswith(("A.", "B.")):
            if key == "sidecar.upload_keys":
                continue
            if left != right:
                rows.append({"path": key, "pocketbase": left, "ppbase": right})
            continue
        if left is None or right is None:
            rows.append({"path": key, "pocketbase": left, "ppbase": right})
            continue
        fields = ["status"]
        if left.get("kind") == "range_error":
            fields.append("headers.content-range")
        elif left.get("kind") != "error":
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
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ppbase-files-probe-") as root:
        work = Path(root)
        pocketbase, targets = asyncio.run(run_pocketbase(args.pocketbase, work))
        ppbase = asyncio.run(run_ppbase(args.database_url, work, targets))
    differences = _compare(pocketbase, ppbase)
    for row in differences:
        row["known"] = KNOWN_DIVERGENCES.get(row["path"])
    for name, payload in (
        ("pocketbase.json", pocketbase),
        ("ppbase.json", ppbase),
        ("diff.json", differences),
    ):
        (args.out / name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n"
        )
    unexpected = [row for row in differences if not row["known"]]
    scenario_count = sum(1 for key in pocketbase if key.startswith(("A.", "B.")))
    print(
        json.dumps(
            {
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
