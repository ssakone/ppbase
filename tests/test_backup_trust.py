from __future__ import annotations

import os
from pathlib import Path

import pytest

from ppbase.backup.control import ControlPlaneRoot
from ppbase.backup.identity import BackupIdentity
from ppbase.backup.trust import BackupTrustError, BackupTrustStore


def test_trust_store_persists_exact_key_and_fails_closed_after_detachment(
    tmp_path: Path,
) -> None:
    control_path = tmp_path / "control"
    root = ControlPlaneRoot.open(control_path)
    identity = BackupIdentity.load_or_create(tmp_path / "foreign-identity")
    store = BackupTrustStore(root)
    try:
        approved = store.approve(
            identity.public_key_bytes,
            actor_id="admin-b",
        )
        assert approved.fingerprint_sha256 == identity.fingerprint_sha256
        assert store.approved_public_key(identity.fingerprint_sha256) == (
            identity.public_key_bytes
        )

        original = control_path / "trust"
        detached = control_path / "trust-detached"
        original.rename(detached)
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, original)

        with pytest.raises(BackupTrustError):
            store.list()
        assert list(outside.iterdir()) == []
    finally:
        store.close()
        identity.close()
        root.close()
