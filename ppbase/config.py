"""PPBase configuration using pydantic-settings."""

from __future__ import annotations

import secrets
import string
from typing import ClassVar

from pydantic_settings import BaseSettings


def _generate_jwt_secret() -> str:
    """Generate a cryptographically secure JWT secret."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(64))


class Settings(BaseSettings):
    """PPBase configuration.

    All values can be set via environment variables with the ``PPBASE_`` prefix.
    For example ``PPBASE_DATABASE_URL`` sets ``database_url``.
    """

    # ---- Database ----
    database_url: str = "postgresql+asyncpg://ppbase:ppbase@localhost:5433/ppbase"
    pool_size: int = 10
    max_overflow: int = 20

    # ---- Server ----
    host: str = "0.0.0.0"
    port: int = 8090
    dev: bool = False

    # ---- Storage ----
    data_dir: str = "./pb_data"
    storage_backend: str = "local"  # "local" or "s3"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""

    # ---- Auth ----
    jwt_secret: str = ""
    admin_token_duration: int = 1_209_600  # 14 days in seconds
    record_token_duration: int = 604_800  # 7 days in seconds
    verification_token_duration: int = 259_200  # 3 days in seconds
    password_reset_token_duration: int = 1_800  # 30 minutes in seconds

    # ---- SMTP (optional — if not configured, tokens are logged) ----
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@ppbase.local"

    # ---- CORS ----
    origins: list[str] = ["*"]

    # ---- Migrations ----
    auto_migrate: bool = True
    migrations_dir: str = "./pb_migrations"

    # ---- Misc ----
    log_level: str = "INFO"
    max_request_body_size: int = 5_242_880  # 5 MB

    # ---- Internal ----
    _resolved_jwt_secret: ClassVar[str] = ""

    model_config = {
        "env_prefix": "PPBASE_",
        "env_file": ".env",
    }

    def get_jwt_secret(self) -> str:
        """Return the JWT secret, auto-generating one if not configured."""
        if self.jwt_secret:
            return self.jwt_secret
        # Generate once per process and cache on the class
        if not Settings._resolved_jwt_secret:
            Settings._resolved_jwt_secret = _generate_jwt_secret()
        return Settings._resolved_jwt_secret
