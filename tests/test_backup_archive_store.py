from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from ppbase.backup.archive_store import BackupArchiveStore
from ppbase.backup.models import BackupAlreadyExistsError, BackupNotFoundError
from ppbase.backup.transport import BackupTransportError


def _zip_bytes(payload: bytes = b"{}") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr("backup.json", payload)
    return output.getvalue()


def test_archive_store_uses_pb_data_backups_and_hides_sidecars(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    data_dir.mkdir(mode=0o750)
    payload = _zip_bytes()

    with BackupArchiveStore(data_dir) as store:
        info = store.publish(
            io.BytesIO(payload),
            "probe.zip",
            require_zip_suffix=True,
            sniff_zip=True,
        )

        assert store.root == data_dir / "backups"
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o750
        assert store.list() == [info]
        assert info.key == "probe.zip"
        assert info.size == len(payload)

    attrs = json.loads((data_dir / "backups" / "probe.zip.attrs").read_text())
    assert attrs["user.content_type"] == "application/zip"
    assert attrs["user.metadata"] == {"original-filename": "probe.zip"}
    assert base64.b64decode(attrs["md5"]) == hashlib.md5(
        payload,
        usedforsecurity=False,
    ).digest()


def test_archive_store_follows_the_configured_pb_data_location(tmp_path: Path) -> None:
    real_data = tmp_path / "real-data"
    real_data.mkdir()
    linked_data = tmp_path / "pb_data"
    linked_data.symlink_to(real_data, target_is_directory=True)

    with BackupArchiveStore(linked_data) as store:
        store.publish(
            io.BytesIO(_zip_bytes()),
            "linked.zip",
            require_zip_suffix=True,
            sniff_zip=True,
        )

    assert (real_data / "backups" / "linked.zip").is_file()


def test_archive_store_refuses_non_zip_without_residue(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    data_dir.mkdir()
    with BackupArchiveStore(data_dir) as store:
        with pytest.raises(BackupTransportError) as error:
            store.publish(
                io.BytesIO(b"not a zip"),
                "invalid.zip",
                require_zip_suffix=True,
                sniff_zip=True,
            )
        assert error.value.code == "backup_upload_invalid_mime"
        assert store.list() == []
        assert list(store.root.iterdir()) == []


def test_archive_store_duplicate_does_not_replace_zip_or_attrs(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    data_dir.mkdir()
    first = _zip_bytes(b'{"first":true}')
    second = _zip_bytes(b'{"second":true}')
    with BackupArchiveStore(data_dir) as store:
        store.publish(
            io.BytesIO(first),
            "same.zip",
            require_zip_suffix=True,
            sniff_zip=True,
        )
        attrs_before = (store.root / "same.zip.attrs").read_bytes()
        with pytest.raises(BackupAlreadyExistsError):
            store.publish(
                io.BytesIO(second),
                "same.zip",
                require_zip_suffix=True,
                sniff_zip=True,
            )
        assert (store.root / "same.zip").read_bytes() == first
        assert (store.root / "same.zip.attrs").read_bytes() == attrs_before


def test_archive_store_refuses_orphaned_sidecar_without_replacing_it(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "pb_data"
    data_dir.mkdir()
    with BackupArchiveStore(data_dir) as store:
        attrs = store.root / "orphan.zip.attrs"
        attrs.write_bytes(b"existing-sidecar")

        with pytest.raises(BackupAlreadyExistsError):
            store.publish(
                io.BytesIO(_zip_bytes()),
                "orphan.zip",
                require_zip_suffix=True,
                sniff_zip=True,
            )

        assert not (store.root / "orphan.zip").exists()
        assert attrs.read_bytes() == b"existing-sidecar"


def test_archive_store_delete_removes_zip_and_sidecar(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    data_dir.mkdir()
    with BackupArchiveStore(data_dir) as store:
        store.publish(
            io.BytesIO(_zip_bytes()),
            "delete.zip",
            require_zip_suffix=True,
            sniff_zip=True,
        )
        store.delete("delete.zip")
        assert store.list() == []
        with pytest.raises(BackupNotFoundError):
            store.pin("delete.zip")
    assert not (data_dir / "backups" / "delete.zip.attrs").exists()


def test_archive_store_enforces_upload_size_limit(tmp_path: Path) -> None:
    data_dir = tmp_path / "pb_data"
    data_dir.mkdir()
    with BackupArchiveStore(data_dir) as store:
        with pytest.raises(BackupTransportError) as error:
            store.publish(
                io.BytesIO(_zip_bytes()),
                "large.zip",
                require_zip_suffix=True,
                sniff_zip=True,
                max_size=4,
            )
        assert error.value.code == "backup_upload_too_large"
        assert list(store.root.iterdir()) == []
