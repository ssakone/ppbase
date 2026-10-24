"""Unit tests: secrets are redacted from responses and kept on round trips."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from ppbase.models.collection import CollectionResponse
from ppbase.services.secret_redaction import (
    SECRET_MASK,
    merge_auth_options,
    redact_collection_options,
    redact_settings,
    without_unchanged_settings_values,
)

OPTIONS = {
    "authToken": {"secret": "auth-secret", "duration": 1209600},
    "passwordResetToken": {"secret": "reset-secret", "duration": 1800},
    "verificationToken": {"secret": "verify-secret", "duration": 604800},
    "emailChangeToken": {"secret": "email-secret", "duration": 1800},
    "fileToken": {"secret": "file-secret", "duration": 120},
    "oauth2": {
        "enabled": True,
        "providers": [
            {"name": "google", "clientId": "google-id", "clientSecret": "google-secret"},
            {"name": "github", "clientId": "github-id", "clientSecret": "github-secret"},
        ],
    },
    "passwordAuth": {"enabled": True, "identityFields": ["email", "username"]},
}


def test_redact_collection_options_removes_every_secret_without_mutating() -> None:
    original = copy.deepcopy(OPTIONS)
    redacted = redact_collection_options(OPTIONS)

    assert OPTIONS == original
    for name in (
        "authToken",
        "passwordResetToken",
        "verificationToken",
        "emailChangeToken",
        "fileToken",
    ):
        assert redacted[name] == {"duration": OPTIONS[name]["duration"]}
    assert redacted["oauth2"]["providers"] == [
        {"name": "google", "clientId": "google-id"},
        {"name": "github", "clientId": "github-id"},
    ]
    assert redacted["passwordAuth"] == OPTIONS["passwordAuth"]


def test_collection_response_serves_redacted_options() -> None:
    record = SimpleNamespace(
        id="users00000000001",
        name="users",
        type="auth",
        system=False,
        schema=[],
        indexes=[],
        options=copy.deepcopy(OPTIONS),
        list_rule=None,
        view_rule=None,
        create_rule=None,
        update_rule=None,
        delete_rule=None,
        created=None,
        updated=None,
    )
    dumped = str(CollectionResponse.from_record(record).model_dump())
    for value in ("auth-secret", "reset-secret", "verify-secret", "email-secret", "file-secret"):
        assert value not in dumped
    assert "google-secret" not in dumped and "github-secret" not in dumped
    # The stored options are untouched.
    assert record.options["authToken"]["secret"] == "auth-secret"


def test_merge_auth_options_keeps_secrets_that_were_not_shown() -> None:
    defaults = {
        "authToken": {"secret": "generated", "duration": 1},
        "fileToken": {"secret": "generated-file", "duration": 1},
        "oauth2": {"enabled": False, "providers": []},
    }
    served = redact_collection_options(OPTIONS)
    merged = merge_auth_options(defaults, OPTIONS, served)
    assert merged["authToken"] == OPTIONS["authToken"]
    assert merged["fileToken"] == OPTIONS["fileToken"]
    assert merged["oauth2"]["providers"] == OPTIONS["oauth2"]["providers"]

    for sent in (None, "", SECRET_MASK):
        incoming = {
            "authToken": {"secret": sent, "duration": 99},
            "oauth2": {
                "providers": [
                    {"name": "github", "clientSecret": sent},
                    {"name": "gitlab", "clientId": "gitlab-id", "clientSecret": "gitlab-secret"},
                ]
            },
        }
        merged = merge_auth_options(defaults, OPTIONS, incoming)
        assert merged["authToken"] == {"secret": "auth-secret", "duration": 99}
        # google was not submitted: removed, as in PocketBase.
        assert merged["oauth2"]["providers"] == [
            {"name": "github", "clientId": "github-id", "clientSecret": "github-secret"},
            {"name": "gitlab", "clientId": "gitlab-id", "clientSecret": "gitlab-secret"},
        ]
        assert merged["oauth2"]["enabled"] is True

    replaced = merge_auth_options(defaults, OPTIONS, {"authToken": {"secret": "rotated"}})
    assert replaced["authToken"] == {"secret": "rotated", "duration": 1209600}
    # A brand new collection never stores a blank or masked secret.
    created = merge_auth_options(defaults, {}, {"authToken": {"secret": SECRET_MASK}})
    assert created["authToken"]["secret"] == "generated"


def test_merge_auth_options_ignores_null_sections_like_go() -> None:
    """A JSON null bound to a Go struct leaves it unchanged.

    v0.40.6 ``TokenConfig`` and ``OAuth2Config`` are struct fields of the
    collection (core/collection_model_auth_options.go:292-297, :407-448); a
    null used to replace them with ``None`` and lose every stored secret.
    """
    defaults = {"authToken": {"secret": "generated", "duration": 1}}
    stored = copy.deepcopy(OPTIONS)
    stored["manageRule"] = "id != ''"
    for name in ("authToken", "fileToken", "oauth2", "passwordAuth"):
        merged = merge_auth_options(defaults, stored, {name: None})
        assert merged[name] == stored[name], name
    # Null scalars of a token configuration are ignored too (int64/string fields).
    merged = merge_auth_options(defaults, stored, {"authToken": {"secret": None, "duration": None}})
    assert merged["authToken"] == stored["authToken"]
    # Pointer-like values keep their meaning: a null rule is "superusers only".
    assert merge_auth_options(defaults, stored, {"manageRule": None})["manageRule"] is None


def test_settings_are_masked_like_pocketbase_redact_clone() -> None:
    stored = {
        "meta": {"appName": "BUMA"},
        "smtp": {"enabled": True, "username": "mailer", "password": "smtp-pass"},
        "s3": {"enabled": False, "secret": ""},
        "backups": {"cron": "0 0 * * *", "s3": {"secret": "backup-secret"}},
        "recordAuthToken": {"secret": "legacy-secret", "duration": 1209600},
        "adminFileToken": {"secret": "", "duration": 120},
        "googleAuth": {"enabled": False, "clientId": "id", "clientSecret": "google"},
    }
    original = copy.deepcopy(stored)
    redacted = redact_settings(stored)

    assert stored == original
    assert redacted["smtp"] == {"enabled": True, "username": "mailer", "password": SECRET_MASK}
    # Empty secrets stay empty (models/settings/settings.go:327-331).
    assert redacted["s3"]["secret"] == ""
    assert redacted["adminFileToken"]["secret"] == ""
    assert redacted["backups"] == {"cron": "0 0 * * *", "s3": {"secret": SECRET_MASK}}
    assert redacted["recordAuthToken"] == {"secret": SECRET_MASK, "duration": 1209600}
    assert redacted["googleAuth"]["clientSecret"] == SECRET_MASK
    assert redacted["meta"] == stored["meta"]
    assert redact_settings({}) == {}


def test_settings_patch_drops_masked_secrets_and_nulls() -> None:
    body = {
        "smtp": {"host": "h", "password": SECRET_MASK},
        "s3": {"secret": None, "bucket": "b"},
        "backups": {"s3": {"secret": ""}},
        "recordAuthToken": {"secret": SECRET_MASK, "duration": 10},
        "meta": {"appName": SECRET_MASK},
    }
    cleaned = without_unchanged_settings_values(body)
    assert cleaned == {
        "smtp": {"host": "h"},
        "s3": {"bucket": "b"},
        "backups": {"s3": {"secret": ""}},
        "recordAuthToken": {"duration": 10},
        # Not a secret field: never touched.
        "meta": {"appName": SECRET_MASK},
    }
    assert body["smtp"]["password"] == SECRET_MASK


def test_settings_patch_null_keeps_sections_and_values() -> None:
    """Measured on 0.22.34: null sections, sub-sections and leaves keep the stored value."""
    body = {
        "smtp": None,
        "meta": {"appName": None, "appURL": "https://app.example"},
        "backups": {"s3": None, "cron": None},
        "rateLimits": {"rules": [{"label": "*", "audience": None}]},
    }
    assert without_unchanged_settings_values(body) == {
        "meta": {"appURL": "https://app.example"},
        "backups": {},
        # Lists are replaced as a whole, as sent.
        "rateLimits": {"rules": [{"label": "*", "audience": None}]},
    }
