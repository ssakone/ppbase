from __future__ import annotations

import re
from pathlib import Path

from ppbase.config import Settings
from ppbase.services import file_storage


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        return None


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:  # noqa: N803
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, _FakeBody]:  # noqa: N803
        return {"Body": _FakeBody(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop((Bucket, Key), None)

    def list_objects_v2(
        self,
        *,
        Bucket: str,  # noqa: N803
        Prefix: str,  # noqa: N803
        ContinuationToken: str | None = None,  # noqa: N803
    ) -> dict[str, object]:
        _ = ContinuationToken
        keys = [
            key
            for (bucket, key), _payload in self.objects.items()
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {
            "Contents": [{"Key": key} for key in keys],
            "IsTruncated": False,
        }

    def delete_objects(self, *, Bucket: str, Delete: dict[str, object]) -> None:  # noqa: N803
        objects = Delete.get("Objects") if isinstance(Delete, dict) else None
        if not isinstance(objects, list):
            return
        for item in objects:
            if not isinstance(item, dict):
                continue
            key = str(item.get("Key", "") or "")
            if key:
                self.objects.pop((Bucket, key), None)


def _cleanup_storage_runtime() -> None:
    file_storage.clear_runtime_storage_overrides()
    file_storage.set_storage_settings(None)


def test_s3_runtime_backend_saves_reads_and_deletes_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_s3 = _FakeS3Client()
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _cfg: fake_s3)
    file_storage.set_storage_settings(Settings(data_dir=str(tmp_path), storage_backend="local"))
    file_storage.configure_storage_runtime_from_settings_payload(
        {
            "s3": {
                "endpoint": "https://example-r2.invalid",
                "bucket": "test-bucket",
                "region": "auto",
                "accessKey": "r2-access-key",
                "secret": "r2-secret",
                "forcePathStyle": True,
            }
        }
    )

    try:
        assert file_storage.get_storage_backend() == "s3"

        saved = file_storage.save_files(
            "users_collection_id",
            "record_id",
            "avatar",
            [("avatar.png", b"avatar-bytes")],
            max_select=1,
        )
        assert len(saved) == 1
        filename = saved[0]
        assert re.match(r"^avatar_[A-Za-z0-9]{10}\.png$", filename)

        object_key = f"users_collection_id/record_id/{filename}"
        assert fake_s3.objects[("test-bucket", object_key)] == b"avatar-bytes"

        local_candidate = tmp_path / "storage" / "users_collection_id" / "record_id" / filename
        assert not local_candidate.exists()

        payload = file_storage.read_file_bytes("users_collection_id", "record_id", filename)
        assert payload == b"avatar-bytes"

        file_storage.delete_files("users_collection_id", "record_id", [filename])
        assert ("test-bucket", object_key) not in fake_s3.objects
    finally:
        _cleanup_storage_runtime()


def test_empty_s3_settings_payload_falls_back_to_local_backend(tmp_path: Path) -> None:
    file_storage.set_storage_settings(Settings(data_dir=str(tmp_path), storage_backend="local"))
    file_storage.configure_storage_runtime_from_settings_payload(
        {
            "s3": {
                "endpoint": "https://example-r2.invalid",
                "bucket": "test-bucket",
                "region": "auto",
                "accessKey": "r2-access-key",
                "secret": "r2-secret",
            }
        }
    )
    assert file_storage.get_storage_backend() == "s3"

    file_storage.configure_storage_runtime_from_settings_payload(
        {
            "s3": {
                "enabled": False,
                "endpoint": "",
                "bucket": "",
                "region": "",
                "accessKey": "",
                "secret": "",
                "forcePathStyle": False,
            }
        }
    )

    try:
        assert file_storage.get_storage_backend() == "local"
        saved = file_storage.save_files(
            "posts_collection_id",
            "record_id",
            "document",
            [("notes.txt", b"local-bytes")],
            max_select=1,
        )
        assert len(saved) == 1
        filename = saved[0]
        local_candidate = tmp_path / "storage" / "posts_collection_id" / "record_id" / filename
        assert local_candidate.is_file()
        assert local_candidate.read_bytes() == b"local-bytes"
    finally:
        _cleanup_storage_runtime()
