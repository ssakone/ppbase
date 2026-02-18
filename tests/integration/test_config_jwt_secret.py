from __future__ import annotations

from ppbase.config import Settings


def test_project_local_jwt_secret_is_persisted_in_data_dir(tmp_path) -> None:
    Settings._resolved_jwt_secret.clear()

    project_dir = tmp_path / "project_a_data"
    settings_a = Settings(data_dir=str(project_dir), jwt_secret="")
    secret_first = settings_a.get_jwt_secret()

    secret_file = project_dir / ".jwt_secret"
    assert secret_file.exists()
    assert len(secret_first) == 64
    assert secret_file.read_text(encoding="utf-8").strip() == secret_first

    # Fresh settings object for the same project directory should reuse it.
    settings_again = Settings(data_dir=str(project_dir), jwt_secret="")
    secret_second = settings_again.get_jwt_secret()
    assert secret_second == secret_first


def test_project_local_jwt_secret_is_isolated_per_data_dir(tmp_path) -> None:
    Settings._resolved_jwt_secret.clear()

    settings_a = Settings(data_dir=str(tmp_path / "project_a_data"), jwt_secret="")
    settings_b = Settings(data_dir=str(tmp_path / "project_b_data"), jwt_secret="")

    secret_a = settings_a.get_jwt_secret()
    secret_b = settings_b.get_jwt_secret()

    assert secret_a != secret_b


def test_explicit_jwt_secret_takes_priority(tmp_path) -> None:
    Settings._resolved_jwt_secret.clear()

    explicit_secret = "explicit-project-secret"
    settings = Settings(data_dir=str(tmp_path / "project_data"), jwt_secret=explicit_secret)

    assert settings.get_jwt_secret() == explicit_secret
    assert not (tmp_path / "project_data" / ".jwt_secret").exists()
