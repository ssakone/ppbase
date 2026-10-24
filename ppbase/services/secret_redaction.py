"""Keep token secrets and credentials out of API responses.

PocketBase never serves secrets to API clients, superusers included:

* collections (current PocketBase, where token options live in the
  collection): the five token secrets and every OAuth2 ``clientSecret`` are
  blanked, then omitted (v0.40.6 ``core/collection_model.go:564-584`` with
  ``json:"secret,omitempty"`` in ``core/collection_model_auth_options.go:293``
  and ``json:"clientSecret,omitempty"`` ``:537``);
* settings (PocketBase 0.22.34, the contract of the migrated clients):
  ``GET`` and ``PATCH /api/settings`` answer ``RedactClone``, where every
  non-empty secret becomes ``******`` (``apis/settings.go:29-30`` and
  ``:75-80``, ``models/settings/settings.go:19-21`` and ``:279-334``).

A client that saves what it was shown must not destroy the stored secret.
PocketBase binds the request on top of the stored value, so an omitted key
keeps it (``forms/settings_upsert.go:28-38``, v0.40.6 ``apis/settings.go:51-59``)
and OAuth2 providers are merged by name (v0.40.6
``core/collection_model_auth_options.go:415-470``).  The 0.22.34 dashboard
strips the mask before saving (``ui/src/utils/CommonHelper.js:332-349``);
PPBase also treats a returned mask as "unchanged" on the server side.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

SECRET_MASK = "******"

# Token configurations stored in the options of an auth collection.
AUTH_TOKEN_OPTION_KEYS = (
    "authToken",
    "passwordResetToken",
    "verificationToken",
    "emailChangeToken",
    "fileToken",
)

# PocketBase 0.22 settings keys that hold a secret (models/settings/settings.go:24-70).
LEGACY_SETTINGS_TOKEN_KEYS = (
    "adminAuthToken",
    "adminPasswordResetToken",
    "adminFileToken",
    "recordAuthToken",
    "recordPasswordResetToken",
    "recordEmailChangeToken",
    "recordVerificationToken",
    "recordFileToken",
)
LEGACY_SETTINGS_OAUTH2_KEYS = (
    "googleAuth",
    "facebookAuth",
    "githubAuth",
    "gitlabAuth",
    "discordAuth",
    "twitterAuth",
    "microsoftAuth",
    "spotifyAuth",
    "kakaoAuth",
    "twitchAuth",
    "stravaAuth",
    "giteeAuth",
    "livechatAuth",
    "giteaAuth",
    "oidcAuth",
    "oidc2Auth",
    "oidc3Auth",
    "appleAuth",
    "instagramAuth",
    "vkAuth",
    "yandexAuth",
    "patreonAuth",
    "mailcowAuth",
    "bitbucketAuth",
    "planningcenterAuth",
)

# Same fields as v0.22.34 ``Settings.RedactClone`` (models/settings/settings.go:287-324).
SETTINGS_SECRET_PATHS: tuple[tuple[str, ...], ...] = (
    ("smtp", "password"),
    ("s3", "secret"),
    ("backups", "s3", "secret"),
    *((key, "secret") for key in LEGACY_SETTINGS_TOKEN_KEYS),
    *((key, "clientSecret") for key in LEGACY_SETTINGS_OAUTH2_KEYS),
)


def _keeps_stored_secret(value: Any) -> bool:
    """Whether a submitted collection secret means "keep the stored one".

    ``None``, ``""`` and the mask are what a client sends back for a secret it
    was not shown.  Storing them would sign tokens with the record token key
    alone (or break signing), so the stored secret is kept.
    """
    return value is None or value == "" or value == SECRET_MASK


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


def redact_collection_options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a deep copy of collection ``options`` without any secret."""
    redacted = copy.deepcopy(dict(options or {}))
    for key in AUTH_TOKEN_OPTION_KEYS:
        token_config = redacted.get(key)
        if isinstance(token_config, dict):
            token_config.pop("secret", None)
    oauth2 = redacted.get("oauth2")
    if isinstance(oauth2, dict) and isinstance(oauth2.get("providers"), list):
        for provider in oauth2["providers"]:
            if isinstance(provider, dict):
                provider.pop("clientSecret", None)
    return redacted


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge like Go binds JSON on top of a stored struct.

    A JSON ``null`` for a stored mapping (a struct in PocketBase, such as
    ``authToken``, ``oauth2`` or ``passwordAuth``) leaves it unchanged: Go's
    ``encoding/json`` ignores a null bound to a struct, and v0.40.6
    ``OAuth2Config.UnmarshalJSON`` keeps the stored providers when none are
    submitted (``core/collection_model_auth_options.go:422-448``).  Other
    values, ``null`` included (``authRule``, ``manageRule``), replace the
    stored one.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        elif value is None and isinstance(merged.get(key), dict):
            continue
        else:
            merged[key] = value
    return merged


def _without_kept_token_secrets(incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the token configuration values that keep the stored ones.

    The secret is kept when absent, ``None``, empty or masked (see
    :func:`_keeps_stored_secret`); a ``None`` duration is ignored like a JSON
    null bound to the ``int64`` field of v0.40.6 ``TokenConfig``
    (``core/collection_model_auth_options.go:292-297``).
    """
    result = dict(incoming)
    for key in AUTH_TOKEN_OPTION_KEYS:
        token_config = result.get(key)
        if not isinstance(token_config, dict):
            continue
        kept = dict(token_config)
        if "secret" in kept and _keeps_stored_secret(kept["secret"]):
            kept.pop("secret")
        if "duration" in kept and kept["duration"] is None:
            kept.pop("duration")
        result[key] = kept
    return result


def merge_auth_options(
    defaults: Mapping[str, Any],
    existing: Mapping[str, Any],
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge submitted auth options without losing stored credentials.

    ``defaults`` (freshly generated secrets) < ``existing`` < ``incoming``.
    Token configurations are deep-merged; a submitted secret that is absent,
    ``None``, empty or masked keeps the stored one.  OAuth2 providers are a
    list: each submitted provider is merged on top of the stored provider of
    the same name, so a missing (or ``None``/empty/masked) ``clientSecret``
    keeps the stored one; providers not submitted are removed, as in
    PocketBase.
    """
    merged = _deep_merge(defaults, existing)
    incoming_copy = _without_kept_token_secrets(incoming)
    incoming_oauth = incoming_copy.get("oauth2")

    if not (isinstance(incoming_oauth, dict) and isinstance(incoming_oauth.get("providers"), list)):
        return _deep_merge(merged, incoming_copy)

    current_oauth = merged.get("oauth2")
    current_providers = (
        current_oauth.get("providers", []) if isinstance(current_oauth, dict) else []
    )
    current_by_name = {
        provider.get("name"): provider
        for provider in current_providers
        if isinstance(provider, dict) and provider.get("name")
    }
    providers: list[Any] = []
    for provider in incoming_oauth["providers"]:
        if not isinstance(provider, dict):
            providers.append(provider)
            continue
        submitted = dict(provider)
        current = current_by_name.get(submitted.get("name"), {})
        if "clientSecret" in submitted and _keeps_stored_secret(submitted["clientSecret"]):
            if current.get("clientSecret"):
                submitted.pop("clientSecret")
        providers.append(_deep_merge(current, submitted))

    oauth_without_providers = dict(incoming_oauth)
    oauth_without_providers.pop("providers", None)
    incoming_copy["oauth2"] = oauth_without_providers
    merged = _deep_merge(merged, incoming_copy)
    merged_oauth = merged.get("oauth2")
    merged_oauth = dict(merged_oauth) if isinstance(merged_oauth, dict) else {}
    merged_oauth["providers"] = providers
    merged["oauth2"] = merged_oauth
    return merged


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def redact_settings(settings_value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a deep copy where every non-empty secret is ``******``."""
    redacted = copy.deepcopy(dict(settings_value or {}))
    for path in SETTINGS_SECRET_PATHS:
        parent: Any = redacted
        for key in path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if not isinstance(parent, dict):
            continue
        leaf = path[-1]
        value = parent.get(leaf)
        if value is not None and value != "":
            parent[leaf] = SECRET_MASK
    return redacted


def _without_null_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_null_values(item) for key, item in value.items() if item is not None}
    return value


def without_unchanged_settings_values(body: Mapping[str, Any]) -> dict[str, Any]:
    """Drop what a settings ``PATCH`` sends without changing it.

    * ``null``, at any depth of a mapping, keeps the stored value: Go ignores
      a JSON ``null`` bound to a struct, string, number or boolean field, which
      are the 0.22.34 settings sections and values (models/settings/settings.go:
      24-70) apart from the unused ``pkce`` pointer and ``emailAuth`` domain
      lists (:639, :695-696).  Measured on 0.22.34: ``{"smtp": null}``,
      ``{"meta": null}``, ``{"backups": {"s3": null}}`` and ``{"smtp": {"host":
      null, "password": null}}`` all answer 200 and keep the stored values.
      Lists are replaced as a whole and kept as sent.
    * The mask on a secret keeps the stored secret.  This differs from
      0.22.34, which stores the mask as the new secret (its dashboard strips
      it before saving, ui/src/utils/CommonHelper.js:332-349).

    An empty string still clears a secret, as in PocketBase 0.22.34.
    """
    result = _without_null_values(copy.deepcopy(dict(body)))
    for path in SETTINGS_SECRET_PATHS:
        parent: Any = result
        for key in path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if not isinstance(parent, dict):
            continue
        leaf = path[-1]
        if leaf in parent and parent[leaf] == SECRET_MASK:
            parent.pop(leaf)
    return result
