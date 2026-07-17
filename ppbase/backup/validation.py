"""Direct SQL validation and clone credential rotation for staged restores.

No function in this module constructs a PPBase application or invokes hooks,
mail, object storage, or lifespan handlers.  The caller supplies a connection
to the newly-created staging database.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import secrets
import string
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ppbase.backup.postgres import (
    DatabaseContract,
    PostgresContractError,
    inspect_database_contract,
    set_backup_control_search_path,
)


_REQUIRED_SYSTEM_COLUMNS: dict[str, frozenset[str]] = {
    "_collections": frozenset({"id", "name", "type", "schema", "options"}),
    "_migrations": frozenset({"file", "applied"}),
    "_params": frozenset({"id", "key", "value"}),
    "_superusers": frozenset(
        {"id", "email", "password_hash", "token_key", "created", "updated"}
    ),
}
_TOKEN_SECRET_KEYS = (
    "authToken",
    "passwordResetToken",
    "verificationToken",
    "emailChangeToken",
    "fileToken",
)
_SECRET_ALPHABET = string.ascii_letters + string.digits


class StagedDatabaseValidationError(PostgresContractError):
    """Raised when a staged database is not safe to mark validated."""


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class StagedDatabaseValidation:
    database: str
    errors: tuple[ValidationIssue, ...]
    warnings: tuple[ValidationIssue, ...]
    system_table_count: int
    collection_count: int
    migration_count: int

    @property
    def valid(self) -> bool:
        return not self.errors

    def require_valid(self) -> None:
        if self.errors:
            raise StagedDatabaseValidationError(
                "; ".join(f"{issue.code}: {issue.message}" for issue in self.errors)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "valid": self.valid,
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "system_table_count": self.system_table_count,
            "collection_count": self.collection_count,
            "migration_count": self.migration_count,
        }


@dataclass(frozen=True)
class CloneCredentialRotation:
    auth_collection_count: int
    auth_record_count: int
    collection_secret_count: int


async def validate_staged_database(
    connection: AsyncConnection,
    *,
    expected_database: str | None = None,
    expected_owner: str | None = None,
    expected_contract: DatabaseContract | None = None,
) -> StagedDatabaseValidation:
    """Validate system tables, collection relations, and migration metadata."""
    await set_backup_control_search_path(connection)
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    identity = (
        await connection.execute(
            text(
                """
                SELECT pg_catalog.current_database() AS database,
                       pg_catalog.pg_encoding_to_char(d.encoding) AS encoding
                FROM pg_catalog.pg_database AS d
                WHERE d.datname = pg_catalog.current_database()
                """
            )
        )
    ).mappings().one()
    database = str(identity["database"])
    if expected_database is not None and database != expected_database:
        errors.append(
            ValidationIssue(
                "unexpected_database",
                f"connected to {database!r}, expected staging DB {expected_database!r}",
            )
        )
    if str(identity["encoding"]).upper().replace("-", "") != "UTF8":
        errors.append(
            ValidationIssue(
                "invalid_encoding",
                f"staging DB encoding is {identity['encoding']!r}, not UTF-8",
            )
        )

    if expected_contract is not None:
        actual_contract = await inspect_database_contract(connection)
        errors.extend(
            _compare_database_contracts(
                expected_contract,
                actual_contract,
                expected_owner=expected_owner,
            )
        )
        errors.extend(
            await _validate_database_acl(
                connection,
                expected_owner=expected_owner or actual_contract.database_owner,
            )
        )

    column_rows = (
        await connection.execute(
            text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = ANY(CAST(:table_names AS text[]))
                ORDER BY table_name, ordinal_position
                """
            ),
            {"table_names": list(_REQUIRED_SYSTEM_COLUMNS)},
        )
    ).mappings().all()
    actual_columns: dict[str, set[str]] = {}
    for row in column_rows:
        actual_columns.setdefault(str(row["table_name"]), set()).add(
            str(row["column_name"])
        )
    for table_name, required_columns in _REQUIRED_SYSTEM_COLUMNS.items():
        if table_name not in actual_columns:
            errors.append(
                ValidationIssue(
                    "missing_system_table",
                    f"required system table public.{table_name} is missing",
                )
            )
            continue
        missing = sorted(required_columns - actual_columns[table_name])
        if missing:
            errors.append(
                ValidationIssue(
                    "missing_system_column",
                    f"public.{table_name} lacks columns: {', '.join(missing)}",
                )
            )

    system_table_count = len(actual_columns)
    collection_count = 0
    migration_count = 0
    if "_collections" in actual_columns:
        collection_rows = (
            await connection.execute(
                text(
                    """
                    SELECT c.id, c.name, c.type,
                           jsonb_typeof(c."schema") AS schema_type,
                           jsonb_typeof(c.options) AS options_type,
                           r.relkind::text AS relkind
                    FROM public."_collections" AS c
                    LEFT JOIN pg_namespace AS n ON n.nspname = 'public'
                    LEFT JOIN pg_class AS r
                      ON r.relnamespace = n.oid AND r.relname = c.name
                    ORDER BY c.name
                    """
                )
            )
        ).mappings().all()
        collection_count = len(collection_rows)
        superusers_collection_valid = False
        for row in collection_rows:
            collection_name = str(row["name"])
            collection_type = str(row["type"])
            if collection_name == "_superusers" and collection_type == "auth":
                superusers_collection_valid = True
            if row["schema_type"] != "array":
                errors.append(
                    ValidationIssue(
                        "invalid_collection_schema",
                        f"collection {collection_name!r} schema is not a JSON array",
                    )
                )
            if row["options_type"] != "object":
                errors.append(
                    ValidationIssue(
                        "invalid_collection_options",
                        f"collection {collection_name!r} options is not a JSON object",
                    )
                )
            relkind = None if row["relkind"] is None else str(row["relkind"])
            expected_kinds = {"v", "m"} if collection_type == "view" else {"r", "p"}
            if relkind not in expected_kinds:
                errors.append(
                    ValidationIssue(
                        "missing_collection_relation",
                        f"collection {collection_name!r} has no compatible public relation",
                    )
                )

        if not superusers_collection_valid:
            errors.append(
                ValidationIssue(
                    "missing_superusers_collection",
                    "_superusers collection metadata is missing or is not auth",
                )
            )

        duplicate_names = (
            await connection.execute(
                text(
                    """
                    SELECT name
                    FROM public."_collections"
                    GROUP BY name
                    HAVING count(*) <> 1
                    ORDER BY name
                    """
                )
            )
        ).scalars().all()
        if duplicate_names:
            errors.append(
                ValidationIssue(
                    "duplicate_collection_name",
                    "duplicate collection names: "
                    + ", ".join(str(name) for name in duplicate_names),
                )
            )

    if "_migrations" in actual_columns:
        migration_stats = (
            await connection.execute(
                text(
                    """
                    SELECT count(*)::integer AS migration_count,
                           count(*) FILTER (
                               WHERE file IS NULL OR btrim(file) = '' OR applied IS NULL
                           )::integer AS invalid_count
                    FROM public."_migrations"
                    """
                )
            )
        ).mappings().one()
        migration_count = int(migration_stats["migration_count"])
        if int(migration_stats["invalid_count"]):
            errors.append(
                ValidationIssue(
                    "invalid_migration_metadata",
                    "migration history contains an empty file or applied timestamp",
                )
            )

    catalog_health = (
        await connection.execute(
            text(
                """
                SELECT
                    (
                        SELECT count(*)
                        FROM pg_constraint AS c
                        JOIN pg_namespace AS n ON n.oid = c.connamespace
                        WHERE n.nspname = 'public' AND NOT c.convalidated
                    )::integer AS invalid_constraints,
                    (
                        SELECT count(*)
                        FROM pg_index AS i
                        JOIN pg_class AS r ON r.oid = i.indrelid
                        JOIN pg_namespace AS n ON n.oid = r.relnamespace
                        WHERE n.nspname = 'public'
                          AND (NOT i.indisvalid OR NOT i.indisready)
                    )::integer AS invalid_indexes
                """
            )
        )
    ).mappings().one()
    if int(catalog_health["invalid_constraints"]):
        errors.append(
            ValidationIssue(
                "invalid_constraints",
                "one or more public constraints are not validated",
            )
        )
    if int(catalog_health["invalid_indexes"]):
        errors.append(
            ValidationIssue(
                "invalid_indexes",
                "one or more public indexes are invalid or not ready",
            )
        )

    if expected_owner is not None:
        unexpected_owners = (
            await connection.execute(
                text(
                    """
                    SELECT DISTINCT pg_get_userbyid(c.relowner) AS owner
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
                      AND pg_get_userbyid(c.relowner) <> :expected_owner
                    ORDER BY owner
                    """
                ),
                {"expected_owner": expected_owner},
            )
        ).scalars().all()
        if unexpected_owners:
            errors.append(
                ValidationIssue(
                    "unexpected_object_owner",
                    "public objects are not all owned by the target owner: "
                    + ", ".join(str(owner) for owner in unexpected_owners),
                )
            )

    if migration_count == 0:
        warnings.append(
            ValidationIssue(
                "empty_migration_history",
                "staging DB contains no application migration history",
            )
        )
    return StagedDatabaseValidation(
        database=database,
        errors=tuple(errors),
        warnings=tuple(warnings),
        system_table_count=system_table_count,
        collection_count=collection_count,
        migration_count=migration_count,
    )


def generate_clone_jwt_secret() -> str:
    """Generate the 64-character secret to install in the clone data_dir."""
    return "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(64))


async def rotate_clone_database_secrets(
    connection: AsyncConnection,
) -> CloneCredentialRotation:
    """Transactionally invalidate every auth/session token in a staged clone.

    This rotates each auth record ``token_key`` (including superusers) and all
    per-collection purpose-token secrets.  The caller must separately install
    ``generate_clone_jwt_secret()`` as the new data-dir ``.jwt_secret`` with
    mode 0600; this function intentionally performs no filesystem writes.
    """
    transaction = (
        connection.begin_nested()
        if connection.in_transaction()
        else connection.begin()
    )
    auth_record_count = 0
    collection_secret_count = 0
    async with transaction:
        await set_backup_control_search_path(connection)
        collections = (
            await connection.execute(
                text(
                    """
                    SELECT id, name, options
                    FROM public."_collections"
                    WHERE type = 'auth'
                    ORDER BY name
                    FOR UPDATE
                    """
                )
            )
        ).mappings().all()

        for collection in collections:
            collection_id = str(collection["id"])
            collection_name = str(collection["name"])
            relation_exists = bool(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                FROM pg_class AS c
                                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                                WHERE n.nspname = 'public'
                                  AND c.relname = :name
                                  AND c.relkind IN ('r', 'p')
                            )
                            """
                        ),
                        {"name": collection_name},
                    )
                ).scalar_one()
            )
            if not relation_exists:
                raise StagedDatabaseValidationError(
                    f"auth collection {collection_name!r} has no physical table"
                )
            identifier = _quote_database_identifier(collection_name)
            await connection.execute(text(f"LOCK TABLE public.{identifier} IN EXCLUSIVE MODE"))
            record_ids = (
                await connection.execute(
                    text(f"SELECT id FROM public.{identifier} ORDER BY id")
                )
            ).scalars().all()
            if record_ids:
                await connection.execute(
                    text(
                        f"UPDATE public.{identifier} "
                        'SET token_key = :token_key, updated = now() WHERE id = :id'
                    ),
                    [
                        {
                            "id": record_id,
                            "token_key": _generate_token_key(),
                        }
                        for record_id in record_ids
                    ],
                )
            auth_record_count += len(record_ids)

            options = collection["options"]
            if not isinstance(options, dict):
                raise StagedDatabaseValidationError(
                    f"auth collection {collection_name!r} options are not an object"
                )
            rotated_options = json.loads(json.dumps(options))
            for key in _TOKEN_SECRET_KEYS:
                token_config = rotated_options.get(key)
                if token_config is None:
                    token_config = {}
                    rotated_options[key] = token_config
                if not isinstance(token_config, dict):
                    raise StagedDatabaseValidationError(
                        f"auth collection {collection_name!r} has invalid {key} config"
                    )
                token_config["secret"] = _generate_token_key()
                collection_secret_count += 1
            await connection.execute(
                text(
                    """
                    UPDATE public."_collections"
                    SET options = CAST(:options AS jsonb), updated = now()
                    WHERE id = :collection_id
                    """
                ),
                {
                    "options": json.dumps(
                        rotated_options,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "collection_id": collection_id,
                },
            )

    return CloneCredentialRotation(
        auth_collection_count=len(collections),
        auth_record_count=auth_record_count,
        collection_secret_count=collection_secret_count,
    )


def _generate_token_key() -> str:
    return "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(50))


def _quote_database_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise StagedDatabaseValidationError("invalid collection table identifier")
    return '"' + value.replace('"', '""') + '"'


def _compare_database_contracts(
    expected: DatabaseContract,
    actual: DatabaseContract,
    *,
    expected_owner: str | None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    scalar_fields = (
        ("server_major", expected.server_major, actual.server_major),
        (
            "encoding",
            expected.encoding.upper().replace("-", ""),
            actual.encoding.upper().replace("-", ""),
        ),
        ("locale_provider", expected.locale_provider, actual.locale_provider),
        ("collate", expected.collate, actual.collate),
        ("ctype", expected.ctype, actual.ctype),
        ("icu_locale", expected.icu_locale, actual.icu_locale),
        ("icu_rules", expected.icu_rules, actual.icu_rules),
        (
            "collation_version",
            expected.collation_version,
            actual.collation_version,
        ),
        (
            "actual_collation_version",
            expected.actual_collation_version,
            actual.actual_collation_version,
        ),
    )
    mismatched_fields = [
        name for name, expected_value, actual_value in scalar_fields
        if expected_value != actual_value
    ]
    if mismatched_fields:
        issues.append(
            ValidationIssue(
                "database_contract_mismatch",
                "restored database differs for: " + ", ".join(mismatched_fields),
            )
        )

    if expected_owner is not None and actual.database_owner != expected_owner:
        issues.append(
            ValidationIssue(
                "database_owner_mismatch",
                f"database owner is {actual.database_owner!r}, expected {expected_owner!r}",
            )
        )

    expected_extensions = {
        (item.name, item.version, item.schema) for item in expected.extensions
    }
    actual_extensions = {
        (item.name, item.version, item.schema) for item in actual.extensions
    }
    if expected_extensions != actual_extensions:
        issues.append(
            ValidationIssue(
                "extension_contract_mismatch",
                "restored extension name/version/schema set differs from the manifest",
            )
        )

    expected_collations = {
        (
            item.schema,
            item.name,
            item.provider,
            item.collate,
            item.ctype,
            item.locale,
            item.rules,
            item.version,
            item.actual_version,
        )
        for item in expected.collations
    }
    actual_collations = {
        (
            item.schema,
            item.name,
            item.provider,
            item.collate,
            item.ctype,
            item.locale,
            item.rules,
            item.version,
            item.actual_version,
        )
        for item in actual.collations
    }
    if expected_collations != actual_collations:
        issues.append(
            ValidationIssue(
                "collation_contract_mismatch",
                "restored relevant collation definitions or versions differ",
            )
        )
    for item in actual.collations:
        if (
            item.version
            and item.actual_version
            and item.version != item.actual_version
        ):
            issues.append(
                ValidationIssue(
                    "stale_restored_collation",
                    f"restored collation {item.schema}.{item.name} has a stale version",
                )
            )
    return issues


async def _validate_database_acl(
    connection: AsyncConnection,
    *,
    expected_owner: str,
) -> list[ValidationIssue]:
    rows = (
        await connection.execute(
            text(
                """
                SELECT
                    CASE
                        WHEN expanded.grantee = 0 THEN 'PUBLIC'
                        ELSE pg_get_userbyid(expanded.grantee)
                    END AS grantee,
                    expanded.privilege_type,
                    expanded.is_grantable,
                    pg_get_userbyid(d.datdba) AS database_owner,
                    session_user AS restore_role
                FROM pg_database AS d
                CROSS JOIN LATERAL aclexplode(COALESCE(
                    d.datacl,
                    acldefault('d', d.datdba)
                )) AS expanded
                WHERE d.datname = current_database()
                ORDER BY grantee, expanded.privilege_type
                """
            )
        )
    ).mappings().all()
    issues: list[ValidationIssue] = []
    if not rows:
        return [
            ValidationIssue(
                "database_acl_mismatch",
                "restored database ACL could not be inspected",
            )
        ]

    actual_owner = str(rows[0]["database_owner"])
    restore_role = str(rows[0]["restore_role"])
    if actual_owner != expected_owner:
        issues.append(
            ValidationIssue(
                "database_acl_mismatch",
                f"database ACL owner is {actual_owner!r}, expected {expected_owner!r}",
            )
        )

    privileges: dict[str, set[str]] = {}
    grantable: dict[str, set[str]] = {}
    for row in rows:
        grantee = str(row["grantee"])
        privilege = str(row["privilege_type"])
        privileges.setdefault(grantee, set()).add(privilege)
        if bool(row["is_grantable"]):
            grantable.setdefault(grantee, set()).add(privilege)

    expected_privileges = {
        expected_owner: {"CONNECT", "CREATE", "TEMPORARY"},
        restore_role: {"CONNECT", "TEMPORARY"},
    }
    if privileges.get("PUBLIC", set()):
        issues.append(
            ValidationIssue(
                "database_acl_mismatch",
                "PUBLIC retains privileges on the restored database",
            )
        )
    unexpected_grantees = sorted(
        set(privileges) - {"PUBLIC", expected_owner, restore_role}
    )
    if unexpected_grantees:
        issues.append(
            ValidationIssue(
                "database_acl_mismatch",
                "unexpected database ACL grantees: " + ", ".join(unexpected_grantees),
            )
        )
    for grantee, expected_set in expected_privileges.items():
        if privileges.get(grantee, set()) != expected_set:
            issues.append(
                ValidationIssue(
                    "database_acl_mismatch",
                    f"database privileges for {grantee!r} are not normalized",
                )
            )
    if grantable.get(restore_role, set()):
        issues.append(
            ValidationIssue(
                "database_acl_mismatch",
                "restore role has database privilege grant options",
            )
        )
    return issues
