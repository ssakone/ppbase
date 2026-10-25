"""Files stored the PocketBase way (``.attrs`` side-cars and thumbnails).

A PocketBase backup keeps, next to every record file, the ``fileblob``
side-car ``<file>.attrs`` holding its real content type, and the generated
thumbnails ``thumbs_<file>/<size>_<file>`` with their own side-cars.  These
tests place such files under PPBase's storage, as an import does, and check
that PPBase serves them like PocketBase v0.22.34 (``apis/file.go`` download,
``tools/filesystem`` ``Serve``).  The paired comparison with the 0.22.34
binary is ``tests/pocketbase_parity/files_probe.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import text

from ppbase.db.engine import get_engine
from ppbase.services.file_storage import get_storage_path
from ppbase.storage.serving import encode_attrs

pytestmark = pytest.mark.asyncio

CSP = "default-src 'none'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
CACHE = "max-age=2592000, stale-while-revalidate=86400"


def _png(size: tuple[int, int], color: tuple[int, int, int] = (40, 120, 200)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _pocketbase_attrs(content: bytes, content_type: str, original: str) -> bytes:
    """A side-car as PocketBase 0.22.34's upload writes it (with metadata)."""
    return encode_attrs(
        content, content_type, {"original-filename": original}, compat="0.22"
    )


async def _record_with_files(
    app_client: AsyncClient,
    admin_token: str,
    files: list[tuple[str, bytes, str]],
    *,
    thumbs: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    name = f"pbfiles_{uuid.uuid4().hex[:8]}"
    created = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": name,
            "type": "base",
            "schema": [
                {
                    "name": "docs",
                    "type": "file",
                    "required": False,
                    "options": {"maxSelect": 10, "thumbs": thumbs or []},
                }
            ],
            "listRule": "",
            "viewRule": "",
        },
    )
    assert created.status_code == 200, created.text
    collection = created.json()
    record_response = await app_client.post(
        f"/api/collections/{name}/records",
        headers={"Authorization": admin_token},
        files=[("docs", item) for item in files],
    )
    assert record_response.status_code == 200, record_response.text
    record = record_response.json()
    stored = {}
    for original, *_rest in files:
        stem = Path(original).stem
        matches = [value for value in record["docs"] if value.startswith(f"{stem}_")]
        assert len(matches) == 1, record["docs"]
        stored[original] = matches[0]
    return collection, record, stored


def _url(collection: dict[str, Any], record: dict[str, Any], filename: str) -> str:
    return f"/api/files/{collection['id']}/{record['id']}/{filename}"


def _inline(name: str, compat: str = "") -> str:
    """v0.40.6 quotes the name (``filesystem.go`` l.587), v0.22.34 does not."""
    return f"inline; filename={name}" if compat else f'inline; filename="{name}"'


def _attachment(name: str, compat: str = "") -> str:
    if compat:
        return f"attachment; filename={name}"
    return f'attachment; filename="{name}"'


def _assert_pocketbase_headers(
    response: Any, content_type: str, disposition: str, compat: str = ""
) -> None:
    assert response.headers["content-type"] == content_type
    assert response.headers["content-disposition"] == disposition
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["cache-control"] == CACHE
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-xss-protection"] == "1; mode=block"
    assert "x-frame-options" not in response.headers
    assert response.headers["accept-ranges"] == "bytes"
    if compat:
        assert "cross-origin-opener-policy" not in response.headers
    else:
        assert response.headers["cross-origin-opener-policy"] == "same-origin"


async def _delete(app_client: AsyncClient, admin_token: str, collection: dict) -> None:
    await app_client.delete(
        f"/api/collections/{collection['id']}",
        headers={"Authorization": admin_token},
    )


async def test_content_type_and_disposition_come_from_the_side_car(
    app_client: AsyncClient, admin_token: str
) -> None:
    photo = _png((120, 80))
    collection, record, stored = await _record_with_files(
        app_client,
        admin_token,
        [
            ("photo.png", photo, "image/png"),
            # PNG bytes under a name whose extension says otherwise: only the
            # recorded type tells what the file is (gap "c" of WS-D2).
            ("scan.bin", photo, "application/octet-stream"),
            ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
            ("theme.css", b"body{color:red}", "text/css"),
            ("manual.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf"),
            ("notes.txt", b"plain text", "text/plain"),
        ],
    )
    try:
        record_dir = get_storage_path(collection["id"], record["id"])
        for original, content_type in (
            ("photo.png", "image/png"),
            ("scan.bin", "image/png"),
            ("logo.svg", "text/plain; charset=utf-8"),
            ("theme.css", "text/plain; charset=utf-8"),
            ("manual.pdf", "application/pdf"),
        ):
            target = record_dir / stored[original]
            (record_dir / f"{stored[original]}.attrs").write_bytes(
                _pocketbase_attrs(target.read_bytes(), content_type, original)
            )

        response = await app_client.get(_url(collection, record, stored["photo.png"]))
        assert response.status_code == 200, response.text
        assert response.content == photo
        assert response.headers["content-length"] == str(len(photo))
        assert response.headers["last-modified"]
        _assert_pocketbase_headers(response, "image/png", _inline(stored["photo.png"]))

        response = await app_client.get(_url(collection, record, stored["scan.bin"]))
        assert response.status_code == 200
        _assert_pocketbase_headers(response, "image/png", _inline(stored["scan.bin"]))

        # .svg and .css force their type; the disposition follows the stored one.
        response = await app_client.get(_url(collection, record, stored["logo.svg"]))
        _assert_pocketbase_headers(
            response, "image/svg+xml", _attachment(stored["logo.svg"])
        )
        response = await app_client.get(_url(collection, record, stored["theme.css"]))
        _assert_pocketbase_headers(
            response, "text/css", _attachment(stored["theme.css"])
        )

        pdf_url = _url(collection, record, stored["manual.pdf"])
        inline = _inline(stored["manual.pdf"])
        attachment = _attachment(stored["manual.pdf"])
        for query, expected in (
            ("", inline),
            ("?download=1", attachment),
            ("?download=true", attachment),
            ("?download=T", attachment),
            ("?download=0", inline),
            ("?download=false", inline),
            ("?download=", inline),
            ("?download=yes", inline),
            ("?download=manual.pdf", inline),
            # Go's url.Values.Get reads the first value.
            ("?download=1&download=0", attachment),
            ("?download=0&download=1", inline),
        ):
            response = await app_client.get(pdf_url + query)
            assert response.status_code == 200, query
            _assert_pocketbase_headers(response, "application/pdf", expected)

        # Without side-car: PPBase's extension-based type (documented).
        response = await app_client.get(_url(collection, record, stored["notes.txt"]))
        _assert_pocketbase_headers(
            response,
            "text/plain; charset=utf-8",
            _attachment(stored["notes.txt"]),
        )

        # HEAD: the same headers, no body.
        response = await app_client.head(_url(collection, record, stored["photo.png"]))
        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["content-length"] == str(len(photo))
        _assert_pocketbase_headers(response, "image/png", _inline(stored["photo.png"]))

        # A side-car is never a servable file, and an undecodable one is a 404.
        for name in (f"{stored['photo.png']}.attrs", f"thumbs_{stored['photo.png']}"):
            response = await app_client.get(_url(collection, record, name))
            assert response.status_code == 404
        (record_dir / f"{stored['photo.png']}.attrs").write_bytes(b"{not json")
        response = await app_client.get(_url(collection, record, stored["photo.png"]))
        assert response.status_code == 404
        response = await app_client.get(
            _url(collection, record, stored["photo.png"]), params={"thumb": "100x100"}
        )
        assert response.status_code == 404
    finally:
        await _delete(app_client, admin_token, collection)


def _image_info(content: bytes) -> tuple[str, tuple[int, int]]:
    with Image.open(BytesIO(content)) as image:
        return str(image.format), image.size


async def test_thumbnails_are_generated_cached_and_published_with_a_side_car(
    app_client: AsyncClient, admin_token: str
) -> None:
    photo = _png((120, 80))
    collection, record, stored = await _record_with_files(
        app_client,
        admin_token,
        [("photo.png", photo, "image/png"), ("notes.txt", b"text", "text/plain")],
        thumbs=["50x50", "20x0", "30x20f"],
    )
    try:
        name = stored["photo.png"]
        url = _url(collection, record, name)
        thumbs_dir = get_storage_path(collection["id"], record["id"]) / f"thumbs_{name}"

        # The default 100x100 is always allowed, even when not configured.
        for size, expected in (
            ("100x100", (100, 100)),
            ("50x50", (50, 50)),
            ("20x0", (20, 13)),
            ("30x20f", (30, 20)),
        ):
            response = await app_client.get(url, params={"thumb": size})
            assert response.status_code == 200, response.text
            assert _image_info(response.content) == ("PNG", expected), size
            _assert_pocketbase_headers(response, "image/png", _inline(f"{size}_{name}"))
            published = thumbs_dir / f"{size}_{name}"
            assert published.read_bytes() == response.content
            side_car = json.loads((thumbs_dir / f"{size}_{name}.attrs").read_bytes())
            assert list(side_car) == [
                "user.cache_control",
                "user.content_disposition",
                "user.content_encoding",
                "user.content_language",
                "user.content_type",
                "user.metadata",
                "md5",
            ]
            assert side_car["user.content_type"] == "image/png"
            assert side_car["user.metadata"] == {}  # v0.40.6 json.MarshalWrite

        # A second request serves the published thumbnail unchanged.
        first = (thumbs_dir / f"100x100_{name}").read_bytes()
        os.utime(thumbs_dir / f"100x100_{name}", (1_600_000_000, 1_600_000_000))
        response = await app_client.get(url, params={"thumb": "100x100"})
        assert response.content == first
        assert response.headers["last-modified"] == "Sun, 13 Sep 2020 12:26:40 GMT"

        # Range and HEAD apply to the served thumbnail.
        response = await app_client.get(
            url, params={"thumb": "100x100"}, headers={"Range": "bytes=0-7"}
        )
        assert response.status_code == 206
        assert response.content == first[:8]
        assert response.headers["content-range"] == f"bytes 0-7/{len(first)}"
        response = await app_client.head(url, params={"thumb": "100x100"})
        assert response.status_code == 200 and response.content == b""
        assert response.headers["content-length"] == str(len(first))
        assert response.headers["content-disposition"] == _inline(f"100x100_{name}")

        # Sizes that are not allowed, or not matched exactly, serve the original.
        for size in ("40x40", "100X100", " 100x100", "100x100 ", "../100x100", "0x0"):
            response = await app_client.get(url, params={"thumb": size})
            assert response.status_code == 200
            assert response.content == photo, size
            assert response.headers["content-disposition"] == _inline(name)
        assert sorted(
            p.name for p in thumbs_dir.iterdir() if not p.name.endswith(".attrs")
        ) == [
            f"100x100_{name}",
            f"20x0_{name}",
            f"30x20f_{name}",
            f"50x50_{name}",
        ]

        # Not an image: the original, no thumbnail directory.
        text_name = stored["notes.txt"]
        response = await app_client.get(
            _url(collection, record, text_name), params={"thumb": "100x100"}
        )
        assert response.status_code == 200 and response.content == b"text"
        assert not (thumbs_dir.parent / f"thumbs_{text_name}").exists()
    finally:
        await _delete(app_client, admin_token, collection)


async def test_pocketbase_thumbnails_are_served_as_stored(
    app_client: AsyncClient, admin_token: str
) -> None:
    photo = _png((120, 80))
    collection, record, stored = await _record_with_files(
        app_client,
        admin_token,
        [
            ("photo.png", photo, "image/png"),
            ("scan.bin", photo, "application/octet-stream"),
            ("broken.png", photo, "image/png"),
        ],
    )
    try:
        record_dir = get_storage_path(collection["id"], record["id"])
        name = stored["photo.png"]
        (record_dir / f"{name}.attrs").write_bytes(
            _pocketbase_attrs(photo, "image/png", "photo.png")
        )
        # A thumbnail made by PocketBase (any bytes: PPBase must not re-encode).
        pocketbase_thumb = _png((100, 100), (250, 10, 10))
        thumbs_dir = record_dir / f"thumbs_{name}"
        thumbs_dir.mkdir()
        (thumbs_dir / f"100x100_{name}").write_bytes(pocketbase_thumb)
        (thumbs_dir / f"100x100_{name}.attrs").write_bytes(
            encode_attrs(pocketbase_thumb, "image/png", compat="0.22")
        )
        response = await app_client.get(
            _url(collection, record, name), params={"thumb": "100x100"}
        )
        assert response.status_code == 200
        assert response.content == pocketbase_thumb
        _assert_pocketbase_headers(response, "image/png", _inline(f"100x100_{name}"))

        # Without side-car (a thumbnail of an earlier PPBase): the original's type.
        (thumbs_dir / f"100x100_{name}.attrs").unlink()
        response = await app_client.get(
            _url(collection, record, name), params={"thumb": "100x100"}
        )
        assert response.content == pocketbase_thumb
        assert response.headers["content-type"] == "image/png"

        # An undecodable thumbnail side-car: fileblob's Exists fails, the
        # thumbnail is generated again and both files are replaced.
        (thumbs_dir / f"100x100_{name}.attrs").write_bytes(b"[")
        response = await app_client.get(
            _url(collection, record, name), params={"thumb": "100x100"}
        )
        assert response.status_code == 200
        assert response.content != pocketbase_thumb
        assert _image_info(response.content) == ("PNG", (100, 100))
        assert json.loads((thumbs_dir / f"100x100_{name}.attrs").read_bytes())["md5"]

        # The thumbnail decision follows the recorded type, not the extension.
        scan = stored["scan.bin"]
        response = await app_client.get(
            _url(collection, record, scan), params={"thumb": "100x100"}
        )
        assert response.content == photo  # application/octet-stream: original
        (record_dir / f"{scan}.attrs").write_bytes(
            _pocketbase_attrs(photo, "image/png", "scan.bin")
        )
        response = await app_client.get(
            _url(collection, record, scan), params={"thumb": "100x100"}
        )
        assert _image_info(response.content) == ("PNG", (100, 100))
        assert response.headers["content-disposition"] == _inline(f"100x100_{scan}")

        # An image PocketBase cannot decode: the original is served.
        broken = stored["broken.png"]
        (record_dir / broken).write_bytes(b"\x89PNG\r\n\x1a\n not really")
        response = await app_client.get(
            _url(collection, record, broken), params={"thumb": "100x100"}
        )
        assert response.status_code == 200
        assert response.content == b"\x89PNG\r\n\x1a\n not really"
        assert not (record_dir / f"thumbs_{broken}" / f"100x100_{broken}").exists()
    finally:
        await _delete(app_client, admin_token, collection)


async def test_symlinked_thumbnail_directory_is_never_followed(
    app_client: AsyncClient, admin_token: str, tmp_path: Path
) -> None:
    photo = _png((60, 60))
    collection, record, stored = await _record_with_files(
        app_client, admin_token, [("photo.png", photo, "image/png")]
    )
    try:
        record_dir = get_storage_path(collection["id"], record["id"])
        name = stored["photo.png"]
        outside = tmp_path / "outside"
        outside.mkdir()
        planted = _png((100, 100), (1, 2, 3))
        (outside / f"100x100_{name}").write_bytes(planted)
        (record_dir / f"thumbs_{name}").symlink_to(outside, target_is_directory=True)
        response = await app_client.get(
            _url(collection, record, name), params={"thumb": "100x100"}
        )
        assert response.status_code == 200
        assert response.content == photo
        assert sorted(p.name for p in outside.iterdir()) == [f"100x100_{name}"]
        assert hashlib.sha256((outside / f"100x100_{name}").read_bytes()).digest() == (
            hashlib.sha256(planted).digest()
        )
    finally:
        await _delete(app_client, admin_token, collection)


@contextlib.contextmanager
def _pocketbase_022(app_client: AsyncClient) -> Iterator[None]:
    """Serve in the PocketBase 0.22 compatibility mode (``app.state``)."""
    state = app_client._transport.app.state  # type: ignore[attr-defined]
    previous = getattr(state, "error_compat", None)
    state.error_compat = "0.22"
    try:
        yield
    finally:
        if previous is None:
            del state.error_compat
        else:
            state.error_compat = previous


async def _import_file(
    collection: dict[str, Any],
    record: dict[str, Any],
    name: str,
    content: bytes,
    content_type: str | None = None,
    *,
    attrs: bytes | None = None,
) -> None:
    """Add a stored file with any PocketBase name, as an import writes it."""
    record_dir = get_storage_path(collection["id"], record["id"])
    (record_dir / name).write_bytes(content)
    if attrs is None and content_type is not None:
        attrs = _pocketbase_attrs(content, content_type, name)
    if attrs is not None:
        (record_dir / f"{name}.attrs").write_bytes(attrs)
    async with get_engine().begin() as connection:
        await connection.execute(
            text(
                f'UPDATE "{collection["name"]}" '
                'SET "docs" = array_append("docs", :name) WHERE "id" = :id'
            ),
            {"name": name, "id": record["id"]},
        )


def _image_bytes(size: tuple[int, int], fmt: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (40, 120, 200)).save(output, format=fmt)
    return output.getvalue()


async def test_version_specific_rules_follow_the_compat_mode(
    app_client: AsyncClient, admin_token: str
) -> None:
    """v0.40.6 by default, v0.22.34 under the PocketBase 0.22 mode."""
    photo = _png((120, 80))
    collection, record, stored = await _record_with_files(
        app_client, admin_token, [("photo.png", photo, "image/png")], thumbs=["50x50"]
    )
    try:
        name = stored["photo.png"]
        record_dir = get_storage_path(collection["id"], record["id"])
        (record_dir / f"{name}.attrs").write_bytes(
            _pocketbase_attrs(photo, "image/png", "photo.png")
        )
        webp = _image_bytes((60, 40), "WEBP")
        await _import_file(
            collection, record, "pic_k3j2h1g0f9.webp", webp, "image/webp"
        )
        # PNG bytes under a .jpg name: the type recorded at upload is the
        # detected one, the thumbnail format is the version's choice.
        await _import_file(
            collection, record, "fake_k3j2h1g0f9.jpg", photo, "image/png"
        )
        # PocketBase keeps "=" in a stored extension (v0.22.34 file.go l.169).
        await _import_file(
            collection,
            record,
            "report_k3j2h1g0f9.v=1",
            b"t",
            "text/plain; charset=utf-8",
        )
        await _import_file(
            collection, record, "app_k3j2h1g0f9.js", b"x=1", "text/plain; charset=utf-8"
        )
        await _import_file(
            collection,
            record,
            "late_k3j2h1g0f9.png",
            photo,
            attrs=b'{"user.content_type":"image/png"} trailing',
        )

        def url(filename: str) -> str:
            return _url(collection, record, filename)

        # Default: v0.40.6.
        response = await app_client.get(url(name))
        _assert_pocketbase_headers(response, "image/png", _inline(name))
        response = await app_client.get(url("report_k3j2h1g0f9.v=1"))
        assert response.headers["content-disposition"] == (
            'attachment; filename="report_k3j2h1g0f9.v=1"'
        )
        response = await app_client.get(url("app_k3j2h1g0f9.js"))
        _assert_pocketbase_headers(
            response, "text/javascript", _attachment("app_k3j2h1g0f9.js")
        )
        # json/v2 refuses the trailing data of the side-car: 404.
        assert (await app_client.get(url("late_k3j2h1g0f9.png"))).status_code == 404
        # webp and image/png named .jpg: PNG thumbnails recorded as image/png.
        response = await app_client.get(
            url("pic_k3j2h1g0f9.webp"), params={"thumb": "100x100"}
        )
        assert _image_info(response.content) == ("PNG", (100, 100))
        _assert_pocketbase_headers(
            response, "image/png", _inline("100x100_pic_k3j2h1g0f9.webp")
        )
        side_car = (
            record_dir
            / "thumbs_pic_k3j2h1g0f9.webp"
            / "100x100_pic_k3j2h1g0f9.webp.attrs"
        ).read_bytes()
        assert not side_car.endswith(b"\n")
        decoded = json.loads(side_car)
        assert decoded["user.content_type"] == "image/png"
        assert decoded["user.metadata"] == {}
        response = await app_client.get(
            url("fake_k3j2h1g0f9.jpg"), params={"thumb": "100x100"}
        )
        assert _image_info(response.content) == ("PNG", (100, 100))
        assert response.headers["content-type"] == "image/png"

        with _pocketbase_022(app_client):
            compat = "0.22"
            response = await app_client.get(url(name))
            _assert_pocketbase_headers(
                response, "image/png", _inline(name, compat), compat
            )
            response = await app_client.get(url("report_k3j2h1g0f9.v=1"))
            assert response.headers["content-disposition"] == (
                "attachment; filename=report_k3j2h1g0f9.v=1"
            )
            response = await app_client.get(url("app_k3j2h1g0f9.js"))
            _assert_pocketbase_headers(
                response,
                "text/plain; charset=utf-8",
                _attachment("app_k3j2h1g0f9.js", compat),
                compat,
            )
            # json.Decoder (v1) reads the first value only.
            response = await app_client.get(url("late_k3j2h1g0f9.png"))
            assert response.status_code == 200
            _assert_pocketbase_headers(
                response, "image/png", _inline("late_k3j2h1g0f9.png", compat), compat
            )
            # No webp thumbnail in v0.22.34: the original.
            response = await app_client.get(
                url("pic_k3j2h1g0f9.webp"), params={"thumb": "100x100"}
            )
            assert response.content == webp
            _assert_pocketbase_headers(
                response, "image/webp", _inline("pic_k3j2h1g0f9.webp", compat), compat
            )
            # The format follows the thumbnail name, the type the original.
            response = await app_client.get(
                url("fake_k3j2h1g0f9.jpg"), params={"thumb": "50x50"}
            )
            assert _image_info(response.content) == ("JPEG", (50, 50))
            _assert_pocketbase_headers(
                response,
                "image/png",
                _inline("50x50_fake_k3j2h1g0f9.jpg", compat),
                compat,
            )
            side_car = (
                record_dir
                / "thumbs_fake_k3j2h1g0f9.jpg"
                / "50x50_fake_k3j2h1g0f9.jpg.attrs"
            ).read_bytes()
            assert side_car.endswith(b"\n")
            decoded = json.loads(side_car)
            assert decoded["user.content_type"] == "image/png"
            assert decoded["user.metadata"] is None
            # A thumbnail stored by the other mode is served as stored.
            response = await app_client.get(
                url("fake_k3j2h1g0f9.jpg"), params={"thumb": "100x100"}
            )
            assert _image_info(response.content) == ("PNG", (100, 100))
    finally:
        await _delete(app_client, admin_token, collection)


async def test_concurrent_requests_generate_a_missing_thumbnail_once(
    app_client: AsyncClient, admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One generation per thumbnail path, like PocketBase's singleflight."""
    import ppbase.api.files as files_api

    calls: list[str] = []
    create = files_api.create_thumb_bytes

    def counting(source: bytes, size: str, output_format: str) -> bytes | None:
        calls.append(size)
        time.sleep(0.3)  # in a worker thread: keeps the others waiting
        return create(source, size, output_format)

    monkeypatch.setattr(files_api, "create_thumb_bytes", counting)
    photo = _png((120, 80))
    collection, record, stored = await _record_with_files(
        app_client, admin_token, [("photo.png", photo, "image/png")], thumbs=["50x50"]
    )
    try:
        url = _url(collection, record, stored["photo.png"])
        responses = await asyncio.gather(
            *(app_client.get(url, params={"thumb": "50x50"}) for _ in range(4))
        )
        assert [response.status_code for response in responses] == [200] * 4
        assert len({response.content for response in responses}) == 1
        assert _image_info(responses[0].content) == ("PNG", (50, 50))
        assert calls == ["50x50"]
        assert not any(files_api._thumb_generation_pending.values())
    finally:
        await _delete(app_client, admin_token, collection)
