# Changelog

All notable changes to PPBase are documented in this file.

## 0.3.3

### Read paths and collection rules

- `?expand=` responses now respect the target collection's `listRule`: an
  expanded relation is only returned when the caller is allowed to read that
  record. Applies to record responses and to auth responses.
- `/api/files/<collection>/<record>/<filename>` resolves the record through the
  same rule evaluation before serving the file, and returns `404` when the
  record is not readable by the caller.
- `@collection.<name>.field` references used in filters are resolved against
  that collection's `listRule`, without going around it.

### Filters and dates

- Date and datetime literals compared with `date` / `autodate` fields are parsed
  strictly. An invalid literal is reported as `400` with a clear message instead
  of failing with a server error.
- `= ""` and `!= ""` treat `NULL` as empty, matching PocketBase behaviour.
- Filter strings accept escaped quotes.
- Unknown filter/sort fields answer `400` for superusers as well.

### Records and collections

- `PATCH` on a record no longer removes a file field when the field is absent
  from the payload; removal requires the explicit `_toDelete` marker.
- `PATCH` on a collection merges the supplied `fields` instead of replacing the
  whole list.

### Auth

- Record auth tokens issued by PocketBase (>= 0.23) are accepted.
- New installations start with the auth attempt limit active. Existing
  installations keep their stored setting; the limit can be switched on in the
  settings screen.
- One-time passwords allow at most 5 attempts.

### Admin UI

- Light and dark themes.
- SQL console, with a confirmation step for statements that write.
