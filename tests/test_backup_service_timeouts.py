from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.pool import NullPool

import ppbase.backup.service as service_module
from ppbase.backup.service import BackupServiceError
from ppbase.config import Settings


def test_restore_cutover_engine_uses_dedicated_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://example.invalid/ppbase",
        backup_restore_connect_timeout=12.5,
        backup_restore_command_timeout=987.0,
    )
    calls: list[tuple[str, dict[str, object]]] = []
    sentinel = object()

    def create_engine(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return sentinel

    monkeypatch.setattr(service_module, "create_async_engine", create_engine)
    connect_args = service_module._restore_engine_connect_args(settings)

    first = service_module._create_restore_cutover_engine(settings, connect_args)
    second = service_module._create_restore_cutover_engine(settings, connect_args)

    assert first is sentinel
    assert second is sentinel
    assert calls == [
        (
            settings.database_url,
            {
                "poolclass": NullPool,
                "connect_args": {
                    "timeout": 12.5,
                    "command_timeout": 987.0,
                },
            },
        ),
        (
            settings.database_url,
            {
                "poolclass": NullPool,
                "connect_args": {
                    "timeout": 12.5,
                    "command_timeout": 987.0,
                },
            },
        ),
    ]


@pytest.mark.parametrize(
    ("connect_timeout", "command_timeout"),
    ((0.0, 1.0), (1.0, 0.0), (float("inf"), 1.0)),
)
def test_restore_cutover_timeouts_must_be_positive_and_finite(
    connect_timeout: float,
    command_timeout: float,
) -> None:
    settings = SimpleNamespace(
        backup_restore_connect_timeout=connect_timeout,
        backup_restore_command_timeout=command_timeout,
    )

    with pytest.raises(BackupServiceError) as error:
        service_module._restore_engine_connect_args(settings)

    assert error.value.code == "backup_restore_timeout_invalid"
