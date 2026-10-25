"""File field values of a record API write, resolved like PocketBase.

A record write may keep, remove or reorder the file names that the record
already references, and add files only by uploading them.  Both PocketBase
versions refuse any other submitted name:

* v0.22.34 while loading the request, before the form validation and the
  ``OnRecordBefore*Request`` hooks (``forms/record_upsert.go`` l.428-439):
  ``400 "Failed to load the submitted data due to invalid formatting."``
  (``apis/record_crud.go`` l.226-227, l.269-270, l.361-362) with
  ``{field: {"code": "validation_unknown_filenames", "message": "The field
  contains unknown filenames."}}`` for the first such field only;
* v0.40.6 in the record validation (``core/field_file.go`` l.250-269):
  ``validation_invalid_file`` "Invalid new files: [names]." with the
  ``invalidFiles`` params, among the other field errors.

:func:`plan_file_field` computes the final value of one file field from the
current value, the request data and the uploads, with the semantics of the
current PocketBase (``CURRENT``) or of 0.22.34 (``POCKETBASE_022``).  The two
differ beyond the error (see each function).  ``None`` keeps the current
semantics without the name check, for server-side callers of the record
service (an OAuth2 avatar URL, for example), like 0.22.34 ``dao.SaveRecord``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ppbase.models.field_multiplicity import (
    _go_cast_string,
    select_or_file_is_multiple,
    select_or_file_max_select,
    to_unique_string_list,
)

#: ``file_semantics`` of ``create_record`` / ``update_record``.
CURRENT = "current"
POCKETBASE_022 = "0.22"

#: forms/record_upsert.go l.433-438 (v0.22.34).
UNKNOWN_FILENAMES = {
    "code": "validation_unknown_filenames",
    "message": "The field contains unknown filenames.",
}

#: core/field_file.go l.261-264 (v0.40.6): each invalid name is cut to 250 bytes.
_INVALID_NAME_BYTES = 250


@dataclass(frozen=True)
class Upload:
    """The ``index``-th file uploaded under the request key ``key``."""

    key: str
    index: int


@dataclass
class FilePlan:
    """The resolved value of one file field (uploads not saved yet)."""

    name: str
    multiple: bool
    entries: list[str | Upload] = field(default_factory=list)
    #: ``{"code", "message"[, "params"]}`` when the field is refused.
    error: dict[str, Any] | None = None
    #: v0.22.34 refuses the field while loading the request.
    load_error: bool = False

    @property
    def uploads(self) -> list[Upload]:
        return [entry for entry in self.entries if isinstance(entry, Upload)]


def semantics_for_mode(error_compat: str) -> str:
    """The semantics of an app error mode (``"0.22"`` or PPBase's own)."""
    return POCKETBASE_022 if error_compat == POCKETBASE_022 else CURRENT


def server_semantics_for_mode(error_compat: str) -> str | None:
    """The semantics of a server-side write (``pb.records()``, ``e.records()``).

    v0.40.6 ``app.Save`` validates the record (core/db.go l.178-187,
    l.280-286), so ``FileField.ValidateValue`` refuses a name the record does
    not have there too (core/record_model.go l.1427-1433, core/field_file.go
    l.241-269); 0.22.34 ``dao.SaveRecord`` saves without any form validation
    (daos/record.go l.586-610).
    """
    return None if error_compat == POCKETBASE_022 else CURRENT


class MultipartText(str):
    """A multipart text value of a record request, in the 0.22 mode.

    PocketBase 0.22.34 keeps the text values of a file field key as a list of
    strings (forms/record_upsert.go l.191-196) and ``list.ToUniqueStringSlice``
    never JSON-decodes a ``[]string`` (tools/list/list.go l.115-116), so
    ``docs='["a","b"]'`` is one (unknown) name.  Only the exact field key is
    concerned: any other key keeps its first value as a string (l.195), that
    a modifier decodes like JSON text.  Everywhere else it is a ``str``.
    """

    __slots__ = ()


def _entry_key(entry: str | Upload) -> Any:
    return entry if isinstance(entry, str) else ("upload", entry.key, entry.index)


def _unique(entries: list[str | Upload]) -> list[str | Upload]:
    seen: set[Any] = set()
    result: list[str | Upload] = []
    for entry in entries:
        key = _entry_key(entry)
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def _normalize(entries: list[str | Upload], multiple: bool) -> list[str | Upload]:
    """``PrepareValue`` / ``normalizeValue``: unique, a single field keeps the last.

    v0.22.34 models/schema/schema_field.go l.363-374, v0.40.6
    core/field_file.go l.714-726.
    """
    entries = _unique(entries)
    return entries if multiple else entries[-1:]


def _to_slice(value: Any) -> list[str | Upload]:
    """v0.40.6 ``toSliceValue`` (core/field_file.go l.728-762) for request data.

    A list is cast element by element and an element is kept only when it
    gives exactly one name; anything else goes through
    ``list.ToUniqueStringSlice``.
    """
    if isinstance(value, list):
        result: list[str | Upload] = []
        for item in value:
            if isinstance(item, Upload):
                result.append(item)
                continue
            names = to_unique_string_list(item)
            if len(names) == 1:
                result.append(names[0])
        return _unique(result)
    return list(to_unique_string_list(value))


def _exclude(base: list[str | Upload], names: list[str | Upload]) -> list[str | Upload]:
    excluded = {_entry_key(entry) for entry in names}
    return [entry for entry in base if _entry_key(entry) not in excluded]


def _truncate_name(name: str) -> str:
    """``invalidStr[:250]`` on Go bytes; each invalid byte becomes U+FFFD."""
    raw = name.encode("utf-8", "surrogatepass")[:_INVALID_NAME_BYTES]
    text = raw.decode("utf-8", "surrogateescape")
    return "".join("�" if "\udc80" <= char <= "\udcff" else char for char in text)


def _invalid_files_error(names: list[str]) -> dict[str, Any]:
    """core/field_file.go l.257-269 (v0.40.6); ``{{.invalidFiles}}`` prints like Go."""
    invalid = [_truncate_name(name) for name in names]
    return {
        "code": "validation_invalid_file",
        "message": f"Invalid new files: [{' '.join(invalid)}].",
        "params": {"invalidFiles": invalid},
    }


def _plan_current(
    plan: FilePlan,
    old_value: Any,
    data: Mapping[str, Any],
    uploads: Mapping[str, int],
    *,
    check: bool,
    max_select: int,
) -> FilePlan:
    """v0.40.6: ``recordDataFromRequest`` then ``FileField.ValidateValue``.

    apis/record_crud.go l.631-669: the body keys of the field (``f``,
    ``f+``, ``+f``, ``f-``) are applied shortest first to the current value
    (core/record_model.go l.1374-1404, setters core/field_file.go
    l.662-712); files uploaded under ``f`` replace the value, merged after
    the names sent under ``f`` itself, and files under ``f+`` / ``+f`` are
    appended / prepended (l.690-727).  Then only names already in the
    record are accepted, and at most ``maxSelect`` files
    (core/field_file.go l.241-275).
    """
    name, multiple = plan.name, plan.multiple
    value = _normalize(_to_slice(old_value), multiple)
    keys = [key for key in data if key in (name, f"{name}+", f"+{name}", f"{name}-")]
    for key in sorted(keys, key=len):
        submitted = _to_slice(data[key])
        if key == name:
            value = _normalize(submitted, multiple)
        elif key == f"{name}+":
            value = _normalize(value + submitted, multiple)
        elif key == f"+{name}":
            value = _normalize(submitted + value, multiple)
        else:
            value = _normalize(_exclude(value, submitted), multiple)

    if uploads.get(name):
        sent = data.get(name)
        merged: list[str | Upload] = (
            list(to_unique_string_list(sent)) if sent is not None else []
        )
        merged += [Upload(name, index) for index in range(uploads[name])]
        value = _normalize(merged, multiple)
    for key, count in uploads.items():
        if not count or key not in (f"{name}+", f"+{name}"):
            continue
        added = [Upload(key, index) for index in range(count)]
        value = _normalize(
            value + added if key.endswith("+") else added + value, multiple
        )
    plan.entries = value

    if not check:
        return plan
    old_names = set(to_unique_string_list(old_value))
    added_names = [
        entry for entry in value if isinstance(entry, str) and entry not in old_names
    ]
    if added_names:
        plan.error = _invalid_files_error(added_names)
    elif len(value) > max_select:
        plan.error = {
            "code": "validation_too_many_files",
            "message": f"The maximum allowed files is {max_select}.",
            "params": {"maxSelect": max_select},
        }
    return plan


def _plan_022(
    plan: FilePlan,
    old_value: Any,
    data: Mapping[str, Any],
    uploads: Mapping[str, int],
    *,
    max_select: int,
) -> FilePlan:
    """v0.22.34: ``LoadRequest`` then ``AddFiles`` and the form validation.

    ``ReplaceModifers`` (models/record.go l.598-677): ``f+`` leaves a file
    field as is and ``f-`` subtracts names (models/schema/schema_field.go
    l.418-425); ``+f`` is not a modifier (an unknown key, ignored); the
    legacy ``f.<name>`` and ``f.<index>`` keys set to an empty value delete
    a name.  ``LoadData`` (forms/record_upsert.go l.415-466) then refuses a
    name the record does not have and keeps the submitted order.  Only files
    uploaded under ``f`` itself are read (l.199-226); ``AddFiles``
    (l.276-316) replaces a single field with the first upload and appends
    to a multiple one.  ``checkFileValue`` refuses more than ``maxSelect``
    names (forms/validators/record_data.go l.324-334).
    """
    name, multiple = plan.name, plan.multiple
    old = to_unique_string_list(old_value)
    value: Any = data.get(name, old_value)
    if isinstance(value, MultipartText):
        value = [str(value)]
    if f"{name}+" in data:
        value = _normalize(_to_slice_022(value), multiple)
    if f"{name}-" in data:
        value = _normalize(
            _exclude(_to_slice_022(value), _to_slice_022(data[f"{name}-"])), multiple
        )

    base = _to_slice_022(value)
    deleted: list[str | Upload] = []
    consumed: set[str] = set()
    for entry in base:
        key = f"{name}.{entry}"
        if key in data and _go_cast_string(data[key]) == "":
            deleted.append(entry)
            consumed.add(key)
    index_key = re.compile(re.escape(name) + r"\.[0-9]+")
    for key, raw in data.items():
        if key in consumed or not index_key.fullmatch(key):
            continue
        if _go_cast_string(raw) == "":
            index = int(key[len(name) + 1 :])
            if index < len(base):
                deleted.append(base[index])
    if deleted:
        value = _normalize(_exclude(base, deleted), multiple)

    submitted = _normalize(_to_slice_022(value), multiple)
    if len(submitted) > len(old) or any(entry not in old for entry in submitted):
        plan.error = dict(UNKNOWN_FILENAMES)
        plan.load_error = True
        return plan

    entries: list[str | Upload] = list(submitted)
    count = uploads.get(name, 0)
    if count:
        if multiple:
            entries += [Upload(name, index) for index in range(count)]
        else:
            entries = [Upload(name, 0)]
    plan.entries = _unique(entries)
    if len(plan.entries) > max_select:
        plan.error = {
            "code": "validation_too_many_values",
            "message": f"Select no more than {max_select}.",
        }
    return plan


def _to_slice_022(value: Any) -> list[str | Upload]:
    """``list.ToUniqueStringSlice`` (v0.22.34 tools/list/list.go l.111-140)."""
    if isinstance(value, list) and any(isinstance(item, Upload) for item in value):
        return _unique(value)
    return list(to_unique_string_list(value))


def plan_file_field(
    semantics: str | None,
    field_def: Any,
    old_value: Any,
    data: Mapping[str, Any],
    uploads: Mapping[str, int],
) -> FilePlan:
    """Resolve one file field of a create (``old_value`` empty) or update.

    ``data`` is the request data (JSON body, or multipart text values and
    ``@jsonPayload``) and ``uploads`` counts the files of each request key.
    """
    options = getattr(field_def, "options", None) or {}
    plan = FilePlan(
        name=str(field_def.name), multiple=select_or_file_is_multiple(options)
    )
    max_select = select_or_file_max_select(options)
    if semantics == POCKETBASE_022:
        return _plan_022(plan, old_value, data, uploads, max_select=max_select)
    return _plan_current(
        plan,
        old_value,
        data,
        uploads,
        check=semantics == CURRENT,
        max_select=max_select,
    )


def is_touched(name: str, data: Mapping[str, Any], uploads: Mapping[str, int]) -> bool:
    """Whether a request addresses the file field ``name`` at all.

    PPBase only writes the columns an update changes, so an untouched file
    field keeps its stored value as is.
    """
    return bool(field_keys(name, data)) or any(
        uploads.get(key) for key in (name, f"{name}+", f"+{name}", f"{name}-")
    )


def field_keys(name: str, data: Mapping[str, Any]) -> list[str]:
    """The keys of ``data`` that address the field ``name``.

    The value, its modifiers (``f+``, ``+f``, ``f-``) and the legacy
    ``f.<name>`` / ``f.<index>`` deletions.
    """
    keys = {name, f"{name}+", f"+{name}", f"{name}-"}
    prefix = f"{name}."
    return [
        key
        for key in data
        if key in keys or (isinstance(key, str) and key.startswith(prefix))
    ]


#: Keys that 0.22.34 ``LoadData`` reads before the schema fields
#: (forms/record_upsert.go l.367-395), or never reads from a request.
_LOADED_BEFORE_SCHEMA = frozenset(
    {
        "id",
        "username",
        "email",
        "emailVisibility",
        "verified",
        "password",
        "passwordConfirm",
        "oldPassword",
        "created",
        "updated",
    }
)


def load_022_create_data(
    fields: Sequence[Any], data: Mapping[str, Any]
) -> dict[str, Any]:
    """0.22.34 ``LoadData`` of an OAuth2 ``createData``, whose error is ignored.

    forms/record_oauth2_login.go l.227 calls ``createForm.LoadData(
    form.CreateData)`` and drops its error.  ``LoadData`` loads the schema
    fields in order and returns at the first file field that names a file
    the new record does not have (forms/record_upsert.go l.415-439), so that
    field and every schema field after it keep their default value; the
    fields before it and the auth fields (l.367-395) are loaded.  Returns
    ``data`` without the keys of the fields 0.22.34 does not load.
    """
    result = dict(data)
    schema = [
        field_def
        for field_def in fields
        if field_def.name not in _LOADED_BEFORE_SCHEMA
        and getattr(field_def, "type", None) != "autodate"
    ]
    for index, field_def in enumerate(schema):
        if getattr(field_def, "type", None) != "file":
            continue
        if plan_file_field(POCKETBASE_022, field_def, None, data, {}).load_error:
            for skipped in schema[index:]:
                for key in field_keys(skipped.name, result):
                    del result[key]
            break
    return result


def file_value(plan: FilePlan, saved: Mapping[Upload, str]) -> str | list[str]:
    """The stored value of a plan once its uploads are saved."""
    names = [
        entry if isinstance(entry, str) else saved[entry] for entry in plan.entries
    ]
    if plan.multiple:
        return names
    return names[-1] if names else ""


def upload_counts(files: Mapping[str, list[Any]] | None) -> dict[str, int]:
    return {key: len(values) for key, values in (files or {}).items() if values}
