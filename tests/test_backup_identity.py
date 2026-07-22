from __future__ import annotations

import os
import stat
import multiprocessing
from pathlib import Path
from queue import Empty
from typing import Any

import ppbase.backup.identity as identity_module
from ppbase.backup.identity import BackupIdentity


def _load_identity_in_process(
    control_dir: str,
    worker_started: Any,
    write_started: Any,
    release_write: Any,
    results: Any,
    *,
    block_write: bool,
) -> None:
    worker_started.set()
    if block_write:
        real_write_all = identity_module._write_all

        def blocked_write(descriptor: int, payload: bytes) -> None:
            write_started.set()
            if not release_write.wait(timeout=10):
                raise AssertionError("timed out releasing the identity writer")
            real_write_all(descriptor, payload)

        identity_module._write_all = blocked_write
    try:
        identity = BackupIdentity.load_or_create(control_dir)
    except BaseException as exc:
        results.put(("error", repr(exc)))
        raise
    results.put(("ok", identity.fingerprint_sha256))


def test_concurrent_first_identity_creation_is_atomic(
    tmp_path: Path,
) -> None:
    control_dir = tmp_path / "control"
    key_path = control_dir / identity_module.PRIVATE_KEY_FILENAME
    context = multiprocessing.get_context("spawn")
    first_started = context.Event()
    first_write_started = context.Event()
    second_started = context.Event()
    unused_write_started = context.Event()
    release_first_write = context.Event()
    results = context.Queue()
    first = context.Process(
        target=_load_identity_in_process,
        args=(
            str(control_dir),
            first_started,
            first_write_started,
            release_first_write,
            results,
        ),
        kwargs={"block_write": True},
    )
    second = context.Process(
        target=_load_identity_in_process,
        args=(
            str(control_dir),
            second_started,
            unused_write_started,
            release_first_write,
            results,
        ),
        kwargs={"block_write": False},
    )
    first.start()
    try:
        assert first_started.wait(timeout=5)
        assert first_write_started.wait(timeout=5)

        # No incomplete private-key bytes are ever visible at the final name.
        assert not key_path.exists()

        second.start()
        assert second_started.wait(timeout=5)
        second.join(timeout=0.2)
        assert second.is_alive()

        release_first_write.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert first.exitcode == 0
        assert second.exitcode == 0
    finally:
        release_first_write.set()
        for process in (first, second):
            if process.pid is not None and process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)

    outcomes = []
    for _ in range(2):
        try:
            outcomes.append(results.get(timeout=2))
        except Empty as exc:  # pragma: no cover - child crash diagnostics
            raise AssertionError("identity worker returned no result") from exc
    assert {status for status, _value in outcomes} == {"ok"}
    assert len({value for _status, value in outcomes}) == 1
    key_info = key_path.lstat()
    assert stat.S_ISREG(key_info.st_mode)
    assert stat.S_IMODE(key_info.st_mode) == 0o600
    assert key_info.st_nlink == 1
    assert not tuple(
        control_dir.glob(f"{identity_module._PRIVATE_KEY_TEMP_PREFIX}*.tmp")
    )


def test_stale_private_key_temporary_is_removed_under_identity_lock(
    tmp_path: Path,
) -> None:
    control_dir = tmp_path / "control"
    identity = BackupIdentity.load_or_create(control_dir)
    stale = control_dir / (
        f"{identity_module._PRIVATE_KEY_TEMP_PREFIX}{'a' * 32}.tmp"
    )
    os.link(control_dir / identity_module.PRIVATE_KEY_FILENAME, stale)
    assert (control_dir / identity_module.PRIVATE_KEY_FILENAME).stat().st_nlink == 2

    same_identity = BackupIdentity.load_or_create(control_dir)

    assert same_identity.fingerprint_sha256 == identity.fingerprint_sha256
    assert not stale.exists()
    assert (control_dir / identity_module.PRIVATE_KEY_FILENAME).stat().st_nlink == 1
