from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ppbase.backup.automation import (
    AUTOMATIC_BACKUP_ACTOR,
    AutomaticBackupSettings,
    BackupCronError,
    BackupCronSchedule,
    automatic_backup_filename,
    create_due_automatic_backup,
)


@pytest.mark.parametrize(
    ("expression", "instant", "expected"),
    [
        ("*/15 * * * *", datetime(2026, 7, 18, 12, 30, tzinfo=UTC), True),
        ("*/15 * * * *", datetime(2026, 7, 18, 12, 31, tzinfo=UTC), False),
        ("0 3 * * 0", datetime(2026, 7, 19, 3, 0, tzinfo=UTC), True),
        ("0 3 18 * 1", datetime(2026, 7, 18, 3, 0, tzinfo=UTC), True),
        ("0 3 18 * 1", datetime(2026, 7, 20, 3, 0, tzinfo=UTC), True),
    ],
)
def test_backup_cron_schedule_matches_standard_utc_minutes(
    expression: str,
    instant: datetime,
    expected: bool,
) -> None:
    assert BackupCronSchedule.parse(expression).matches(instant) is expected


@pytest.mark.parametrize("expression", ["", "* * *", "60 * * * *", "*/0 * * * *"])
def test_backup_cron_schedule_rejects_invalid_expressions(expression: str) -> None:
    with pytest.raises(BackupCronError):
        BackupCronSchedule.parse(expression)


def test_automatic_backup_filename_is_deterministic_and_safe() -> None:
    assert automatic_backup_filename(
        "Démo / Finance",
        datetime(2026, 7, 18, 12, 34, 56, tzinfo=UTC),
    ) == "ppbase_auto_d_mo_finance_20260718T123400Z.zip"


@pytest.mark.asyncio
async def test_due_automatic_backup_reuses_native_engine_and_closes_service() -> None:
    calls: list[dict[str, object]] = []

    class FakeStore:
        @staticmethod
        def list_sets():
            return []

    class FakeService:
        store = FakeStore()

        async def create_local_backup(self, **kwargs):
            calls.append(kwargs)

        async def delete_local_backup(self, _backup_id):
            raise AssertionError("retention should have nothing to delete")

        def close(self):
            calls.append({"closed": True})

    await create_due_automatic_backup(
        object(),  # type: ignore[arg-type]
        object(),
        AutomaticBackupSettings(
            cron="0 * * * *",
            max_keep=3,
            app_name="PPBase",
        ),
        datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
        service_factory=lambda _engine, _settings: FakeService(),
    )

    assert calls == [
        {
            "actor_id": AUTOMATIC_BACKUP_ACTOR,
            "transport_filename": "ppbase_auto_ppbase_20260718T120000Z.zip",
        },
        {"closed": True},
    ]
