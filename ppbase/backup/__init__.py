"""Native PPBase backup primitives.

The package deliberately separates resource preparation (performed while the
write barrier is held) from signing and sealing (performed only after the lease
has been verified and released successfully).
"""

from ppbase.backup.identity import (
    MANIFEST_SIGNATURE_DOMAIN,
    BackupIdentity,
    BackupIdentityError,
    BackupIdentityMissingError,
    public_key_fingerprint,
    verify_manifest_signature,
)
from ppbase.backup.models import (
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    DATABASE_DUMP_RESOURCE,
    BackupAlreadyExistsError,
    BackupError,
    BackupInspection,
    BackupIntegrityError,
    BackupManifest,
    BackupManifestError,
    BackupNotFoundError,
    BackupResource,
    BackupSetSummary,
    BackupStateError,
    BackupUnsafeSourceError,
    canonical_json_bytes,
)
from ppbase.backup.storage import (
    JWT_SECRET_RESOURCE,
    AuthenticatedBackupInspection,
    BackupDeletionUncertainError,
    BackupSetBuilder,
    LocalBackupStore,
    PinnedBackupArchive,
    PreparedBackupSet,
)
from ppbase.backup.trust import (
    BackupTrustError,
    BackupTrustStore,
    TrustedBackupSigner,
)


__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_FORMAT_VERSION",
    "DATABASE_DUMP_RESOURCE",
    "JWT_SECRET_RESOURCE",
    "MANIFEST_SIGNATURE_DOMAIN",
    "AuthenticatedBackupInspection",
    "BackupAlreadyExistsError",
    "BackupDeletionUncertainError",
    "BackupError",
    "BackupIdentity",
    "BackupIdentityError",
    "BackupIdentityMissingError",
    "BackupInspection",
    "BackupIntegrityError",
    "BackupManifest",
    "BackupManifestError",
    "BackupNotFoundError",
    "BackupResource",
    "BackupSetBuilder",
    "BackupSetSummary",
    "BackupStateError",
    "BackupTrustError",
    "BackupTrustStore",
    "BackupUnsafeSourceError",
    "LocalBackupStore",
    "PinnedBackupArchive",
    "PreparedBackupSet",
    "TrustedBackupSigner",
    "canonical_json_bytes",
    "public_key_fingerprint",
    "verify_manifest_signature",
]
