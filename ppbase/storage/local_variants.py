"""Anchored reads of PocketBase thumbnails and ``.attrs`` side-cars.

A local PocketBase storage keeps, next to each record file, a ``fileblob``
side-car ``<file>.attrs`` and the generated thumbnails
``thumbs_<file>/<size>_<file>`` (with their own side-cars).  These helpers
open them with the same descriptor chain as ``ppbase.services.file_storage``
(validated single-component names, no symlink ever followed, identity
checks between ``lstat`` and ``open``); they add no path logic of their own.
"""

from __future__ import annotations

import os

from ppbase.core.storage_safety import StorageSafetyError, validate_file_reference
from ppbase.services.file_storage import (
    StorageFileStream,
    _get_storage_root,
    _open_directory_at,
    _open_record_directory,
    _open_regular_file_at,
    _StorageConfig,
)
from ppbase.storage.serving import ATTRS_SUFFIX

#: A PocketBase side-car is a few hundred bytes (the original file name is
#: capped at 255 characters, v0.22.34 ``filesystem.go`` l.203-208).
ATTRS_MAX_BYTES = 64 * 1024


class OversizedFileAttributes(ValueError):
    """A side-car larger than :data:`ATTRS_MAX_BYTES`."""


def open_local_stream(
    config: _StorageConfig,
    collection_id: str,
    record_id: str,
    filename: str,
    *,
    directory_name: str | None = None,
) -> StorageFileStream | None:
    """Open ``[directory_name/]filename`` below one record directory.

    Returns ``None`` when the record directory, the optional variant
    directory or the regular file does not exist.
    """
    if config.backend != "local":
        raise RuntimeError("Local storage reads require the local backend.")
    safe_filename = validate_file_reference(filename)
    safe_directory = (
        validate_file_reference(directory_name) if directory_name is not None else None
    )
    with _open_record_directory(
        config,
        collection_id,
        record_id,
        create=False,
    ) as opened:
        if opened is None:
            return None
        record_path = (
            _get_storage_root(config) / opened.collection_name / opened.record_name
        )
        if safe_directory is None:
            return _open_regular_file_at(
                opened.record_fd,
                safe_filename,
                config=config,
                storage_path=record_path / safe_filename,
            )
        variant_fd = _open_directory_at(
            opened.record_fd,
            safe_directory,
            create=False,
        )
        if variant_fd is None:
            return None
        try:
            return _open_regular_file_at(
                variant_fd,
                safe_filename,
                config=config,
                storage_path=record_path / safe_directory / safe_filename,
            )
        finally:
            os.close(variant_fd)


def read_local_attrs(
    config: _StorageConfig,
    collection_id: str,
    record_id: str,
    filename: str,
    *,
    directory_name: str | None = None,
) -> bytes | None:
    """Return the raw ``<filename>.attrs`` side-car, ``None`` if absent.

    A side-car name that is not a valid single component (a stored name at
    the length limit) cannot exist in a PocketBase storage: it is absent.
    """
    try:
        sidecar = validate_file_reference(f"{filename}{ATTRS_SUFFIX}")
    except StorageSafetyError:
        return None
    opened = open_local_stream(
        config,
        collection_id,
        record_id,
        sidecar,
        directory_name=directory_name,
    )
    if opened is None:
        return None
    try:
        payload = opened.stream.read(ATTRS_MAX_BYTES + 1)
    finally:
        opened.close()
    if len(payload) > ATTRS_MAX_BYTES:
        raise OversizedFileAttributes("The .attrs side-car is too large.")
    return bytes(payload)
