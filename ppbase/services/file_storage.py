"""File storage service for saving and deleting record files.

Files are stored at: {data_dir}/storage/{collection_id}/{record_id}/{filename}
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from ppbase.config import Settings

# Cached settings instance
_settings: Settings | None = None


def _get_settings() -> Settings:
    """Get current settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def get_storage_path(collection_id: str, record_id: str) -> Path:
    """Get the storage directory path for a record."""
    settings = _get_settings()
    return Path(settings.data_dir) / "storage" / collection_id / record_id


def save_files(
    collection_id: str,
    record_id: str,
    field_name: str,
    files: list[tuple[str, bytes]],
    max_select: int = 1,
) -> list[str]:
    """Save uploaded files and return list of saved filenames.
    
    Args:
        collection_id: The collection ID
        record_id: The record ID
        field_name: The field name (for logging)
        files: List of (filename, content) tuples
        max_select: Max number of files allowed (1 = single file mode)
    
    Returns:
        List of saved filenames
    """
    storage_dir = get_storage_path(collection_id, record_id)
    storage_dir.mkdir(parents=True, exist_ok=True)
    
    saved: list[str] = []
    
    for original_name, content in files:
        # Generate unique filename to avoid conflicts
        ext = Path(original_name).suffix
        unique_name = f"{uuid.uuid4().hex[:12]}{ext}"
        
        file_path = storage_dir / unique_name
        file_path.write_bytes(content)
        saved.append(unique_name)
        
        # In single file mode, only save the first file
        if max_select == 1:
            break
    
    return saved


def delete_files(
    collection_id: str,
    record_id: str,
    filenames: list[str],
) -> None:
    """Delete specified files from storage.
    
    Args:
        collection_id: The collection ID
        record_id: The record ID
        filenames: List of filenames to delete
    """
    storage_dir = get_storage_path(collection_id, record_id)
    
    for filename in filenames:
        file_path = storage_dir / filename
        if file_path.is_file():
            file_path.unlink()
    
    # Remove directory if empty
    if storage_dir.is_dir() and not any(storage_dir.iterdir()):
        storage_dir.rmdir()


def delete_all_files(collection_id: str, record_id: str) -> None:
    """Delete all files for a record.
    
    Args:
        collection_id: The collection ID
        record_id: The record ID
    """
    import shutil
    storage_dir = get_storage_path(collection_id, record_id)
    if storage_dir.is_dir():
        shutil.rmtree(storage_dir)
