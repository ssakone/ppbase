"""File storage service with local and S3-compatible backends."""

from __future__ import annotations

import re
import secrets
import shutil
import string
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from ppbase.config import Settings

_settings: Settings | None = None
_runtime_storage_overrides: dict[str, Any] | None = None
_runtime_lock = Lock()
_s3_client_cache: Any | None = None
_s3_client_cache_key: tuple[Any, ...] | None = None

_SAFE_STEM_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")
_ALPHANUM = string.ascii_letters + string.digits


@dataclass(frozen=True)
class _StorageConfig:
    data_dir: str
    backend: str
    s3_endpoint: str
    s3_bucket: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    s3_force_path_style: bool


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_storage_settings(settings: Settings | None) -> None:
    """Bind storage helpers to the active app settings instance."""
    global _settings
    _settings = settings


def _s3_has_required_credentials(values: dict[str, Any]) -> bool:
    return bool(
        str(values.get("s3_bucket", "") or "").strip()
        and str(values.get("s3_access_key", "") or "").strip()
        and str(values.get("s3_secret_key", "") or "").strip()
    )


def _resolve_storage_config() -> _StorageConfig:
    settings = _get_settings()
    values: dict[str, Any] = {
        "data_dir": str(getattr(settings, "data_dir", "./pb_data")),
        "storage_backend": str(getattr(settings, "storage_backend", "local") or "local"),
        "s3_endpoint": str(getattr(settings, "s3_endpoint", "") or "").strip(),
        "s3_bucket": str(getattr(settings, "s3_bucket", "") or "").strip(),
        "s3_region": str(getattr(settings, "s3_region", "") or "").strip(),
        "s3_access_key": str(getattr(settings, "s3_access_key", "") or "").strip(),
        "s3_secret_key": str(getattr(settings, "s3_secret_key", "") or "").strip(),
        "s3_force_path_style": bool(getattr(settings, "s3_force_path_style", False)),
    }

    with _runtime_lock:
        if isinstance(_runtime_storage_overrides, dict):
            values.update(_runtime_storage_overrides)

    backend = str(values.get("storage_backend", "local") or "local").strip().lower()
    if backend not in {"local", "s3"}:
        backend = "local"
    if backend == "s3" and not _s3_has_required_credentials(values):
        backend = "local"

    return _StorageConfig(
        data_dir=str(values.get("data_dir", getattr(settings, "data_dir", "./pb_data"))),
        backend=backend,
        s3_endpoint=str(values.get("s3_endpoint", "") or "").strip(),
        s3_bucket=str(values.get("s3_bucket", "") or "").strip(),
        s3_region=str(values.get("s3_region", "") or "").strip(),
        s3_access_key=str(values.get("s3_access_key", "") or "").strip(),
        s3_secret_key=str(values.get("s3_secret_key", "") or "").strip(),
        s3_force_path_style=bool(values.get("s3_force_path_style", False)),
    )


def _clear_s3_client_cache() -> None:
    global _s3_client_cache
    global _s3_client_cache_key
    with _runtime_lock:
        _s3_client_cache = None
        _s3_client_cache_key = None


def clear_runtime_storage_overrides() -> None:
    """Clear runtime storage overrides (fallback to environment settings)."""
    global _runtime_storage_overrides
    with _runtime_lock:
        _runtime_storage_overrides = None
    _clear_s3_client_cache()


def configure_storage_runtime_from_settings_payload(
    settings_value: dict[str, Any] | None,
) -> None:
    """Configure runtime storage backend overrides from settings payload."""
    global _runtime_storage_overrides

    overrides: dict[str, Any] | None = None
    if isinstance(settings_value, dict):
        raw_s3 = settings_value.get("s3")
        if isinstance(raw_s3, dict):
            endpoint = str(raw_s3.get("endpoint", "") or "").strip()
            bucket = str(raw_s3.get("bucket", "") or "").strip()
            region = str(raw_s3.get("region", "") or "").strip()
            access_key = str(raw_s3.get("accessKey", "") or "").strip()
            secret_key = str(raw_s3.get("secret", "") or "").strip()
            enabled_raw = raw_s3.get("enabled")
            enabled = bool(enabled_raw) if enabled_raw is not None else False
            has_any_value = any([endpoint, bucket, region, access_key, secret_key, enabled])

            if has_any_value:
                use_s3 = enabled or bool(bucket and access_key and secret_key)
                overrides = {
                    "storage_backend": "s3" if use_s3 else "local",
                    "s3_endpoint": endpoint,
                    "s3_bucket": bucket,
                    "s3_region": region,
                    "s3_access_key": access_key,
                    "s3_secret_key": secret_key,
                    "s3_force_path_style": bool(raw_s3.get("forcePathStyle", False)),
                }

    with _runtime_lock:
        _runtime_storage_overrides = overrides
    _clear_s3_client_cache()


def get_storage_backend() -> str:
    """Return active storage backend name."""
    return _resolve_storage_config().backend


def get_storage_path(collection_id: str, record_id: str) -> Path:
    """Return local storage directory path for a record."""
    config = _resolve_storage_config()
    return Path(config.data_dir) / "storage" / collection_id / record_id


def _sanitize_stem(original_name: str) -> str:
    stem = Path(original_name).stem.strip()
    if not stem:
        return "file"
    stem = stem.replace(" ", "_")
    stem = _SAFE_STEM_PATTERN.sub("_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    return stem or "file"


def _random_suffix(length: int = 10) -> str:
    return "".join(secrets.choice(_ALPHANUM) for _ in range(length))


def _generate_storage_filename(original_name: str) -> str:
    ext = Path(original_name).suffix
    stem = _sanitize_stem(original_name)
    return f"{stem}_{_random_suffix()}{ext}"


def _storage_object_key(collection_id: str, record_id: str, filename: str) -> str:
    clean_name = Path(str(filename)).name
    return f"{collection_id}/{record_id}/{clean_name}"


def _get_s3_client(config: _StorageConfig) -> Any:
    global _s3_client_cache
    global _s3_client_cache_key

    cache_key = (
        config.s3_endpoint,
        config.s3_region,
        config.s3_access_key,
        config.s3_secret_key,
        config.s3_force_path_style,
    )

    with _runtime_lock:
        if _s3_client_cache is not None and _s3_client_cache_key == cache_key:
            return _s3_client_cache

    try:
        import boto3
    except Exception as exc:
        raise RuntimeError(
            "S3 storage backend requires boto3. Install it with: pip install boto3"
        ) from exc

    client_kwargs: dict[str, Any] = {
        "aws_access_key_id": config.s3_access_key,
        "aws_secret_access_key": config.s3_secret_key,
    }
    if config.s3_region:
        client_kwargs["region_name"] = config.s3_region
    if config.s3_endpoint:
        client_kwargs["endpoint_url"] = config.s3_endpoint
    if config.s3_force_path_style:
        try:
            from botocore.config import Config as BotocoreConfig

            client_kwargs["config"] = BotocoreConfig(
                s3={"addressing_style": "path"},
            )
        except Exception:
            pass

    client = boto3.client("s3", **client_kwargs)
    with _runtime_lock:
        _s3_client_cache = client
        _s3_client_cache_key = cache_key
    return client


def _save_s3_files(
    config: _StorageConfig,
    collection_id: str,
    record_id: str,
    files: list[tuple[str, bytes]],
    max_select: int,
) -> list[str]:
    client = _get_s3_client(config)
    saved: list[str] = []

    for original_name, content in files:
        unique_name = _generate_storage_filename(original_name)
        object_key = _storage_object_key(collection_id, record_id, unique_name)
        client.put_object(Bucket=config.s3_bucket, Key=object_key, Body=content)
        saved.append(unique_name)
        if max_select == 1:
            break

    return saved


def _delete_s3_files(
    config: _StorageConfig,
    collection_id: str,
    record_id: str,
    filenames: list[str],
) -> None:
    client = _get_s3_client(config)
    for filename in filenames:
        object_key = _storage_object_key(collection_id, record_id, filename)
        try:
            client.delete_object(Bucket=config.s3_bucket, Key=object_key)
        except Exception:
            continue


def _delete_all_s3_files(config: _StorageConfig, collection_id: str, record_id: str) -> None:
    client = _get_s3_client(config)
    prefix = f"{collection_id}/{record_id}/"
    continuation_token: str | None = None

    while True:
        params: dict[str, Any] = {
            "Bucket": config.s3_bucket,
            "Prefix": prefix,
        }
        if continuation_token:
            params["ContinuationToken"] = continuation_token
        response = client.list_objects_v2(**params)
        contents = response.get("Contents") or []
        keys = [{"Key": str(item.get("Key", ""))} for item in contents if item.get("Key")]
        if keys:
            try:
                client.delete_objects(
                    Bucket=config.s3_bucket,
                    Delete={"Objects": keys, "Quiet": True},
                )
            except Exception:
                for key in keys:
                    try:
                        client.delete_object(Bucket=config.s3_bucket, Key=key["Key"])
                    except Exception:
                        continue
        if not response.get("IsTruncated"):
            break
        continuation_token = str(response.get("NextContinuationToken", "") or "") or None


def read_file_bytes(collection_id: str, record_id: str, filename: str) -> bytes | None:
    """Read stored file bytes from active backend."""
    config = _resolve_storage_config()
    if config.backend == "s3":
        client = _get_s3_client(config)
        object_key = _storage_object_key(collection_id, record_id, filename)
        try:
            response = client.get_object(Bucket=config.s3_bucket, Key=object_key)
        except Exception:
            return None

        body = response.get("Body")
        if body is None:
            return None
        try:
            data = body.read()
            return bytes(data)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    file_path = get_storage_path(collection_id, record_id) / filename
    if not file_path.is_file():
        return None
    return file_path.read_bytes()


def save_files(
    collection_id: str,
    record_id: str,
    field_name: str,
    files: list[tuple[str, bytes]],
    max_select: int = 1,
) -> list[str]:
    """Save uploaded files and return stored filenames."""
    _ = field_name
    if not files:
        return []

    config = _resolve_storage_config()
    if config.backend == "s3":
        return _save_s3_files(config, collection_id, record_id, files, max_select)

    storage_dir = get_storage_path(collection_id, record_id)
    storage_dir.mkdir(parents=True, exist_ok=True)

    saved: list[str] = []
    for original_name, content in files:
        unique_name = ""
        file_path = storage_dir
        for _attempt in range(20):
            unique_name = _generate_storage_filename(original_name)
            file_path = storage_dir / unique_name
            if not file_path.exists():
                break
        else:
            raise RuntimeError("Unable to generate a unique filename for uploaded file")

        file_path.write_bytes(content)
        saved.append(unique_name)
        if max_select == 1:
            break

    return saved


def delete_files(collection_id: str, record_id: str, filenames: list[str]) -> None:
    """Delete specific files from active backend."""
    if not filenames:
        return

    config = _resolve_storage_config()
    if config.backend == "s3":
        _delete_s3_files(config, collection_id, record_id, filenames)
        return

    storage_dir = get_storage_path(collection_id, record_id)
    for filename in filenames:
        file_path = storage_dir / filename
        if file_path.is_file():
            file_path.unlink()

    if storage_dir.is_dir() and not any(storage_dir.iterdir()):
        storage_dir.rmdir()


def delete_all_files(collection_id: str, record_id: str) -> None:
    """Delete all files of a record from active backend."""
    config = _resolve_storage_config()
    if config.backend == "s3":
        _delete_all_s3_files(config, collection_id, record_id)
        return

    storage_dir = get_storage_path(collection_id, record_id)
    if storage_dir.is_dir():
        shutil.rmtree(storage_dir)
