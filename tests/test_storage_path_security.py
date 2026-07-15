from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ppbase.config import Settings
from ppbase.api import records as records_api
from ppbase.api.records import _cleanup_created_record_files
from ppbase.core.storage_safety import (
    StorageSafetyError,
    validate_collection_id,
    validate_file_reference,
    validate_record_id,
)
from ppbase.services import file_storage


VALID_COLLECTION_ID = "_pbc_2287844090"
VALID_RECORD_ID = "kfzjt5oy8r34hvn"
VALID_FILENAME = "report_52iWbGinWd.txt"


def _s3_config() -> file_storage._StorageConfig:
    return file_storage._StorageConfig(
        data_dir="unused",
        backend="s3",
        s3_endpoint="https://storage.invalid",
        s3_bucket="bucket",
        s3_region="auto",
        s3_access_key="access",
        s3_secret_key="secret",
        s3_force_path_style=True,
    )


@pytest.fixture
def local_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    monkeypatch.setattr(file_storage, "_s3_client_cache", None)
    monkeypatch.setattr(file_storage, "_s3_client_cache_key", None)
    return data_dir


@pytest.mark.parametrize(
    "value",
    [
        "ezmtyl1cnyy58wz",
        "_pb_users_auth_",
        "pbc_184785686",
        "ab:cd'ef12",
        "a:b",
    ],
)
def test_valid_storage_identifiers_remain_compatible(value: str) -> None:
    assert validate_collection_id(value) == value
    assert validate_record_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "../escape",
        "..\\escape",
        "nested/value",
        "nested\\value",
        "/absolute",
        "C:\\absolute",
        "a\x00b",
        " leading",
        "trailing ",
        "abcdefghijklmnop",
    ],
)
def test_storage_identifiers_reject_unsafe_components(value: str) -> None:
    with pytest.raises(StorageSafetyError):
        validate_collection_id(value)
    with pytest.raises(StorageSafetyError):
        validate_record_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "../secret.txt",
        "..\\secret.txt",
        "/absolute.txt",
        "C:\\absolute.txt",
        "nested/file.txt",
        "nested\\file.txt",
        "https://example.com/avatar.png",
        "bad\x00name.txt",
    ],
)
def test_file_references_reject_paths(value: str) -> None:
    with pytest.raises(StorageSafetyError):
        validate_file_reference(value)


def test_valid_local_storage_round_trip_stays_below_root(local_data_dir: Path) -> None:
    saved = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("report.txt", b"safe payload")],
    )

    assert len(saved) == 1
    filename = saved[0]
    file_path = file_storage.get_storage_file_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        filename,
    )
    storage_root = (local_data_dir / "storage").resolve()
    file_path.relative_to(storage_root)
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        filename,
    ) == b"safe payload"

    file_storage.delete_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        [filename],
    )
    assert not file_path.exists()


def test_local_storage_ids_are_case_and_normalization_distinct(
    local_data_dir: Path,
) -> None:
    upper_collection = "CaseCollection"
    lower_collection = "casecollection"
    upper_record = "RecordA"
    lower_record = "recorda"

    upper_name = file_storage.save_files(
        upper_collection,
        upper_record,
        "document",
        [("upper.txt", b"upper")],
    )[0]
    lower_name = file_storage.save_files(
        lower_collection,
        lower_record,
        "document",
        [("lower.txt", b"lower")],
    )[0]

    upper_path = file_storage.get_storage_path(upper_collection, upper_record)
    lower_path = file_storage.get_storage_path(lower_collection, lower_record)
    assert upper_path != lower_path
    assert upper_path.name.startswith(file_storage._LOCAL_ID_ESCAPE_PREFIX)
    assert lower_path.name == lower_record
    assert lower_path.parent.name == lower_collection

    file_storage.delete_all_files(upper_collection, upper_record)
    assert file_storage.read_file_bytes(
        lower_collection,
        lower_record,
        lower_name,
    ) == b"lower"
    assert file_storage.read_file_bytes(
        upper_collection,
        upper_record,
        upper_name,
    ) is None

    composed_record = "récord"
    decomposed_record = "re\u0301cord"
    composed_name = file_storage.save_files(
        VALID_COLLECTION_ID,
        composed_record,
        "document",
        [("composed.txt", b"composed")],
    )[0]
    decomposed_name = file_storage.save_files(
        VALID_COLLECTION_ID,
        decomposed_record,
        "document",
        [("decomposed.txt", b"decomposed")],
    )[0]
    assert file_storage.get_storage_path(
        VALID_COLLECTION_ID, composed_record
    ) != file_storage.get_storage_path(VALID_COLLECTION_ID, decomposed_record)
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID, composed_record, composed_name
    ) == b"composed"
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID, decomposed_record, decomposed_name
    ) == b"decomposed"

    escaped_record = "A"
    lookalike_record = "~41"
    escaped_name = file_storage.save_files(
        VALID_COLLECTION_ID,
        escaped_record,
        "document",
        [("escaped.txt", b"escaped")],
    )[0]
    lookalike_name = file_storage.save_files(
        VALID_COLLECTION_ID,
        lookalike_record,
        "document",
        [("lookalike.txt", b"lookalike")],
    )[0]
    assert file_storage.get_storage_path(
        VALID_COLLECTION_ID, escaped_record
    ) != file_storage.get_storage_path(VALID_COLLECTION_ID, lookalike_record)
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID, escaped_record, escaped_name
    ) == b"escaped"
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID, lookalike_record, lookalike_name
    ) == b"lookalike"


def test_custom_lowercase_ppbase_id_uses_injective_local_layout(
    local_data_dir: Path,
) -> None:
    custom_id = "ab:cd'ef12"
    physical_name = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        custom_id,
    ).name
    assert physical_name.startswith(file_storage._LOCAL_ID_ESCAPE_PREFIX)
    assert not physical_name.startswith(".")
    assert custom_id not in physical_name


def test_legacy_fallback_is_discovered_after_an_initial_miss(
    local_data_dir: Path,
) -> None:
    custom_id = "ab:cd'ef12"
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        custom_id,
        VALID_FILENAME,
    ) is None

    legacy_dir = local_data_dir / "storage" / VALID_COLLECTION_ID / custom_id
    legacy_dir.mkdir(parents=True)
    (legacy_dir / VALID_FILENAME).write_bytes(b"restored legacy file")

    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        custom_id,
        VALID_FILENAME,
    ) == b"restored legacy file"


def test_case_insensitive_legacy_alias_is_not_opened_as_lowercase_id(
    local_data_dir: Path,
) -> None:
    storage_root = local_data_dir / "storage"
    uppercase_dir = storage_root / "ABC" / VALID_RECORD_ID
    uppercase_dir.mkdir(parents=True)
    sentinel = uppercase_dir / VALID_FILENAME
    sentinel.write_bytes(b"uppercase sentinel")
    if not (storage_root / "abc").exists():
        pytest.skip("Filesystem is case-sensitive")

    assert file_storage.read_file_bytes(
        "abc",
        VALID_RECORD_ID,
        VALID_FILENAME,
    ) is None
    with pytest.raises(StorageSafetyError):
        file_storage.get_storage_path("abc", VALID_RECORD_ID)
    with pytest.raises(StorageSafetyError):
        file_storage.save_files(
            "abc",
            VALID_RECORD_ID,
            "document",
            [("new.txt", b"must not alias")],
        )
    assert sentinel.read_bytes() == b"uppercase sentinel"


def test_cached_legacy_fallback_revalidates_exact_spelling_after_rename(
    local_data_dir: Path,
) -> None:
    uppercase_id = "RecordA"
    lowercase_id = "recorda"
    legacy_dir = (
        local_data_dir
        / "storage"
        / VALID_COLLECTION_ID
        / uppercase_id
    )
    legacy_dir.mkdir(parents=True)
    (legacy_dir / VALID_FILENAME).write_bytes(b"legacy inode")

    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        uppercase_id,
        VALID_FILENAME,
    ) == b"legacy inode"

    lowercase_dir = legacy_dir.with_name(lowercase_id)
    legacy_dir.rename(lowercase_dir)

    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        uppercase_id,
        VALID_FILENAME,
    ) is None
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        lowercase_id,
        VALID_FILENAME,
    ) == b"legacy inode"


def test_local_file_lookup_requires_exact_entry_spelling(
    local_data_dir: Path,
) -> None:
    storage_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    storage_dir.mkdir(parents=True)
    exact_name = "CaseFile.txt"
    wrong_case = "casefile.txt"
    exact_path = storage_dir / exact_name
    exact_path.write_bytes(b"keep exact")

    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        wrong_case,
    ) is None
    file_storage.delete_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        [wrong_case],
    )
    assert exact_path.read_bytes() == b"keep exact"


def test_storage_validators_reject_lone_surrogates() -> None:
    surrogate = chr(0xD800)
    with pytest.raises(StorageSafetyError):
        validate_collection_id(surrogate)
    with pytest.raises(StorageSafetyError):
        validate_record_id(surrogate)
    with pytest.raises(StorageSafetyError):
        validate_file_reference(f"bad{surrogate}.txt")


def test_valid_ppbase_custom_id_round_trips_without_normalization(
    local_data_dir: Path,
) -> None:
    custom_record_id = "ab:cd'ef12"
    filename = file_storage.save_files(
        VALID_COLLECTION_ID,
        custom_record_id,
        "document",
        [("report.txt", b"custom id")],
    )[0]

    assert file_storage.get_storage_file_path(
        VALID_COLLECTION_ID,
        custom_record_id,
        filename,
    ).is_file()
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        custom_record_id,
        filename,
    ) == b"custom id"


def test_delete_all_rejects_traversal_and_preserves_sentinel(
    local_data_dir: Path,
) -> None:
    sentinel = local_data_dir / "sentinel"
    sentinel.mkdir(parents=True)
    marker = sentinel / "marker.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(StorageSafetyError):
        file_storage.delete_all_files(VALID_COLLECTION_ID, "../../sentinel")

    assert sentinel.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_batch_cleanup_skips_unsafe_target_and_preserves_sentinel(
    local_data_dir: Path,
) -> None:
    sentinel = local_data_dir / "sentinel"
    sentinel.mkdir(parents=True)
    marker = sentinel / "marker.txt"
    marker.write_text("keep", encoding="utf-8")

    _cleanup_created_record_files(
        {(VALID_COLLECTION_ID, "../../sentinel", VALID_FILENAME)}
    )

    assert sentinel.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_delete_file_rejects_absolute_reference_and_preserves_sentinel(
    local_data_dir: Path,
) -> None:
    sentinel = local_data_dir / "sentinel.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(StorageSafetyError):
        file_storage.delete_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            [str(sentinel.resolve())],
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_file_symlink_is_rejected_for_read_and_unlink(
    local_data_dir: Path,
) -> None:
    sentinel = local_data_dir / "outside.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("keep", encoding="utf-8")
    storage_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    storage_dir.mkdir(parents=True)
    linked_file = storage_dir / VALID_FILENAME
    try:
        linked_file.symlink_to(sentinel)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    with pytest.raises(StorageSafetyError):
        file_storage.read_file_bytes(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            VALID_FILENAME,
        )
    with pytest.raises(StorageSafetyError):
        file_storage.delete_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            [VALID_FILENAME],
        )

    assert linked_file.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_save_rejects_symlinked_collection_directory(
    local_data_dir: Path,
) -> None:
    outside = local_data_dir / "outside_collection"
    outside.mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    storage_root = local_data_dir / "storage"
    storage_root.mkdir(parents=True)
    collection_link = storage_root / VALID_COLLECTION_ID
    try:
        collection_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    with pytest.raises(StorageSafetyError):
        file_storage.save_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            "document",
            [("report.txt", b"must not be written")],
        )

    assert collection_link.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in outside.iterdir()) == ["marker.txt"]


def test_rmtree_rejects_nested_symlink_and_preserves_outside_tree(
    local_data_dir: Path,
) -> None:
    outside = local_data_dir / "outside"
    outside.mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    storage_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    storage_dir.mkdir(parents=True)
    linked_dir = storage_dir / "linked"
    try:
        linked_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    with pytest.raises(StorageSafetyError):
        file_storage.delete_all_files(VALID_COLLECTION_ID, VALID_RECORD_ID)

    assert storage_dir.is_dir()
    assert linked_dir.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_save_rejects_synchronized_record_directory_symlink_swap(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = local_data_dir / "outside_record"
    outside.mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    collection_dir = local_data_dir / "storage" / VALID_COLLECTION_ID
    collection_dir.mkdir(parents=True)

    ready = threading.Event()
    proceed = threading.Event()
    original_open_directory_at = file_storage._open_directory_at
    armed = True
    physical_record_name = file_storage._local_storage_id_name(VALID_RECORD_ID)

    def _controlled_open_directory_at(
        parent_fd: int,
        name: str,
        *,
        create: bool,
        mode: int = 0o700,
    ) -> int | None:
        nonlocal armed
        if armed and create and name == physical_record_name:
            armed = False
            ready.set()
            assert proceed.wait(timeout=5)
        return original_open_directory_at(
            parent_fd,
            name,
            create=create,
            mode=mode,
        )

    monkeypatch.setattr(
        file_storage,
        "_open_directory_at",
        _controlled_open_directory_at,
    )
    errors: list[BaseException] = []

    def _save() -> None:
        try:
            file_storage.save_files(
                VALID_COLLECTION_ID,
                VALID_RECORD_ID,
                "document",
                [("report.txt", b"must stay confined")],
            )
        except BaseException as exc:  # captured for the coordinating thread
            errors.append(exc)

    worker = threading.Thread(target=_save)
    worker.start()
    try:
        assert ready.wait(timeout=5)
        record_link = collection_dir / physical_record_name
        try:
            record_link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Symlinks are unavailable: {exc}")
    finally:
        proceed.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], StorageSafetyError)
    assert marker.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in outside.iterdir()) == ["marker.txt"]


def test_read_rejects_synchronized_leaf_symlink_swap(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("report.txt", b"original")],
    )
    filename = saved[0]
    file_path = file_storage.get_storage_file_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        filename,
    )
    sentinel = local_data_dir / "outside.txt"
    sentinel.write_text("secret", encoding="utf-8")

    ready = threading.Event()
    proceed = threading.Event()
    original_open_regular_file_at = file_storage._open_regular_file_at
    armed = True

    def _controlled_open_regular_file_at(
        directory_fd: int,
        requested_filename: str,
        **kwargs: object,
    ):
        nonlocal armed
        if armed and requested_filename == filename:
            armed = False
            ready.set()
            assert proceed.wait(timeout=5)
        return original_open_regular_file_at(
            directory_fd,
            requested_filename,
            **kwargs,
        )

    monkeypatch.setattr(
        file_storage,
        "_open_regular_file_at",
        _controlled_open_regular_file_at,
    )
    errors: list[BaseException] = []

    def _read() -> None:
        try:
            file_storage.read_file_bytes(
                VALID_COLLECTION_ID,
                VALID_RECORD_ID,
                filename,
            )
        except BaseException as exc:  # captured for the coordinating thread
            errors.append(exc)

    worker = threading.Thread(target=_read)
    worker.start()
    try:
        assert ready.wait(timeout=5)
        file_path.unlink()
        try:
            file_path.symlink_to(sentinel)
        except OSError as exc:
            pytest.skip(f"Symlinks are unavailable: {exc}")
    finally:
        proceed.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], StorageSafetyError)
    assert sentinel.read_text(encoding="utf-8") == "secret"


def test_unlink_rejects_synchronized_leaf_symlink_swap(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("report.txt", b"original")],
    )[0]
    file_path = file_storage.get_storage_file_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        filename,
    )
    sentinel = local_data_dir / "outside_unlink.txt"
    sentinel.write_text("keep", encoding="utf-8")
    ready = threading.Event()
    proceed = threading.Event()
    original_unlink_regular_file_at = file_storage._unlink_regular_file_at
    armed = True

    def _controlled_unlink_regular_file_at(
        directory_fd: int,
        requested_filename: str,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> bool:
        nonlocal armed
        if armed and requested_filename == filename:
            armed = False
            ready.set()
            assert proceed.wait(timeout=5)
        return original_unlink_regular_file_at(
            directory_fd,
            requested_filename,
            expected_identity=expected_identity,
        )

    monkeypatch.setattr(
        file_storage,
        "_unlink_regular_file_at",
        _controlled_unlink_regular_file_at,
    )
    errors: list[BaseException] = []

    def _delete() -> None:
        try:
            file_storage.delete_files(
                VALID_COLLECTION_ID,
                VALID_RECORD_ID,
                [filename],
            )
        except BaseException as exc:  # captured for the coordinating thread
            errors.append(exc)

    worker = threading.Thread(target=_delete)
    worker.start()
    try:
        assert ready.wait(timeout=5)
        file_path.unlink()
        try:
            file_path.symlink_to(sentinel)
        except OSError as exc:
            pytest.skip(f"Symlinks are unavailable: {exc}")
    finally:
        proceed.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], StorageSafetyError)
    assert file_path.is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_unlink_stays_on_open_record_fd_after_directory_symlink_swap(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("report.txt", b"delete only this inode")],
    )[0]
    record_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    detached_dir = record_dir.with_name("detached_record")
    outside = local_data_dir / "outside_record_unlink"
    outside.mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    ready = threading.Event()
    proceed = threading.Event()
    original_unlink_regular_file_at = file_storage._unlink_regular_file_at
    armed = True

    def _controlled_unlink_regular_file_at(
        directory_fd: int,
        requested_filename: str,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> bool:
        nonlocal armed
        if armed and requested_filename == filename:
            armed = False
            ready.set()
            assert proceed.wait(timeout=5)
        return original_unlink_regular_file_at(
            directory_fd,
            requested_filename,
            expected_identity=expected_identity,
        )

    monkeypatch.setattr(
        file_storage,
        "_unlink_regular_file_at",
        _controlled_unlink_regular_file_at,
    )
    errors: list[BaseException] = []

    def _delete() -> None:
        try:
            file_storage.delete_files(
                VALID_COLLECTION_ID,
                VALID_RECORD_ID,
                [filename],
            )
        except BaseException as exc:  # captured for the coordinating thread
            errors.append(exc)

    worker = threading.Thread(target=_delete)
    worker.start()
    try:
        assert ready.wait(timeout=5)
        record_dir.rename(detached_dir)
        try:
            record_dir.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Symlinks are unavailable: {exc}")
    finally:
        proceed.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], StorageSafetyError)
    assert record_dir.is_symlink()
    assert detached_dir.is_dir()
    assert list(detached_dir.iterdir()) == []
    assert marker.read_text(encoding="utf-8") == "keep"


def test_rmtree_rejects_symlink_inserted_between_validation_and_removal(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    nested = storage_dir / "nested"
    nested.mkdir(parents=True)
    nested_file = nested / "inside.txt"
    nested_file.write_text("inside", encoding="utf-8")
    outside = local_data_dir / "outside_rmtree"
    outside.mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")

    ready = threading.Event()
    proceed = threading.Event()
    original_remove_tree_contents_at = file_storage._remove_tree_contents_at
    armed = True

    def _controlled_remove_tree_contents_at(directory_fd: int) -> None:
        nonlocal armed
        if armed:
            armed = False
            ready.set()
            assert proceed.wait(timeout=5)
        original_remove_tree_contents_at(directory_fd)

    monkeypatch.setattr(
        file_storage,
        "_remove_tree_contents_at",
        _controlled_remove_tree_contents_at,
    )
    errors: list[BaseException] = []

    def _delete_all() -> None:
        try:
            file_storage.delete_all_files(
                VALID_COLLECTION_ID,
                VALID_RECORD_ID,
            )
        except BaseException as exc:  # captured for the coordinating thread
            errors.append(exc)

    worker = threading.Thread(target=_delete_all)
    worker.start()
    try:
        assert ready.wait(timeout=5)
        nested_file.unlink()
        nested.rmdir()
        try:
            nested.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Symlinks are unavailable: {exc}")
    finally:
        proceed.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], StorageSafetyError)
    assert nested.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_rmtree_stays_on_open_record_fd_after_directory_symlink_swap(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    nested = record_dir / "nested"
    nested.mkdir(parents=True)
    (nested / "inside.txt").write_text("inside", encoding="utf-8")
    detached_dir = record_dir.with_name("detached_tree")
    outside = local_data_dir / "outside_record_rmtree"
    outside.mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    ready = threading.Event()
    proceed = threading.Event()
    original_remove_tree_contents_at = file_storage._remove_tree_contents_at
    armed = True

    def _controlled_remove_tree_contents_at(directory_fd: int) -> None:
        nonlocal armed
        if armed:
            armed = False
            ready.set()
            assert proceed.wait(timeout=5)
        original_remove_tree_contents_at(directory_fd)

    monkeypatch.setattr(
        file_storage,
        "_remove_tree_contents_at",
        _controlled_remove_tree_contents_at,
    )
    errors: list[BaseException] = []

    def _delete_all() -> None:
        try:
            file_storage.delete_all_files(
                VALID_COLLECTION_ID,
                VALID_RECORD_ID,
            )
        except BaseException as exc:  # captured for the coordinating thread
            errors.append(exc)

    worker = threading.Thread(target=_delete_all)
    worker.start()
    try:
        assert ready.wait(timeout=5)
        record_dir.rename(detached_dir)
        try:
            record_dir.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"Symlinks are unavailable: {exc}")
    finally:
        proceed.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], StorageSafetyError)
    assert record_dir.is_symlink()
    assert detached_dir.is_dir()
    assert list(detached_dir.iterdir()) == []
    assert marker.read_text(encoding="utf-8") == "keep"


def test_local_storage_fails_closed_without_descriptor_capabilities(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_storage.os, "supports_dir_fd", frozenset())

    with pytest.raises(StorageSafetyError, match="directory-descriptor"):
        file_storage.save_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            "document",
            [("report.txt", b"payload")],
        )
    with pytest.raises(StorageSafetyError, match="directory-descriptor"):
        file_storage.get_storage_path(VALID_COLLECTION_ID, VALID_RECORD_ID)

    assert not local_data_dir.exists()


def test_s3_remains_available_without_local_descriptor_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Body:
        def read(self) -> bytes:
            return b"s3 payload"

        def close(self) -> None:
            return None

    class _Client:
        def get_object(self, **_kwargs: object) -> dict[str, object]:
            return {"Body": _Body()}

    monkeypatch.setattr(file_storage.os, "supports_dir_fd", frozenset())
    monkeypatch.setattr(file_storage, "_resolve_storage_config", _s3_config)
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _config: _Client())

    assert (
        file_storage.read_file_bytes(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            VALID_FILENAME,
        )
        == b"s3 payload"
    )


def test_local_save_failure_removes_only_files_from_that_call(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preexisting = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("existing.txt", b"keep")],
    )[0]
    original_write = file_storage._write_file_exclusive_at
    call_count = 0

    def _fail_second_write(
        directory_fd: int,
        filename: str,
        content: bytes,
    ) -> tuple[int, int]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated write failure")
        return original_write(directory_fd, filename, content)

    monkeypatch.setattr(
        file_storage,
        "_write_file_exclusive_at",
        _fail_second_write,
    )

    with pytest.raises(RuntimeError, match="simulated write failure"):
        file_storage.save_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            "document",
            [("first.txt", b"first"), ("second.txt", b"second")],
            max_select=2,
        )

    storage_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    assert sorted(path.name for path in storage_dir.iterdir()) == [preexisting]
    assert (storage_dir / preexisting).read_bytes() == b"keep"


def test_s3_save_failure_removes_every_acknowledged_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.put_count = 0

        def put_object(  # noqa: N803
            self,
            *,
            Bucket: str,
            Key: str,
            Body: bytes,
            IfNoneMatch: str,
        ) -> None:
            _ = Bucket
            assert IfNoneMatch == "*"
            self.put_count += 1
            if self.put_count == 2:
                raise RuntimeError("simulated S3 failure")
            self.objects[Key] = bytes(Body)

        def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
            _ = Bucket
            self.objects.pop(Key, None)

    client = _Client()
    monkeypatch.setattr(file_storage, "_resolve_storage_config", _s3_config)
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _config: client)

    with pytest.raises(
        file_storage.StorageObjectWriteError,
        match="ambiguous object outcome",
    ):
        file_storage.save_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            "document",
            [("first.txt", b"first"), ("second.txt", b"second")],
            max_select=2,
        )

    assert client.objects == {}


def test_s3_persist_then_transport_error_preserves_ambiguous_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.delete_calls: list[str] = []

        def put_object(  # noqa: N803
            self,
            *,
            Bucket: str,
            Key: str,
            Body: bytes,
            IfNoneMatch: str,
        ) -> None:
            _ = Bucket
            assert IfNoneMatch == "*"
            self.objects[Key] = bytes(Body)
            raise RuntimeError("transport failed after persistence")

        def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
            _ = Bucket
            self.delete_calls.append(Key)
            self.objects.pop(Key, None)

    client = _Client()
    tracked: set[tuple[str, str, str]] = set()
    monkeypatch.setattr(file_storage, "_resolve_storage_config", _s3_config)
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _config: client)

    with file_storage.capture_storage_writes(tracked):
        with pytest.raises(
            file_storage.StorageObjectWriteError,
            match="ambiguous object outcome",
        ) as caught:
            file_storage.save_files(
                VALID_COLLECTION_ID,
                VALID_RECORD_ID,
                "document",
                [("report.txt", b"persisted")],
            )

    assert tracked == set()
    assert caught.value.created_filenames == ()
    assert caught.value.cleanup_failed_filenames == ()
    assert len(caught.value.ambiguous_filenames) == 1
    ambiguous_filename = caught.value.ambiguous_filenames[0]
    object_key = f"{VALID_COLLECTION_ID}/{VALID_RECORD_ID}/{ambiguous_filename}"
    assert client.objects[object_key] == b"persisted"
    assert client.delete_calls == []


def test_s3_conditional_create_preserves_preexisting_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PreconditionFailed(Exception):
        response = {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        }

    class _Client:
        def __init__(self) -> None:
            self.objects = {
                f"{VALID_COLLECTION_ID}/{VALID_RECORD_ID}/collision.txt": b"sentinel"
            }

        def put_object(  # noqa: N803
            self,
            *,
            Bucket: str,
            Key: str,
            Body: bytes,
            IfNoneMatch: str,
        ) -> None:
            _ = Bucket
            assert IfNoneMatch == "*"
            if Key in self.objects:
                raise _PreconditionFailed()
            self.objects[Key] = bytes(Body)

        def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
            _ = Bucket
            self.objects.pop(Key, None)

    generated_names = iter(["collision.txt", "fresh.txt"])
    client = _Client()
    monkeypatch.setattr(file_storage, "_resolve_storage_config", _s3_config)
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _config: client)
    monkeypatch.setattr(
        file_storage,
        "_generate_storage_filename",
        lambda _original_name: next(generated_names),
    )

    saved = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("report.txt", b"fresh payload")],
    )

    assert saved == ["fresh.txt"]
    assert client.objects[
        f"{VALID_COLLECTION_ID}/{VALID_RECORD_ID}/collision.txt"
    ] == b"sentinel"
    assert client.objects[
        f"{VALID_COLLECTION_ID}/{VALID_RECORD_ID}/fresh.txt"
    ] == b"fresh payload"


def test_delete_files_skips_remote_references_but_validates_other_names(
    local_data_dir: Path,
) -> None:
    first, second = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("first.txt", b"first"), ("second.txt", b"second")],
        max_select=2,
    )

    file_storage.delete_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        ["https://example.com/avatar.png", first],
    )
    assert (
        file_storage.read_file_bytes(VALID_COLLECTION_ID, VALID_RECORD_ID, first)
        is None
    )
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        second,
    ) == b"second"

    with pytest.raises(StorageSafetyError):
        file_storage.delete_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            [second, "../unsafe.txt"],
        )
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        second,
    ) == b"second"


def test_s3_delete_skips_remote_reference_and_deletes_safe_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_object(self, *, Bucket: str, Key: str) -> None:  # noqa: N803
            _ = Bucket
            self.deleted.append(Key)

    client = _Client()
    monkeypatch.setattr(file_storage, "_resolve_storage_config", _s3_config)
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _config: client)

    file_storage.delete_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        ["https://example.com/avatar.png", VALID_FILENAME],
    )

    assert client.deleted == [
        f"{VALID_COLLECTION_ID}/{VALID_RECORD_ID}/{VALID_FILENAME}"
    ]

    with pytest.raises(StorageSafetyError):
        file_storage.delete_files(
            "../unsafe",
            VALID_RECORD_ID,
            ["https://example.com/avatar.png"],
        )


def test_s3_delete_failures_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        def delete_object(self, **_kwargs: object) -> None:
            raise RuntimeError("delete denied")

    monkeypatch.setattr(file_storage, "_resolve_storage_config", _s3_config)
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _config: _Client())

    with pytest.raises(OSError, match="Failed to delete 1 S3"):
        file_storage.delete_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            [VALID_FILENAME],
        )


def test_s3_stream_forwards_native_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Body:
        def read(self, _size: int = -1) -> bytes:
            return b"slice"

        def close(self) -> None:
            return None

    calls: list[dict[str, object]] = []

    class _Client:
        def get_object(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {
                "Body": _Body(),
                "ContentLength": 5,
                "ETag": '"v1"',
                "LastModified": datetime(
                    2026,
                    7,
                    15,
                    13,
                    0,
                    tzinfo=timezone(timedelta(hours=1)),
                ),
            }

    monkeypatch.setattr(file_storage, "_resolve_storage_config", _s3_config)
    monkeypatch.setattr(file_storage, "_get_s3_client", lambda _config: _Client())

    opened = file_storage.open_file_stream(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        VALID_FILENAME,
        byte_range=(10, 14),
        if_match='"v1"',
    )
    assert opened is not None
    try:
        assert opened.stream.read() == b"slice"
        assert opened.content_length == 5
        assert opened.etag == '"v1"'
        assert opened.last_modified == "Wed, 15 Jul 2026 12:00:00 GMT"
    finally:
        opened.close()
    assert calls == [
        {
            "Bucket": "bucket",
            "Key": f"{VALID_COLLECTION_ID}/{VALID_RECORD_ID}/{VALID_FILENAME}",
            "Range": "bytes=10-14",
            "IfMatch": '"v1"',
        }
    ]


def test_delete_storage_dir_if_empty_preserves_nonempty_record(
    local_data_dir: Path,
) -> None:
    empty_record_id = "empty_record"
    file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        empty_record_id,
    ).mkdir(parents=True)
    assert file_storage.delete_storage_dir_if_empty(
        VALID_COLLECTION_ID,
        empty_record_id,
    )

    filename = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("report.txt", b"keep")],
    )[0]

    assert not file_storage.delete_storage_dir_if_empty(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        filename,
    ) == b"keep"

    file_storage.delete_files(VALID_COLLECTION_ID, VALID_RECORD_ID, [filename])
    assert not file_storage.delete_storage_dir_if_empty(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )


def test_delete_rejects_record_directory_replacement_before_rmdir(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("report.txt", b"remove source only")],
    )[0]
    record_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    detached_dir = record_dir.with_name("detached_empty_record")
    ready = threading.Event()
    proceed = threading.Event()
    original_remove = file_storage._remove_record_directory_if_empty_at

    def _controlled_remove(
        collection_fd: int,
        record_name: str,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> bool:
        ready.set()
        assert proceed.wait(timeout=5)
        return original_remove(
            collection_fd,
            record_name,
            expected_identity=expected_identity,
        )

    monkeypatch.setattr(
        file_storage,
        "_remove_record_directory_if_empty_at",
        _controlled_remove,
    )
    errors: list[BaseException] = []

    def _delete() -> None:
        try:
            file_storage.delete_files(
                VALID_COLLECTION_ID,
                VALID_RECORD_ID,
                [filename],
            )
        except BaseException as exc:  # captured for the coordinating thread
            errors.append(exc)

    worker = threading.Thread(target=_delete)
    worker.start()
    try:
        assert ready.wait(timeout=5)
        record_dir.rename(detached_dir)
        record_dir.mkdir()
        marker = record_dir / "replacement.txt"
        marker.write_text("keep replacement", encoding="utf-8")
    finally:
        proceed.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], StorageSafetyError)
    assert detached_dir.is_dir()
    assert list(detached_dir.iterdir()) == []
    assert (record_dir / "replacement.txt").read_text(encoding="utf-8") == (
        "keep replacement"
    )


def test_local_variant_round_trip_is_anchored_and_atomic(
    local_data_dir: Path,
) -> None:
    source_name = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("image.png", b"source")],
    )[0]
    directory_name = f"thumbs_{source_name}"
    variant_name = f"100x100_{source_name}"

    file_storage.write_local_storage_variant_bytes(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        directory_name,
        variant_name,
        b"thumbnail",
    )

    assert file_storage.read_local_storage_variant_bytes(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        directory_name,
        variant_name,
    ) == b"thumbnail"
    assert not any(
        path.name.startswith(".__ppbase_")
        for path in (
            file_storage.get_storage_path(VALID_COLLECTION_ID, VALID_RECORD_ID)
            / directory_name
        ).iterdir()
    )


def test_local_variant_rejects_symlinked_directory_and_target(
    local_data_dir: Path,
) -> None:
    source_name = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("image.png", b"source")],
    )[0]
    storage_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    outside = local_data_dir / "outside_variants"
    outside.mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    linked_directory_name = f"thumbs_{source_name}"
    linked_directory = storage_dir / linked_directory_name
    try:
        linked_directory.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    with pytest.raises(StorageSafetyError):
        file_storage.write_local_storage_variant_bytes(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            linked_directory_name,
            f"100x100_{source_name}",
            b"must not escape",
        )
    assert marker.read_text(encoding="utf-8") == "keep"

    linked_directory.unlink()
    linked_directory.mkdir()
    target_name = f"100x100_{source_name}"
    target = linked_directory / target_name
    target.symlink_to(marker)
    with pytest.raises(StorageSafetyError):
        file_storage.write_local_storage_variant_bytes(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            linked_directory_name,
            target_name,
            b"must not overwrite sentinel",
        )
    assert target.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_delete_file_removes_thumbnail_variants(local_data_dir: Path) -> None:
    source_name = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("image.png", b"source")],
    )[0]
    directory_name = f"thumbs_{source_name}"
    variant_name = f"100x100_{source_name}"
    file_storage.write_local_storage_variant_bytes(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        directory_name,
        variant_name,
        b"thumbnail",
    )

    file_storage.delete_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        [source_name],
    )

    record_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    assert not record_dir.exists()


def test_delete_file_rejects_symlinked_thumbnail_directory_before_unlink(
    local_data_dir: Path,
) -> None:
    source_name = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("image.png", b"source")],
    )[0]
    record_dir = file_storage.get_storage_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
    )
    source_path = record_dir / source_name
    outside = local_data_dir / "outside_thumbnails"
    outside.mkdir(parents=True)
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    linked_variants = record_dir / f"thumbs_{source_name}"
    try:
        linked_variants.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    with pytest.raises(StorageSafetyError):
        file_storage.delete_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            [source_name],
        )

    assert source_path.read_bytes() == b"source"
    assert marker.read_text(encoding="utf-8") == "keep"


class _CommitAckLostEngine:
    @asynccontextmanager
    async def begin(self):
        try:
            yield object()
        except BaseException:
            raise
        else:
            raise RuntimeError("commit acknowledgement lost")


@pytest.mark.asyncio
@pytest.mark.parametrize("durably_referenced", [True, False])
async def test_ambiguous_commit_reconciles_new_file_references(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    durably_referenced: bool,
) -> None:
    written: list[str] = []

    async def _operation(_engine) -> dict[str, object]:
        written.extend(
            file_storage.save_files(
                VALID_COLLECTION_ID,
                VALID_RECORD_ID,
                "document",
                [("report.txt", b"commit outcome")],
            )
        )
        return {"ok": True}

    async def _get_all_collections(_engine) -> list[object]:
        return []

    async def _in_transaction_references(*_args, **_kwargs):
        return {
            (VALID_COLLECTION_ID, VALID_RECORD_ID, written[0])
        }

    async def _durable_references(_engine, _targets):
        if durably_referenced:
            return {
                (VALID_COLLECTION_ID, VALID_RECORD_ID, written[0])
            }
        return set()

    monkeypatch.setattr(
        records_api,
        "get_all_collections",
        _get_all_collections,
    )
    monkeypatch.setattr(
        records_api,
        "_final_batch_file_references",
        _in_transaction_references,
    )
    monkeypatch.setattr(
        records_api,
        "_reconcile_storage_file_references",
        _durable_references,
    )

    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await records_api._run_storage_transaction(
            _CommitAckLostEngine(),
            _operation,
        )

    file_path = file_storage.get_storage_file_path(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        written[0],
    )
    assert file_path.exists() is durably_referenced


@pytest.mark.asyncio
async def test_ambiguous_commit_preserves_deferred_deletes(
    local_data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = file_storage.save_files(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        "document",
        [("existing.txt", b"preserve on ambiguity")],
    )[0]

    async def _operation(_engine) -> dict[str, object]:
        file_storage.delete_files(
            VALID_COLLECTION_ID,
            VALID_RECORD_ID,
            [filename],
        )
        return {"ok": True}

    async def _get_all_collections(_engine) -> list[object]:
        return []

    async def _no_references(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(
        records_api,
        "get_all_collections",
        _get_all_collections,
    )
    monkeypatch.setattr(
        records_api,
        "_final_batch_file_references",
        _no_references,
    )
    monkeypatch.setattr(
        records_api,
        "_reconcile_storage_file_references",
        _no_references,
    )

    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        await records_api._run_storage_transaction(
            _CommitAckLostEngine(),
            _operation,
        )

    assert file_storage.read_file_bytes(
        VALID_COLLECTION_ID,
        VALID_RECORD_ID,
        filename,
    ) == b"preserve on ambiguity"
