"""Unit tests for safe migration and collections snapshot generation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from ppbase.db.system_tables import CollectionRecord
from ppbase.services import migration_generator
from ppbase.services.migration_generator import (
    generate_create_migration,
    generate_snapshot_migration,
    generate_update_migration,
)


def _load_generated_migration(path: str | Path) -> ModuleType:
    migration_path = Path(path)
    spec = importlib.util.spec_from_file_location(
        f"test_migration_{migration_path.stem}",
        migration_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collection(
    collection_id: str,
    name: str,
    *,
    collection_type: str = "base",
    system: bool = False,
    schema: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": collection_id,
        "name": name,
        "type": collection_type,
        "system": system,
        "schema": schema or [],
        "indexes": [],
        "listRule": None,
        "viewRule": None,
        "createRule": None,
        "updateRule": None,
        "deleteRule": None,
        "options": options or {},
    }


@pytest.mark.asyncio
async def test_snapshot_is_global_ordered_and_never_exports_secrets(
    tmp_path: Path,
) -> None:
    users = _collection(
        "_pb_users_auth_",
        "users",
        collection_type="auth",
        schema=[
            {
                "id": "users_profile_field",
                "name": "profile",
                "type": "json",
                "required": False,
                "options": {},
            }
        ],
        options={
            "authToken": {"secret": "users-token-secret", "duration": 1234},
            "passwordResetToken": {
                "secret": "users-reset-secret",
                "duration": 456,
            },
            "oauth2": {
                "enabled": True,
                "providers": [
                    {
                        "name": "github",
                        "clientId": "users-oauth-id",
                        "clientSecret": "users-oauth-secret",
                        "displayName": "GitHub",
                    }
                ],
            },
        },
    )
    system_collection = _collection(
        "system_auth_id",
        "_superusers",
        collection_type="auth",
        system=True,
        options={"authToken": {"secret": "system-token-secret"}},
    )
    base_collection = _collection(
        "articles_id",
        "articles",
        schema=[
            {
                "id": "article_title_field",
                "name": "title",
                "type": "text",
                "required": True,
                "options": {},
            }
        ],
    )
    auth_collection = _collection(
        "members_id",
        "members",
        collection_type="auth",
        options={
            "fileToken": {"secret": "members-file-secret", "duration": 180},
            "oauth2": {
                "providers": [
                    {
                        "name": "google",
                        "client_id": "members-oauth-id",
                        "client_secret": "members-oauth-secret",
                    }
                ]
            },
        },
    )
    view_collection = _collection(
        "articles_view_id",
        "articles_view",
        collection_type="view",
        options={"query": 'SELECT * FROM "articles"'},
    )

    generated_path = generate_snapshot_migration(
        [view_collection, system_collection, users, auth_collection, base_collection],
        tmp_path,
    )

    assert len(list(tmp_path.glob("*.py"))) == 1
    source = Path(generated_path).read_text(encoding="utf-8")
    for credential in (
        "users-token-secret",
        "users-reset-secret",
        "users-oauth-id",
        "users-oauth-secret",
        "members-file-secret",
        "members-oauth-id",
        "members-oauth-secret",
        "system-token-secret",
    ):
        assert credential not in source

    module = _load_generated_migration(generated_path)
    assert [item["name"] for item in module._CREATED_COLLECTIONS] == [
        "articles",
        "members",
        "articles_view",
    ]
    assert all(item["name"] != "_superusers" for item in module._CREATED_COLLECTIONS)
    assert module._USERS_CHANGES["schema"][0]["id"] == "users_profile_field"
    assert module._USERS_CHANGES["options"]["authToken"] == {"duration": 1234}
    assert module._USERS_CHANGES["options"]["oauth2"]["providers"] == [
        {"name": "github", "displayName": "GitHub"}
    ]
    assert module._CREATED_COLLECTIONS[0]["id"] == "articles_id"
    assert module._CREATED_COLLECTIONS[0]["schema"][0]["id"] == (
        "article_title_field"
    )

    class RecordingApp:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def find_collection(self, collection_id: str) -> SimpleNamespace:
            assert collection_id == "_pb_users_auth_"
            return SimpleNamespace(id="_pb_users_auth_")

        async def update_collection(self, collection_id: str, changes: dict) -> None:
            self.calls.append(("update", collection_id, changes))

        async def save_collection(self, definition: dict) -> None:
            self.calls.append(("save", definition["name"]))

        async def delete_collection(self, collection_id: str) -> None:
            self.calls.append(("delete", collection_id))

    app = RecordingApp()
    await module.up(app)
    assert [(call[0], call[1]) for call in app.calls] == [
        ("update", "_pb_users_auth_"),
        ("save", "articles"),
        ("save", "members"),
        ("save", "articles_view"),
    ]

    app.calls.clear()
    await module.down(app)
    assert app.calls == []
    assert not hasattr(module, "_USERS_ROLLBACK")


def test_unit_generators_are_unique_and_sanitize_auth_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration_generator.time, "time", lambda: 1700000000)
    record = CollectionRecord(
        id="members_id",
        name="members",
        type="auth",
        system=False,
        schema=[],
        indexes=[],
        options={
            "authToken": {"secret": "create-token-secret", "duration": 30},
            "oauth2": {
                "providers": [
                    {
                        "name": "github",
                        "clientId": "create-client-id",
                        "clientSecret": "create-client-secret",
                    }
                ]
            },
        },
    )

    first_path = generate_create_migration(record, tmp_path)
    first_source = Path(first_path).read_text(encoding="utf-8")
    second_path = generate_create_migration(record, tmp_path)

    assert Path(first_path).name == "1700000000_created_members.py"
    assert Path(second_path).name == "1700000001_created_members.py"
    assert Path(first_path).read_text(encoding="utf-8") == first_source
    assert not list(tmp_path.glob("*.tmp"))

    old_definition = _collection(
        "members_id",
        "members",
        collection_type="auth",
        options={"authToken": {"secret": "old-update-secret", "duration": 30}},
    )
    new_definition = _collection(
        "members_id",
        "members",
        collection_type="auth",
        options={"authToken": {"secret": "new-update-secret", "duration": 60}},
    )
    update_path = generate_update_migration(old_definition, new_definition, tmp_path)

    combined_source = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.glob("*.py")
    )
    for credential in (
        "create-token-secret",
        "create-client-id",
        "create-client-secret",
        "old-update-secret",
        "new-update-secret",
    ):
        assert credential not in combined_source
    assert Path(update_path).exists()


def test_snapshot_targets_legacy_users_by_name(tmp_path: Path) -> None:
    legacy_users = _collection(
        "legacy_users_id",
        "users",
        collection_type="auth",
        schema=[],
    )

    module = _load_generated_migration(
        generate_snapshot_migration([legacy_users], tmp_path)
    )

    assert module._USERS_TARGET == "users"
    assert module._CREATED_COLLECTIONS == []


@pytest.mark.asyncio
async def test_snapshot_rewrites_users_relations_to_the_replay_database_id(
    tmp_path: Path,
) -> None:
    legacy_users = _collection(
        "legacy_users_id",
        "users",
        collection_type="auth",
        schema=[
            {
                "id": "manager_relation",
                "name": "manager",
                "type": "relation",
                "required": False,
                "options": {"collectionId": "legacy_users_id", "maxSelect": 1},
            }
        ],
    )
    profiles = _collection(
        "profiles_id",
        "profiles",
        schema=[
            {
                "id": "owner_relation",
                "name": "owner",
                "type": "relation",
                "required": True,
                "options": {
                    "collectionId": "legacy_users_id",
                    "cascadeDelete": True,
                    "maxSelect": 1,
                },
            }
        ],
    )

    module = _load_generated_migration(
        generate_snapshot_migration([profiles, legacy_users], tmp_path)
    )

    # The generated snapshot preserves its source IDs. Rewriting happens only
    # after resolving the users collection that exists on the replay database.
    assert module._SOURCE_USERS_ID == "legacy_users_id"
    assert module._USERS_CHANGES["schema"][0]["options"]["collectionId"] == (
        "legacy_users_id"
    )
    assert module._CREATED_COLLECTIONS[0]["schema"][0]["options"][
        "collectionId"
    ] == "legacy_users_id"

    class RecordingApp:
        def __init__(self, users_id: str) -> None:
            self.users_id = users_id
            self.users_changes: dict[str, Any] | None = None
            self.saved: list[dict[str, Any]] = []

        async def find_collection(self, collection_id: str) -> SimpleNamespace:
            if collection_id == self.users_id or collection_id == "users":
                return SimpleNamespace(id=self.users_id)
            raise LookupError(collection_id)

        async def update_collection(self, collection_id: str, changes: dict) -> None:
            assert collection_id == self.users_id
            self.users_changes = changes

        async def save_collection(self, definition: dict) -> None:
            self.saved.append(definition)

    stable_app = RecordingApp("_pb_users_auth_")
    await module.up(stable_app)
    assert stable_app.users_changes is not None
    assert stable_app.users_changes["schema"][0]["options"]["collectionId"] == (
        "_pb_users_auth_"
    )
    assert stable_app.saved[0]["schema"][0]["options"]["collectionId"] == (
        "_pb_users_auth_"
    )

    legacy_app = RecordingApp("legacy_users_id")
    await module.up(legacy_app)
    assert legacy_app.users_changes is not None
    assert legacy_app.users_changes["schema"][0]["options"]["collectionId"] == (
        "legacy_users_id"
    )
    assert legacy_app.saved[0]["schema"][0]["options"]["collectionId"] == (
        "legacy_users_id"
    )

    # Runtime normalization must not mutate the module constants, otherwise a
    # later retry against a different database would inherit the first target.
    assert module._CREATED_COLLECTIONS[0]["schema"][0]["options"][
        "collectionId"
    ] == "legacy_users_id"


def test_snapshot_orders_views_by_real_dependencies(tmp_path: Path) -> None:
    source = _collection("source_id", "source")
    parent = _collection(
        "parent_view_id",
        "z_parent_view",
        collection_type="view",
        options={"query": 'SELECT "id" FROM "source"'},
    )
    child = _collection(
        "child_view_id",
        "a_child_view",
        collection_type="view",
        options={"query": 'SELECT "id" FROM "z_parent_view"'},
    )

    module = _load_generated_migration(
        generate_snapshot_migration(
            [child, parent, source],
            tmp_path,
            view_dependencies={"a_child_view": {"z_parent_view"}},
        )
    )

    assert [definition["name"] for definition in module._CREATED_COLLECTIONS] == [
        "source",
        "z_parent_view",
        "a_child_view",
    ]
