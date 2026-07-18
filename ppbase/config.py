"""PPBase configuration using pydantic-settings."""

from __future__ import annotations

import os
import secrets
import string
from pathlib import Path
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
    public_dir: str | None = None
    storage_backend: str = "local"  # "local" or "s3"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_force_path_style: bool = False

    # ---- Native backup / restore staging ----
    # Canonical sets and control-plane state intentionally live outside the
    # restored business ``data_dir``. Only ``data_dir/storage`` is copied.
    backup_root: str = "./pb_backups"
    backup_control_dir: str = "./pb_backup_control"
    backup_staging_root: str = "./pb_restore_staging"
    backup_barrier_timeout: float = 300.0
    backup_pg_dump_path: str = "pg_dump"
    backup_pg_restore_path: str = "pg_restore"
    backup_psql_path: str = "psql"
    # Read-only source login used only by pg_dump and its privilege preflight.
    # It must be distinct from the writable PPBase runtime login.
    backup_dump_database_url: str = ""
    # Restore staging uses separate cluster roles. The creator DSN is used
    # only against a maintenance DB for CREATE DATABASE; the restore DSN has
    # no CREATEDB and connects to the newly generated target.
    backup_creator_database_url: str = ""
    backup_restore_database_url: str = ""
    backup_target_owner: str = ""
    backup_allowed_extensions: list[str] = ["plpgsql=1.0"]
    # ZIP transport is bounded independently from the ordinary API body
    # setting because database backups are expected to be substantially larger.
    backup_max_upload_bytes: int = 20 * 1024 * 1024 * 1024
    backup_max_uncompressed_bytes: int = 100 * 1024 * 1024 * 1024
    backup_max_resource_bytes: int = 20 * 1024 * 1024 * 1024
    backup_max_archive_entries: int = 200_000
    backup_max_central_directory_bytes: int = 64 * 1024 * 1024
    backup_max_compression_ratio: int = 500
    backup_transport_chunk_size: int = 1024 * 1024
    backup_multipart_overhead_bytes: int = 1024 * 1024

    # ---- Auth ----
    jwt_secret: str = ""
    admin_token_duration: int = 1_209_600  # 14 days in seconds
    record_token_duration: int = 604_800  # 7 days in seconds
    verification_token_duration: int = 259_200  # 3 days in seconds
    password_reset_token_duration: int = 1_800  # 30 minutes in seconds

    # ---- OAuth2 Provider Credentials ----
    oauth2_google_client_id: str = ""
    oauth2_google_client_secret: str = ""
    oauth2_github_client_id: str = ""
    oauth2_github_client_secret: str = ""
    oauth2_gitlab_client_id: str = ""
    oauth2_gitlab_client_secret: str = ""
    oauth2_discord_client_id: str = ""
    oauth2_discord_client_secret: str = ""
    oauth2_facebook_client_id: str = ""
    oauth2_facebook_client_secret: str = ""

    # ---- SMTP (optional — if not configured, tokens are logged) ----
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@ppbase.local"

    # ---- CORS ----
    origins: list[str] = ["*"]

    # ---- Migrations ----
    # ``auto_migrate`` is the legacy PPBase switch that historically
    # controlled both startup application and Dashboard file generation.
    # The two optional settings below separate those concerns while keeping
    # existing deployments backwards compatible when they are unset.
    auto_migrate: bool = True
    apply_migrations_on_start: bool | None = None
    generate_migrations: bool | None = None
    migration_lock_timeout: float = 30.0
    migrations_dir: str = "./pb_migrations"

    # ---- Hooks ----
    hooks_dir: str = "./pb_hooks"

    # ---- Misc ----
    log_level: str = "INFO"
    max_request_body_size: int = 5_242_880  # 5 MB

    # ---- Internal ----
    _resolved_jwt_secret: ClassVar[dict[str, str]] = {}

    model_config = {
        "env_prefix": "PPBASE_",
        "env_file": ".env",
    }

    def get_jwt_secret(self) -> str:
        """Return the JWT secret, auto-generating one if not configured."""
        if self.jwt_secret:
            return self.jwt_secret

        project_data_dir = Path(self.data_dir or "./pb_data").expanduser()
        cache_key = os.path.abspath(str(project_data_dir))

        cached = Settings._resolved_jwt_secret.get(cache_key)
        if cached:
            return cached

        secret_file = project_data_dir / ".jwt_secret"

        # Prefer an already persisted project secret.
        try:
            existing = secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if existing:
            Settings._resolved_jwt_secret[cache_key] = existing
            return existing

        generated = _generate_jwt_secret()

        # Persist the generated secret so tokens remain stable across restarts.
        try:
            project_data_dir.mkdir(parents=True, exist_ok=True)
            if secret_file.exists():
                existing = secret_file.read_text(encoding="utf-8").strip()
                if existing:
                    generated = existing
                else:
                    secret_file.write_text(generated + "\n", encoding="utf-8")
            else:
                secret_file.write_text(generated + "\n", encoding="utf-8")

            try:
                os.chmod(secret_file, 0o600)
            except OSError:
                pass
        except OSError:
            # Fall back to process memory only when filesystem write isn't possible.
            pass

        Settings._resolved_jwt_secret[cache_key] = generated
        return generated

    def should_apply_migrations(self) -> bool:
        """Return whether pending migration files run during startup."""
        if self.apply_migrations_on_start is not None:
            return self.apply_migrations_on_start
        return self.auto_migrate

    def should_generate_migrations(self) -> bool:
        """Return whether collection changes generate migration files."""
        if self.generate_migrations is not None:
            return self.generate_migrations
        return self.auto_migrate
