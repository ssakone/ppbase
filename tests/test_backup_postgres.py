from __future__ import annotations

from ppbase.backup.postgres import (
    CollationContract,
    DatabaseContract,
    ExtensionContract,
    ObjectSecuritySummary,
    sqlalchemy_url_to_libpq,
)


def _contract() -> DatabaseContract:
    return DatabaseContract(
        database="source_db",
        server_version_num=160004,
        server_major=16,
        encoding="UTF8",
        locale_provider="libc",
        collate="C",
        ctype="C",
        icu_locale=None,
        icu_rules=None,
        collation_version=None,
        actual_collation_version=None,
        database_owner="source_owner",
        database_acl=None,
        extensions=(ExtensionContract("plpgsql", "1.0", "pg_catalog"),),
        collations=(
            CollationContract(
                schema="pg_catalog",
                name="C",
                provider="libc",
                collate="C",
                ctype="C",
                locale=None,
                rules=None,
                version=None,
                actual_version=None,
            ),
        ),
        object_security=(
            ObjectSecuritySummary("relation", 4, ("source_owner",), 0),
        ),
    )


def test_sqlalchemy_url_to_libpq_separates_password_and_maps_ssl() -> None:
    connection = sqlalchemy_url_to_libpq(
        "postgresql+asyncpg://restore:p%3Aa%5Css@db.example:5439/target?ssl=require"
    )

    assert connection.conninfo == (
        "host='db.example' port='5439' dbname='target' user='restore' "
        "sslmode='require'"
    )
    assert "p:a" not in connection.conninfo
    assert connection.password == "p:a\\ss"


def test_database_contract_manifest_round_trip() -> None:
    contract = _contract()

    assert DatabaseContract.from_dict(contract.to_dict()) == contract
