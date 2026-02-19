"""OAuth2 authentication service with PKCE support."""

import asyncio
import base64
import hashlib
import json
import secrets
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.db.system_tables import CollectionRecord, ExternalAuthRecord
from ppbase.services.auth_service import generate_token_key


class OAuth2Provider(ABC):
    """Base class for OAuth2 providers."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret

    @abstractmethod
    def get_auth_url(self, state: str, code_challenge: str, redirect_url: str) -> str:
        """Build OAuth2 authorization URL with PKCE challenge."""
        pass

    @abstractmethod
    async def exchange_code(
        self, code: str, redirect_url: str, code_verifier: str
    ) -> dict[str, Any]:
        """Exchange authorization code for access token."""
        pass

    @abstractmethod
    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        """Fetch user profile from provider API."""
        pass


class GoogleProvider(OAuth2Provider):
    """Google OAuth2 provider."""

    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USER_INFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

    def get_auth_url(self, state: str, code_challenge: str, redirect_url: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_url,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, redirect_url: str, code_verifier: str
    ) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_url,
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to exchange code: {resp.text}", {"provider": "google"}
                )
            return resp.json()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to get user info: {resp.text}", {"provider": "google"}
                )
            data = resp.json()
            return {
                "id": data.get("id"),
                "email": data.get("email"),
                "name": data.get("name"),
                "username": data.get("email", "").split("@")[0],
                "avatarURL": data.get("picture"),
            }


class GitHubProvider(OAuth2Provider):
    """GitHub OAuth2 provider."""

    AUTH_URL = "https://github.com/login/oauth/authorize"
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    USER_INFO_URL = "https://api.github.com/user"
    USER_EMAIL_URL = "https://api.github.com/user/emails"

    def get_auth_url(self, state: str, code_challenge: str, redirect_url: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_url,
            "scope": "read:user user:email",
            "state": state,
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, redirect_url: str, code_verifier: str
    ) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_url,
                },
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to exchange code: {resp.text}", {"provider": "github"}
                )
            return resp.json()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            # Get user profile
            resp = await client.get(
                self.USER_INFO_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to get user info: {resp.text}", {"provider": "github"}
                )
            user_data = resp.json()

            # Get primary email if not public
            email = user_data.get("email")
            if not email:
                email_resp = await client.get(
                    self.USER_EMAIL_URL,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
                if email_resp.status_code == 200:
                    emails = email_resp.json()
                    primary = next((e for e in emails if e.get("primary")), None)
                    if primary:
                        email = primary.get("email")

            return {
                "id": str(user_data.get("id")),
                "email": email,
                "name": user_data.get("name") or user_data.get("login"),
                "username": user_data.get("login"),
                "avatarURL": user_data.get("avatar_url"),
            }


class GitLabProvider(OAuth2Provider):
    """GitLab OAuth2 provider."""

    AUTH_URL = "https://gitlab.com/oauth/authorize"
    TOKEN_URL = "https://gitlab.com/oauth/token"
    USER_INFO_URL = "https://gitlab.com/api/v4/user"

    def get_auth_url(self, state: str, code_challenge: str, redirect_url: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_url,
            "response_type": "code",
            "scope": "read_user",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, redirect_url: str, code_verifier: str
    ) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_url,
                    "grant_type": "authorization_code",
                    "code_verifier": code_verifier,
                },
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to exchange code: {resp.text}", {"provider": "gitlab"}
                )
            return resp.json()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to get user info: {resp.text}", {"provider": "gitlab"}
                )
            data = resp.json()
            return {
                "id": str(data.get("id")),
                "email": data.get("email"),
                "name": data.get("name"),
                "username": data.get("username"),
                "avatarURL": data.get("avatar_url"),
            }


class DiscordProvider(OAuth2Provider):
    """Discord OAuth2 provider."""

    AUTH_URL = "https://discord.com/api/oauth2/authorize"
    TOKEN_URL = "https://discord.com/api/oauth2/token"
    USER_INFO_URL = "https://discord.com/api/users/@me"

    def get_auth_url(self, state: str, code_challenge: str, redirect_url: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_url,
            "response_type": "code",
            "scope": "identify email",
            "state": state,
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, redirect_url: str, code_verifier: str
    ) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_url,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to exchange code: {resp.text}", {"provider": "discord"}
                )
            return resp.json()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to get user info: {resp.text}", {"provider": "discord"}
                )
            data = resp.json()
            username = data.get("username")
            discriminator = data.get("discriminator")
            if discriminator and discriminator != "0":
                username = f"{username}#{discriminator}"

            return {
                "id": data.get("id"),
                "email": data.get("email"),
                "name": data.get("global_name") or username,
                "username": username,
                "avatarURL": (
                    f"https://cdn.discordapp.com/avatars/{data.get('id')}/{data.get('avatar')}.png"
                    if data.get("avatar")
                    else None
                ),
            }


class FacebookProvider(OAuth2Provider):
    """Facebook OAuth2 provider."""

    AUTH_URL = "https://www.facebook.com/v18.0/dialog/oauth"
    TOKEN_URL = "https://graph.facebook.com/v18.0/oauth/access_token"
    USER_INFO_URL = "https://graph.facebook.com/me"

    def get_auth_url(self, state: str, code_challenge: str, redirect_url: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_url,
            "scope": "email public_profile",
            "state": state,
        }
        return f"{self.AUTH_URL}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, redirect_url: str, code_verifier: str
    ) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.TOKEN_URL,
                params={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_url,
                },
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to exchange code: {resp.text}", {"provider": "facebook"}
                )
            return resp.json()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.USER_INFO_URL,
                params={
                    "fields": "id,name,email,picture.width(200).height(200)",
                    "access_token": access_token,
                },
            )
            if resp.status_code != 200:
                raise ValueError(
                    f"Failed to get user info: {resp.text}", {"provider": "facebook"}
                )
            data = resp.json()
            picture_url = None
            if data.get("picture", {}).get("data", {}).get("url"):
                picture_url = data["picture"]["data"]["url"]

            return {
                "id": data.get("id"),
                "email": data.get("email"),
                "name": data.get("name"),
                "username": data.get("email", "").split("@")[0] if data.get("email") else None,
                "avatarURL": picture_url,
            }


def generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge pair.

    Returns:
        Tuple of (code_verifier, code_challenge)
    """
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .decode("utf-8")
        .rstrip("=")
    )
    return code_verifier, code_challenge


def generate_state() -> str:
    """Generate random state parameter for CSRF protection."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")


def get_provider_class(provider_name: str) -> type[OAuth2Provider]:
    """Get provider class by name."""
    providers = {
        "google": GoogleProvider,
        "github": GitHubProvider,
        "gitlab": GitLabProvider,
        "discord": DiscordProvider,
        "facebook": FacebookProvider,
    }
    provider_class = providers.get(provider_name.lower())
    if not provider_class:
        raise ValueError(f"Unknown provider: {provider_name}")
    return provider_class


def get_provider_credentials(
    provider_name: str,
    settings: Any,
    collection_options: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Get OAuth2 credentials from collection options or environment variables.

    Priority: collection options > environment variables

    Args:
        provider_name: Provider name (e.g., "google", "github")
        settings: App settings instance
        collection_options: Optional collection options dict

    Returns:
        Tuple of (client_id, client_secret)
    """
    # Try collection options first
    if collection_options and collection_options.get("oauth2", {}).get("providers"):
        for provider_config in collection_options["oauth2"]["providers"]:
            if provider_config.get("name", "").lower() == provider_name.lower():
                client_id = provider_config.get("clientId")
                client_secret = provider_config.get("clientSecret")
                if client_id and client_secret:
                    return client_id, client_secret

    # Fallback to environment variables
    env_var_map = {
        "google": (settings.oauth2_google_client_id, settings.oauth2_google_client_secret),
        "github": (settings.oauth2_github_client_id, settings.oauth2_github_client_secret),
        "gitlab": (settings.oauth2_gitlab_client_id, settings.oauth2_gitlab_client_secret),
        "discord": (settings.oauth2_discord_client_id, settings.oauth2_discord_client_secret),
        "facebook": (settings.oauth2_facebook_client_id, settings.oauth2_facebook_client_secret),
    }

    credentials = env_var_map.get(provider_name.lower())
    if credentials and credentials[0] and credentials[1]:
        return credentials

    raise ValueError(
        f"OAuth2 credentials not configured for provider: {provider_name}"
    )


async def link_or_create_oauth_user(
    engine: AsyncEngine,
    collection: CollectionRecord,
    provider_name: str,
    provider_user_info: dict[str, Any],
    token_data: dict[str, Any],
    create_data: dict[str, Any],
) -> dict[str, Any]:
    """Link or create user record for OAuth2 authentication.

    Args:
        engine: Database engine
        collection: Auth collection
        provider_name: OAuth2 provider name (e.g., "google")
        provider_user_info: User info from provider
        token_data: Token response from provider (access_token, refresh_token, etc.)
        create_data: Additional data to merge when creating new user

    Returns:
        Dict with keys: record, isNew, meta (OAuth2Meta)
    """
    from ppbase.services.record_service import create_record, get_record

    async with engine.begin() as conn:
        # Check if external auth already exists
        result = await conn.execute(
            select(ExternalAuthRecord.record_id).where(
                ExternalAuthRecord.collection_id == collection.id,
                ExternalAuthRecord.provider == provider_name,
                ExternalAuthRecord.provider_id == provider_user_info["id"],
            )
        )
        external_auth_record_id = result.scalar_one_or_none()

        is_new = False
        record = None

        if external_auth_record_id:
            # Existing OAuth2 link - get the user record
            record = await get_record(engine, collection, str(external_auth_record_id))
        else:
            # Try linking by existing auth email first.
            existing_record_id: str | None = None
            provider_email = str(provider_user_info.get("email") or "").strip()
            if provider_email:
                table = collection.name
                email_result = await conn.execute(
                    text(f'SELECT "id" FROM "{table}" WHERE "email" = :email LIMIT 1'),
                    {"email": provider_email},
                )
                row = email_result.first()
                if row:
                    existing_record_id = str(row[0])

            if existing_record_id:
                record = await get_record(engine, collection, existing_record_id)
                is_new = False
            else:
                # New OAuth2 user - create record
                is_new = True

                # Apply mapped fields from collection options
                mapped_fields = (
                    collection.options.get("oauth2", {}).get("mappedFields", {})
                    if collection.options
                    else {}
                )
                oauth_data = {}
                for provider_field, collection_field in mapped_fields.items():
                    if collection_field and provider_user_info.get(provider_field):
                        oauth_data[collection_field] = provider_user_info[provider_field]

                # Merge createData with OAuth data (createData takes precedence)
                record_data = {**oauth_data, **create_data}

                # OAuth providers usually return email; use it as fallback for auth collections.
                if not record_data.get("email") and provider_user_info.get("email"):
                    record_data["email"] = provider_user_info["email"]

                # Auth collection creation requires password/passwordConfirm.
                # Generate a random one for OAuth-only users when missing.
                if not record_data.get("password"):
                    generated_password = generate_token_key(30)
                    record_data["password"] = generated_password
                    record_data["passwordConfirm"] = generated_password

                # Set verified=true for OAuth2 users
                record_data["verified"] = True
                if "emailVisibility" not in record_data:
                    record_data["emailVisibility"] = False

                # Create user record
                record = await create_record(engine, collection, record_data)

            # Create external auth link for newly linked/created records.
            if record is not None:
                from ppbase.core.id_generator import generate_id

                external_auth_data = {
                    "id": generate_id(),
                    "collection_id": collection.id,
                    "record_id": record["id"],
                    "provider": provider_name,
                    "provider_id": provider_user_info["id"],
                }
                await conn.execute(
                    ExternalAuthRecord.__table__.insert().values(**external_auth_data)
                )

        if record is None:
            raise ValueError("Failed to resolve OAuth2 auth record.")

    # Build OAuth2 meta
    expiry = None
    if token_data.get("expires_in"):
        expiry_seconds = int(token_data["expires_in"])
        expiry = (
            datetime.now(timezone.utc).timestamp() + expiry_seconds
        )

    meta = {
        "id": provider_user_info.get("id"),
        "name": provider_user_info.get("name"),
        "email": provider_user_info.get("email"),
        "username": provider_user_info.get("username"),
        "avatarURL": provider_user_info.get("avatarURL"),
        "isNew": is_new,
        "accessToken": token_data.get("access_token"),
        "refreshToken": token_data.get("refresh_token"),
        "expiry": expiry,
    }

    return {"record": record, "isNew": is_new, "meta": meta}
