"""The base of e-mailed links is the stored ``meta.appURL`` or the default.

PocketBase: v0.22.34 mails/admin.go:22-26, mails/record.go:175-184 and the
default ``http://localhost:8090`` (models/settings/settings.go:77).
"""

from __future__ import annotations

import pytest

from ppbase.api.settings import _DEFAULT_SETTINGS
from ppbase.services.app_url import DEFAULT_APP_URL, app_url_from_settings


def test_default_is_the_pocketbase_default_and_the_settings_default() -> None:
    assert DEFAULT_APP_URL == "http://localhost:8090"
    assert _DEFAULT_SETTINGS["meta"]["appURL"] == DEFAULT_APP_URL


@pytest.mark.parametrize(
    "settings_value",
    [
        None,
        {},
        {"meta": None},
        {"meta": {}},
        {"meta": {"appURL": ""}},
        {"meta": {"appURL": "   "}},
        {"meta": {"appURL": None}},
        {"meta": {"appURL": 8090}},
        {"meta": {"appURL": "buma.example"}},
        {"meta": {"appURL": "/relative/path"}},
        {"meta": {"appURL": "http://[::1"}},
    ],
)
def test_missing_empty_or_relative_values_give_the_default(settings_value) -> None:
    assert app_url_from_settings(settings_value) == DEFAULT_APP_URL


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("https://buma.example", "https://buma.example"),
        ("https://buma.example/", "https://buma.example"),
        ("https://buma.example/pb//", "https://buma.example/pb"),
        (" https://buma.example:8443/app ", "https://buma.example:8443/app"),
        # Used as stored, like PocketBase: no 0.0.0.0 rewrite.
        ("http://0.0.0.0:8090", "http://0.0.0.0:8090"),
    ],
)
def test_stored_value_is_used_as_is_without_trailing_slash(stored: str, expected: str) -> None:
    assert app_url_from_settings({"meta": {"appURL": stored}}) == expected
