"""UTC cron scheduling and retention for native automatic backups."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ppbase.db.system_tables import ParamRecord


logger = logging.getLogger(__name__)

AUTOMATIC_BACKUP_ACTOR = "system:backup-cron"
_AUTOMATIC_FILENAME_PREFIX = "ppbase_auto_"
_SAFE_SLUG_RE = re.compile(r"[^a-z0-9]+")


class BackupCronError(ValueError):
    """Raised when a native-backup cron expression is unsupported."""


def _parse_number(value: str, *, minimum: int, maximum: int) -> int:
    if not value.isdigit():
        raise BackupCronError("cron fields must use numeric values")
    number = int(value)
    if not minimum <= number <= maximum:
        raise BackupCronError(
            f"cron value must be between {minimum} and {maximum}"
        )
    return number


def _parse_field(
    raw: str,
    *,
    minimum: int,
    maximum: int,
    sunday_alias: bool = False,
) -> tuple[frozenset[int], bool]:
    text = str(raw or "").strip()
    if not text:
        raise BackupCronError("cron fields cannot be empty")
    values: set[int] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            raise BackupCronError("cron lists cannot contain empty entries")
        base, separator, step_text = token.partition("/")
        if separator:
            step = _parse_number(step_text, minimum=1, maximum=maximum - minimum + 1)
        else:
            step = 1
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _parse_number(start_text, minimum=minimum, maximum=maximum)
            end = _parse_number(end_text, minimum=minimum, maximum=maximum)
            if start > end:
                raise BackupCronError("cron ranges must be ascending")
        else:
            start = end = _parse_number(base, minimum=minimum, maximum=maximum)
        for value in range(start, end + 1, step):
            values.add(0 if sunday_alias and value == 7 else value)
    if not values:
        raise BackupCronError("cron field selects no values")
    full_range = {
        0 if sunday_alias and value == 7 else value
        for value in range(minimum, maximum + 1)
    }
    wildcard = values == full_range
    return frozenset(values), wildcard


@dataclass(frozen=True, slots=True)
class BackupCronSchedule:
    """Strict five-field cron matcher evaluated in UTC at minute precision."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_wildcard: bool
    weekdays_wildcard: bool

    @classmethod
    def parse(cls, expression: str) -> "BackupCronSchedule":
        fields = str(expression or "").split()
        if len(fields) != 5:
            raise BackupCronError("backup cron must contain exactly five fields")
        minutes, _ = _parse_field(fields[0], minimum=0, maximum=59)
        hours, _ = _parse_field(fields[1], minimum=0, maximum=23)
        days, days_wildcard = _parse_field(fields[2], minimum=1, maximum=31)
        months, _ = _parse_field(fields[3], minimum=1, maximum=12)
        weekdays, weekdays_wildcard = _parse_field(
            fields[4],
            minimum=0,
            maximum=7,
            sunday_alias=True,
        )
        return cls(
            minutes=minutes,
            hours=hours,
            days=days,
            months=months,
            weekdays=weekdays,
            days_wildcard=days_wildcard,
            weekdays_wildcard=weekdays_wildcard,
        )

    def matches(self, value: datetime) -> bool:
        selected = value.astimezone(UTC)
        if (
            selected.minute not in self.minutes
            or selected.hour not in self.hours
            or selected.month not in self.months
        ):
            return False
        day_matches = selected.day in self.days
        cron_weekday = (selected.weekday() + 1) % 7
        weekday_matches = cron_weekday in self.weekdays
        if self.days_wildcard or self.weekdays_wildcard:
            return day_matches and weekday_matches
        # Traditional cron treats restricted day-of-month and day-of-week as
        # alternatives rather than requiring both to match.
        return day_matches or weekday_matches


@dataclass(frozen=True, slots=True)
class AutomaticBackupSettings:
    cron: str
    max_keep: int
    app_name: str


async def load_automatic_backup_settings(
    engine: AsyncEngine,
) -> AutomaticBackupSettings:
    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        value = (
            await session.execute(
                select(ParamRecord.value).where(ParamRecord.key == "settings")
            )
        ).scalar_one_or_none()
        await session.rollback()
    payload = value if isinstance(value, dict) else {}
    raw_backups = payload.get("backups")
    backups = raw_backups if isinstance(raw_backups, dict) else {}
    raw_meta = payload.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    cron = str(backups.get("cron", "") or "").strip()
    try:
        max_keep = int(backups.get("cronMaxKeep", 3))
    except (TypeError, ValueError):
        max_keep = 3
    max_keep = min(10_000, max(0, max_keep))
    app_name = str(meta.get("appName", "") or "PPBase").strip() or "PPBase"
    return AutomaticBackupSettings(
        cron=cron,
        max_keep=max_keep,
        app_name=app_name,
    )


def automatic_backup_filename(app_name: str, minute: datetime) -> str:
    slug = _SAFE_SLUG_RE.sub("_", str(app_name).casefold()).strip("_")[:48]
    if not slug:
        slug = "ppbase"
    selected = minute.astimezone(UTC).replace(second=0, microsecond=0)
    return (
        f"{_AUTOMATIC_FILENAME_PREFIX}{slug}_"
        f"{selected.strftime('%Y%m%dT%H%M00Z')}.zip"
    )


async def _prune_automatic_backups(service: Any, max_keep: int) -> None:
    if max_keep == 0:
        return
    archives = await asyncio.to_thread(service.archive_store.list)
    automatic = [
        archive
        for archive in archives
        if archive.key.startswith(_AUTOMATIC_FILENAME_PREFIX)
    ]
    automatic.sort(
        key=lambda item: item.modified,
        reverse=True,
    )
    for archive in automatic[max_keep:]:
        try:
            await service.delete_local_backup(archive.key)
        except Exception:
            logger.exception(
                "Failed to prune automatic native backup %s",
                archive.key,
            )


async def create_due_automatic_backup(
    engine: AsyncEngine,
    runtime_settings: Any,
    config: AutomaticBackupSettings,
    minute: datetime,
    *,
    service_factory: Callable[[AsyncEngine, Any], Any] | None = None,
) -> None:
    if service_factory is None:
        from ppbase.backup.service import NativeBackupService

        service_factory = NativeBackupService
    filename = automatic_backup_filename(config.app_name, minute)
    service = service_factory(engine, runtime_settings)
    try:
        await service.create_local_backup(
            actor_id=AUTOMATIC_BACKUP_ACTOR,
            transport_filename=filename,
        )
        await _prune_automatic_backups(service, config.max_keep)
    finally:
        service.close()


async def run_automatic_backup_scheduler(
    engine: AsyncEngine,
    runtime_settings: Any,
    *,
    poll_seconds: float = 20.0,
) -> None:
    """Poll durable settings and run each matching UTC minute at most once."""
    last_minute: datetime | None = None
    last_invalid_cron: str | None = None
    while True:
        try:
            config = await load_automatic_backup_settings(engine)
            minute = datetime.now(UTC).replace(second=0, microsecond=0)
            if config.cron and minute != last_minute:
                try:
                    schedule = BackupCronSchedule.parse(config.cron)
                except BackupCronError:
                    if config.cron != last_invalid_cron:
                        logger.exception(
                            "Ignoring invalid automatic native-backup cron expression"
                        )
                        last_invalid_cron = config.cron
                else:
                    last_invalid_cron = None
                    if schedule.matches(minute):
                        try:
                            await create_due_automatic_backup(
                                engine,
                                runtime_settings,
                                config,
                                minute,
                            )
                        except Exception:
                            logger.exception(
                                "Automatic native backup failed for UTC minute %s",
                                minute.isoformat(),
                            )
                last_minute = minute
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic native-backup scheduler iteration failed")
        await asyncio.sleep(max(1.0, float(poll_seconds)))
