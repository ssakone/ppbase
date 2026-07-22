from __future__ import annotations

import re
from pathlib import Path

import pytest

from ppbase.config import Settings
from ppbase.core.storage_safety import StorageSafetyError
from ppbase.services import file_storage
from ppbase.services.write_barrier import WriteBarrierLease, WriteBarrierMode


@pytest.fixture(autouse=True)
def _inject_explicit_storage_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise low-level backends beneath the guarded public facade."""
    shared = WriteBarrierLease(
        connection=None,  # type: ignore[arg-type]
        mode=WriteBarrierMode.SHARED,
        backend_pid=0,
        barrier_key=0,
    )
    exclusive = WriteBarrierLease(
        connection=None,  # type: ignore[arg-type]
        mode=WriteBarrierMode.EXCLUSIVE,
        backend_pid=0,
        barrier_key=0,
    )
    for lease in (shared, exclusive):
        lease._active = True
        lease._barrier_acquired = True

    for name in ("save_files", "delete_files", "delete_all_files"):
        original = getattr(file_storage, name)

        def _with_shared(*args, _original=original, **kwargs):
            kwargs.setdefault("lease", shared)
            return _original(*args, **kwargs)

        monkeypatch.setattr(file_storage, name, _with_shared)

    for name in (
        "set_storage_settings",
        "configure_storage_runtime_from_settings_payload",
        "clear_runtime_storage_overrides",
    ):
        original = getattr(file_storage, name)

        def _with_exclusive(*args, _original=original, **kwargs):
            kwargs.setdefault("lease", exclusive)
            return _original(*args, **kwargs)

        monkeypatch.setattr(file_storage, name, _with_exclusive)


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

    def put_object(  # noqa: N803
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        IfNoneMatch: str | None = None,
    ) -> None:
        assert IfNoneMatch == "*"
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
            "_pb_users_auth_",
            "record_id",
            "avatar",
            [("avatar.png", b"avatar-bytes")],
            max_select=1,
        )
        assert len(saved) == 1
        filename = saved[0]
        assert re.match(r"^avatar_[A-Za-z0-9]{10}\.png$", filename)

        object_key = f"_pb_users_auth_/record_id/{filename}"
        assert fake_s3.objects[("test-bucket", object_key)] == b"avatar-bytes"

        local_candidate = tmp_path / "storage" / "_pb_users_auth_" / "record_id" / filename
        assert not local_candidate.exists()

        payload = file_storage.read_file_bytes("_pb_users_auth_", "record_id", filename)
        assert payload == b"avatar-bytes"

        file_storage.delete_files("_pb_users_auth_", "record_id", [filename])
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
            "_pbc_2287844090",
            "record_id",
            "document",
            [("notes.txt", b"local-bytes")],
            max_select=1,
        )
        assert len(saved) == 1
        filename = saved[0]
        local_candidate = tmp_path / "storage" / "_pbc_2287844090" / "record_id" / filename
        assert local_candidate.is_file()
        assert local_candidate.read_bytes() == b"local-bytes"
    finally:
        _cleanup_storage_runtime()


def test_durable_storage_config_resolver_is_pure_and_honors_explicit_disable(
    tmp_path: Path,
) -> None:
    live_settings = Settings(data_dir=str(tmp_path), storage_backend="local")
    file_storage.set_storage_settings(live_settings)
    file_storage.configure_storage_runtime_from_settings_payload(
        {
            "s3": {
                "enabled": True,
                "bucket": "live-bucket",
                "accessKey": "live-access",
                "secret": "live-secret",
            }
        }
    )
    assert file_storage.get_storage_backend() == "s3"

    environment_settings = Settings(
        data_dir=str(tmp_path),
        storage_backend="s3",
        s3_bucket="environment-bucket",
        s3_access_key="environment-access",
        s3_secret_key="environment-secret",
    )
    resolved = file_storage.resolve_storage_config_from_settings_payload(
        environment_settings,
        {"s3": {"enabled": False}},
    )

    try:
        assert resolved.backend == "local"
        assert resolved.data_dir == str(tmp_path)
        assert environment_settings.storage_backend == "s3"
        assert file_storage.get_storage_backend() == "s3"
    finally:
        _cleanup_storage_runtime()


def test_s3_rejects_unsafe_components_before_client_call(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_s3 = _FakeS3Client()
    client_calls = 0

    def _fake_get_s3_client(_cfg):
        nonlocal client_calls
        client_calls += 1
        return fake_s3

    monkeypatch.setattr(file_storage, "_get_s3_client", _fake_get_s3_client)
    file_storage.set_storage_settings(Settings(data_dir=str(tmp_path)))
    file_storage.configure_storage_runtime_from_settings_payload(
        {
            "s3": {
                "enabled": True,
                "bucket": "test-bucket",
                "accessKey": "access-key",
                "secret": "secret-key",
            }
        }
    )

    try:
        with pytest.raises(StorageSafetyError):
            file_storage.save_files(
                "../collection",
                "record_id",
                "document",
                [("safe.txt", b"payload")],
            )
        with pytest.raises(StorageSafetyError):
            file_storage.read_file_bytes(
                "_pbc_2287844090",
                "record_id",
                "../safe.txt",
            )
        with pytest.raises(StorageSafetyError):
            file_storage.delete_all_files("_pbc_2287844090", "../record")

        assert client_calls == 0
        assert fake_s3.objects == {}
    finally:
        _cleanup_storage_runtime()


def test_s3_malicious_filename_is_not_normalized_to_existing_object(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_s3 = _FakeS3Client()
    collection_id = "_pbc_2287844090"
    record_id = "record_id"
    object_key = f"{collection_id}/{record_id}/safe.txt"
    fake_s3.objects[("test-bucket", object_key)] = b"keep"
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _cfg: fake_s3)
    file_storage.set_storage_settings(Settings(data_dir=str(tmp_path)))
    file_storage.configure_storage_runtime_from_settings_payload(
        {
            "s3": {
                "enabled": True,
                "bucket": "test-bucket",
                "accessKey": "access-key",
                "secret": "secret-key",
            }
        }
    )

    try:
        with pytest.raises(StorageSafetyError):
            file_storage.read_file_bytes(collection_id, record_id, "../safe.txt")
        with pytest.raises(StorageSafetyError):
            file_storage.delete_files(collection_id, record_id, ["../safe.txt"])

        assert fake_s3.objects[("test-bucket", object_key)] == b"keep"
    finally:
        _cleanup_storage_runtime()


def test_s3_delete_all_is_limited_to_validated_record_prefix(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake_s3 = _FakeS3Client()
    collection_id = "_pbc_2287844090"
    record_id = "record_id"
    neighbor_id = "neighbor_id"
    record_key = f"{collection_id}/{record_id}/record.txt"
    neighbor_key = f"{collection_id}/{neighbor_id}/neighbor.txt"
    fake_s3.objects[("test-bucket", record_key)] = b"delete"
    fake_s3.objects[("test-bucket", neighbor_key)] = b"keep"
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _cfg: fake_s3)
    file_storage.set_storage_settings(Settings(data_dir=str(tmp_path)))
    file_storage.configure_storage_runtime_from_settings_payload(
        {
            "s3": {
                "enabled": True,
                "bucket": "test-bucket",
                "accessKey": "access-key",
                "secret": "secret-key",
            }
        }
    )

    try:
        file_storage.delete_all_files(collection_id, record_id)

        assert ("test-bucket", record_key) not in fake_s3.objects
        assert fake_s3.objects[("test-bucket", neighbor_key)] == b"keep"
    finally:
        _cleanup_storage_runtime()
