"""Migration configuration compatibility tests."""

from argparse import Namespace
from pathlib import Path

from ppbase.__main__ import _cmd_migrate_create
from ppbase.config import Settings


def test_legacy_auto_migrate_remains_the_fallback() -> None:
    enabled = Settings(auto_migrate=True)
    disabled = Settings(auto_migrate=False)

    assert enabled.should_apply_migrations() is True
    assert enabled.should_generate_migrations() is True
    assert disabled.should_apply_migrations() is False
    assert disabled.should_generate_migrations() is False


def test_explicit_migration_settings_separate_application_and_generation() -> None:
    settings = Settings(
        auto_migrate=True,
        apply_migrations_on_start=True,
        generate_migrations=False,
    )

    assert settings.should_apply_migrations() is True
    assert settings.should_generate_migrations() is False


def test_cli_create_uses_configured_migrations_dir_when_dir_is_omitted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    migrations_dir = tmp_path / "configured_migrations"
    monkeypatch.setenv("PPBASE_MIGRATIONS_DIR", str(migrations_dir))

    _cmd_migrate_create(Namespace(dir=None, name="from_settings"))

    generated = list(migrations_dir.glob("*_from_settings.py"))
    assert len(generated) == 1
