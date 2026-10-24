"""Token secrets and credentials never leave PPBase through the API.

PocketBase 0.22.34 masks the settings secrets (apis/settings.go:29-30 and
:75-80, models/settings/settings.go:279-334) and keeps them in collection-free
settings; current PocketBase omits the collection token secrets and OAuth2
client secrets (v0.40.6 core/collection_model.go:564-584).  Saving what was
served must keep the stored secrets.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.asyncio

TOKEN_OPTIONS = (
    "authToken",
    "passwordResetToken",
    "verificationToken",
    "emailChangeToken",
    "fileToken",
)
MASK = "******"


async def _stored_options(name: str) -> dict[str, Any]:
    from ppbase.db.engine import get_engine

    async with get_engine().connect() as conn:
        result = await conn.execute(
            text("SELECT options FROM _collections WHERE name = :name"), {"name": name}
        )
        return result.scalar_one()


async def _stored_settings() -> dict[str, Any]:
    from ppbase.db.engine import get_engine

    async with get_engine().connect() as conn:
        result = await conn.execute(text("SELECT value FROM _params WHERE key = 'settings'"))
        return result.scalar_one()


async def _restore_settings(value: dict[str, Any]) -> None:
    from ppbase.db.engine import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(
            text("UPDATE _params SET value = CAST(:value AS jsonb) WHERE key = 'settings'"),
            {"value": json.dumps(value)},
        )


def _secrets(options: dict[str, Any]) -> dict[str, Any]:
    stored = {name: options[name]["secret"] for name in TOKEN_OPTIONS}
    providers = (options.get("oauth2") or {}).get("providers") or []
    stored["providers"] = {p["name"]: p.get("clientSecret") for p in providers}
    return stored


def _assert_no_secret(collection: dict[str, Any], values: list[str]) -> None:
    options = collection["options"]
    for name in TOKEN_OPTIONS:
        if name in options:
            assert "secret" not in options[name], name
    for provider in (options.get("oauth2") or {}).get("providers") or []:
        assert "clientSecret" not in provider
    serialized = json.dumps(collection)
    for value in values:
        assert value not in serialized


async def _auth_collection_with_provider(app_client: AsyncClient, admin_token: str) -> str:
    name = f"vault_{uuid.uuid4().hex[:8]}"
    resp = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": name,
            "type": "auth",
            "schema": [],
            "options": {
                "oauth2": {
                    "enabled": True,
                    "providers": [
                        {
                            "name": "github",
                            "clientId": "github-client-id",
                            "clientSecret": "github-client-secret-value",
                        }
                    ],
                }
            },
        },
    )
    assert resp.status_code == 200, resp.text
    return name


class TestCollectionSecrets:
    async def test_collection_responses_never_carry_secrets(
        self, app_client: AsyncClient, admin_token: str
    ) -> None:
        name = await _auth_collection_with_provider(app_client, admin_token)
        stored = _secrets(await _stored_options(name))
        values = [stored[n] for n in TOKEN_OPTIONS] + ["github-client-secret-value"]
        superusers = _secrets(await _stored_options("_superusers"))
        values += [superusers[n] for n in TOKEN_OPTIONS]

        headers = {"Authorization": admin_token}
        one = await app_client.get(f"/api/collections/{name}", headers=headers)
        assert one.status_code == 200
        _assert_no_secret(one.json(), values)
        assert one.json()["options"]["oauth2"]["providers"][0]["clientId"] == "github-client-id"
        assert one.json()["options"]["authToken"]["duration"] > 0

        listing = await app_client.get("/api/collections?perPage=500", headers=headers)
        assert listing.status_code == 200
        assert all(value not in listing.text for value in values)
        for item in listing.json()["items"]:
            _assert_no_secret(item, values)

        su = await app_client.get("/api/collections/_superusers", headers=headers)
        _assert_no_secret(su.json(), values)

    async def test_create_response_has_no_secret_and_stores_generated_ones(
        self, app_client: AsyncClient, admin_token: str
    ) -> None:
        name = f"created_{uuid.uuid4().hex[:8]}"
        resp = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": name,
                "type": "auth",
                "schema": [],
                # Masked or blank secrets are never stored as secrets.
                "options": {
                    "authToken": {"secret": MASK, "duration": 1234},
                    "fileToken": {"secret": ""},
                },
            },
        )
        assert resp.status_code == 200, resp.text
        stored = _secrets(await _stored_options(name))
        _assert_no_secret(resp.json(), [stored[n] for n in TOKEN_OPTIONS])
        assert resp.json()["options"]["authToken"]["duration"] == 1234
        assert all(len(stored[n]) == 50 for n in TOKEN_OPTIONS)

    async def test_dashboard_round_trip_keeps_secrets(
        self, app_client: AsyncClient, admin_token: str
    ) -> None:
        name = await _auth_collection_with_provider(app_client, admin_token)
        before = _secrets(await _stored_options(name))
        headers = {"Authorization": admin_token}
        served = (await app_client.get(f"/api/collections/{name}", headers=headers)).json()

        # The whole served options (no secrets) sent back unchanged.
        full = await app_client.patch(
            f"/api/collections/{name}", headers=headers, json={"options": served["options"]}
        )
        assert full.status_code == 200, full.text
        assert _secrets(await _stored_options(name)) == before

        # What the PPBase dashboard sends: only oauth2, provider without its secret.
        oauth2 = copy.deepcopy(served["options"]["oauth2"])
        oauth2["providers"][0]["clientId"] = "github-client-id-2"
        dashboard = await app_client.patch(
            f"/api/collections/{name}", headers=headers, json={"options": {"oauth2": oauth2}}
        )
        assert dashboard.status_code == 200, dashboard.text
        stored = await _stored_options(name)
        assert _secrets(stored) == before
        assert stored["oauth2"]["providers"][0]["clientId"] == "github-client-id-2"
        _assert_no_secret(dashboard.json(), ["github-client-secret-value"])

    @pytest.mark.parametrize("sent", [MASK, "", None])
    async def test_masked_blank_or_null_secret_keeps_the_stored_one(
        self, app_client: AsyncClient, admin_token: str, sent: Any
    ) -> None:
        name = await _auth_collection_with_provider(app_client, admin_token)
        before = _secrets(await _stored_options(name))
        resp = await app_client.patch(
            f"/api/collections/{name}",
            headers={"Authorization": admin_token},
            json={
                "options": {
                    "authToken": {"secret": sent, "duration": 4321},
                    "oauth2": {
                        "enabled": True,
                        "providers": [
                            {"name": "github", "clientId": "github-client-id", "clientSecret": sent}
                        ],
                    },
                }
            },
        )
        assert resp.status_code == 200, resp.text
        stored = await _stored_options(name)
        assert _secrets(stored) == before
        assert stored["authToken"]["duration"] == 4321

    async def test_null_token_configs_and_oauth2_keep_the_stored_ones(
        self, app_client: AsyncClient, admin_token: str
    ) -> None:
        """A JSON null bound to a Go struct leaves it unchanged (v0.40.6
        ``TokenConfig``/``OAuth2Config``, core/collection_model_auth_options.go:
        292-297 and :407-448); it used to store ``None`` and lose the secrets."""
        name = await _auth_collection_with_provider(app_client, admin_token)
        before = await _stored_options(name)
        headers = {"Authorization": admin_token}
        resp = await app_client.patch(
            f"/api/collections/{name}",
            headers=headers,
            json={"options": {**{option: None for option in TOKEN_OPTIONS}, "oauth2": None}},
        )
        assert resp.status_code == 200, resp.text
        stored = await _stored_options(name)
        assert _secrets(stored) == _secrets(before)
        assert {option: stored[option] for option in TOKEN_OPTIONS} == {
            option: before[option] for option in TOKEN_OPTIONS
        }
        assert stored["oauth2"] == before["oauth2"]

        # The collection still signs and accepts tokens.
        email = f"null_{uuid.uuid4().hex[:8]}@example.com"
        created = await app_client.post(
            f"/api/collections/{name}/records",
            headers=headers,
            json={"email": email, "password": "securepass123", "passwordConfirm": "securepass123"},
        )
        assert created.status_code == 200, created.text
        login = await app_client.post(
            f"/api/collections/{name}/auth-with-password",
            json={"identity": email, "password": "securepass123"},
        )
        assert login.status_code == 200, login.text
        refresh = await app_client.post(
            f"/api/collections/{name}/auth-refresh",
            headers={"Authorization": login.json()["token"]},
        )
        assert refresh.status_code == 200, refresh.text

    async def test_new_secret_is_stored_but_not_echoed(
        self, app_client: AsyncClient, admin_token: str
    ) -> None:
        name = await _auth_collection_with_provider(app_client, admin_token)
        new_secret = "n" * 20 + uuid.uuid4().hex
        resp = await app_client.patch(
            f"/api/collections/{name}",
            headers={"Authorization": admin_token},
            json={"options": {"authToken": {"secret": new_secret}}},
        )
        assert resp.status_code == 200, resp.text
        assert new_secret not in resp.text
        assert (await _stored_options(name))["authToken"]["secret"] == new_secret

    async def test_import_round_trip_keeps_secrets(
        self, app_client: AsyncClient, admin_token: str
    ) -> None:
        name = await _auth_collection_with_provider(app_client, admin_token)
        before = _secrets(await _stored_options(name))
        headers = {"Authorization": admin_token}
        served = (await app_client.get(f"/api/collections/{name}", headers=headers)).json()
        # What an export carries: no token secret, providers without clientSecret.
        exported = copy.deepcopy(served)
        for option in TOKEN_OPTIONS:
            exported["options"].get(option, {}).pop("secret", None)
        for provider in exported["options"]["oauth2"]["providers"]:
            provider.pop("clientSecret", None)
        resp = await app_client.put(
            "/api/collections/import",
            headers=headers,
            json={"collections": [exported], "deleteMissing": False},
        )
        assert resp.status_code == 204, resp.text
        assert _secrets(await _stored_options(name)) == before


class TestSettingsSecrets:
    async def test_settings_secrets_are_masked_and_kept(
        self, app_client: AsyncClient, admin_token: str
    ) -> None:
        headers = {"Authorization": admin_token}
        # The settings row is created with the defaults on first read.
        assert (await app_client.get("/api/settings", headers=headers)).status_code == 200
        original = await _stored_settings()
        try:
            update = await app_client.patch(
                "/api/settings",
                headers=headers,
                json={
                    "smtp": {"username": "mailer", "password": "smtp-password-value"},
                    "s3": {"enabled": False, "secret": "s3-secret-value"},
                    "backups": {"s3": {"enabled": False, "secret": "backup-secret-value"}},
                    # PocketBase 0.22 key: stored as-is, never served back.
                    "recordAuthToken": {"secret": "legacy-token-secret-value"},
                },
            )
            assert update.status_code == 200, update.text
            for value in (
                "smtp-password-value",
                "s3-secret-value",
                "backup-secret-value",
                "legacy-token-secret-value",
            ):
                assert value not in update.text
            body = update.json()
            assert body["smtp"]["password"] == MASK
            assert body["smtp"]["username"] == "mailer"
            assert body["s3"]["secret"] == MASK
            assert body["backups"]["s3"]["secret"] == MASK
            assert body["recordAuthToken"]["secret"] == MASK

            listed = await app_client.get("/api/settings", headers=headers)
            assert listed.status_code == 200
            assert listed.json() == body

            stored = await _stored_settings()
            assert stored["smtp"]["password"] == "smtp-password-value"
            assert stored["s3"]["secret"] == "s3-secret-value"
            assert stored["backups"]["s3"]["secret"] == "backup-secret-value"

            # Saving the served document back keeps every secret.
            saved = await app_client.patch("/api/settings", headers=headers, json=body)
            assert saved.status_code == 200, saved.text
            stored = await _stored_settings()
            assert stored["smtp"]["password"] == "smtp-password-value"
            assert stored["s3"]["secret"] == "s3-secret-value"
            assert stored["backups"]["s3"]["secret"] == "backup-secret-value"
            assert stored["recordAuthToken"]["secret"] == "legacy-token-secret-value"

            # null keeps (Go ignores a JSON null bound to a string field).
            nulled = await app_client.patch(
                "/api/settings", headers=headers, json={"smtp": {"password": None, "host": "h2"}}
            )
            assert nulled.status_code == 200, nulled.text
            stored = await _stored_settings()
            assert stored["smtp"]["password"] == "smtp-password-value"
            assert stored["smtp"]["host"] == "h2"

            # null sections, sub-sections and values keep the stored ones
            # (measured on 0.22.34: 200 and nothing changed).
            kept = await _stored_settings()
            for patch in (
                {"smtp": None},
                {"meta": None},
                {"s3": None},
                {"backups": None},
                {"backups": {"s3": None, "cron": None, "cronMaxKeep": None}},
                {"recordAuthToken": None},
                {"smtp": {"host": None, "password": None}},
            ):
                resp = await app_client.patch("/api/settings", headers=headers, json=patch)
                assert resp.status_code == 200, (patch, resp.text)
                assert await _stored_settings() == kept, patch

            # An empty string clears the secret, as in PocketBase 0.22.34.
            cleared = await app_client.patch(
                "/api/settings", headers=headers, json={"smtp": {"password": ""}}
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["smtp"]["password"] == ""
            assert (await _stored_settings())["smtp"]["password"] == ""
        finally:
            await _restore_settings(original)


class TestBoundParametersAreHidden:
    async def test_sql_errors_do_not_contain_bound_values(self, app_client: AsyncClient) -> None:
        from ppbase.db.engine import get_engine

        engine = get_engine()
        assert engine.sync_engine.hide_parameters is True
        secret = "bound-secret-" + uuid.uuid4().hex
        with pytest.raises(DBAPIError) as exc_info:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT CAST(:value AS integer)"), {"value": secret})
        message = str(exc_info.value)
        assert secret not in message
        assert "hide_parameters" in message
