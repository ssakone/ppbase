from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ppbase.backup.models import BackupIntegrityError
from ppbase.backup.service import _require_copied_file_references
from ppbase.core.storage_safety import StorageSafetyError
from ppbase.services.file_references import (
    canonical_local_file_references_for_row,
)


def test_canonical_local_file_references_ignore_views_remote_values_and_duplicates(
) -> None:
    collection = {
        "id": "docs00000000001",
        "name": "documents",
        "type": "base",
        "schema": [
            {"name": "attachment", "type": "file"},
            {"name": "gallery", "type": "file"},
            {"name": "title", "type": "text"},
        ],
    }
    row = {
        "id": "rec000000000001",
        "attachment": "report.pdf",
        "gallery": [
            "report.pdf",
            "photo.png",
            "https://cdn.example.test/legacy.png",
        ],
    }

    assert canonical_local_file_references_for_row(collection, row) == (
        ("docs00000000001", "rec000000000001", "photo.png"),
        ("docs00000000001", "rec000000000001", "report.pdf"),
    )
    assert canonical_local_file_references_for_row(
        {**collection, "type": "view"},
        row,
    ) == ()


def test_canonical_local_file_references_reject_unsafe_local_names() -> None:
    collection = {
        "id": "docs00000000001",
        "name": "documents",
        "type": "base",
        "schema": [{"name": "attachment", "type": "file"}],
    }

    with pytest.raises(StorageSafetyError):
        canonical_local_file_references_for_row(
            collection,
            {"id": "rec000000000001", "attachment": "../escape"},
        )


def test_missing_storage_is_allowed_only_for_an_empty_reference_inventory(
    tmp_path: Path,
) -> None:
    storage_config = SimpleNamespace(
        data_dir=str(tmp_path / "data"),
        backend="local",
    )
    prepared = SimpleNamespace(resources=())

    _require_copied_file_references((), prepared, storage_config)

    with pytest.raises(BackupIntegrityError, match="missing"):
        _require_copied_file_references(
            (("docs00000000001", "rec000000000001", "report.pdf"),),
            prepared,
            storage_config,
        )
