"""Pydantic models for the Collections API request/response cycle."""

from __future__ import annotations

import warnings
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator

# Suppress Pydantic warning about "schema" field shadowing BaseModel.schema()
# (deprecated v1 compat method).  We need "schema" for PocketBase API compat.
warnings.filterwarnings("ignore", message='Field name "schema".*shadows an attribute')


# ---------------------------------------------------------------------------
# Field format conversion
# ---------------------------------------------------------------------------

# Keys that belong at the top level of a FieldDefinition (everything else
# is treated as a type-specific option and should be nested under "options").
_FIELD_CORE_KEYS = frozenset({
    "id", "name", "type", "required", "system", "hidden", "presentable", "options",
})


def _fields_to_schema(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert v0.23+ flat field definitions to internal schema format.

    In v0.23+ format, type-specific options (``maxSelect``, ``values``,
    ``min``, ``max``, etc.) sit at the top level of each field dict.  The
    internal format expected by ``FieldDefinition`` and ``schema_manager``
    nests them under an ``options`` key.
    """
    result: list[dict[str, Any]] = []
    for field_def in fields:
        core: dict[str, Any] = {}
        opts: dict[str, Any] = {}
        for k, v in field_def.items():
            if k in _FIELD_CORE_KEYS:
                core[k] = v
            else:
                opts[k] = v
        # Merge any existing options dict with extras found at top level
        existing_opts = core.pop("options", None)
        if isinstance(existing_opts, dict):
            # Existing nested options take precedence
            merged = {**opts, **existing_opts}
        else:
            merged = opts
        if merged:
            core["options"] = merged
        result.append(core)
    return result


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CollectionCreate(BaseModel):
    """Body for ``POST /api/collections``.

    Accepts both ``schema`` (legacy) and ``fields`` (PocketBase v0.23+)
    for the field definitions.
    """

    id: str | None = None
    name: str
    type: str = "base"  # "base" | "auth" | "view"
    system: bool = False
    schema: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] | None = None
    indexes: list[str] = Field(default_factory=list)
    list_rule: str | None = Field(default=None, alias="listRule")
    view_rule: str | None = Field(default=None, alias="viewRule")
    create_rule: str | None = Field(default=None, alias="createRule")
    update_rule: str | None = Field(default=None, alias="updateRule")
    delete_rule: str | None = Field(default=None, alias="deleteRule")
    options: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True, "protected_namespaces": ()}

    @model_validator(mode="after")
    def _merge_fields_into_schema(self) -> CollectionCreate:
        """If ``fields`` is provided but ``schema`` is empty, use ``fields``.

        Converts v0.23+ flat format to internal nested-options format.
        Always normalises ``schema`` through ``_fields_to_schema`` so that
        flat-format options (``min``, ``max``, ``values``, etc.) sent
        directly in ``schema`` are properly nested under ``options``.
        """
        if self.fields and not self.schema:
            self.schema = _fields_to_schema(self.fields)
        elif self.schema:
            self.schema = _fields_to_schema(self.schema)
        return self


class CollectionUpdate(BaseModel):
    """Body for ``PATCH /api/collections/{idOrName}``."""

    name: str | None = None
    type: str | None = None
    system: bool | None = None
    schema: list[dict[str, Any]] | None = None
    fields: list[dict[str, Any]] | None = None
    indexes: list[str] | None = None
    list_rule: str | None = Field(default=None, alias="listRule")
    view_rule: str | None = Field(default=None, alias="viewRule")
    create_rule: str | None = Field(default=None, alias="createRule")
    update_rule: str | None = Field(default=None, alias="updateRule")
    delete_rule: str | None = Field(default=None, alias="deleteRule")
    options: dict[str, Any] | None = None

    model_config = {"populate_by_name": True, "protected_namespaces": ()}

    @model_validator(mode="after")
    def _merge_fields_into_schema(self) -> CollectionUpdate:
        """If ``fields`` is provided but ``schema`` is not, use ``fields``.

        Converts v0.23+ flat format to internal nested-options format.
        Always normalises ``schema`` through ``_fields_to_schema`` so that
        flat-format options are properly nested under ``options``.
        """
        if self.fields is not None and self.schema is None:
            self.schema = _fields_to_schema(self.fields)
        elif self.schema is not None:
            self.schema = _fields_to_schema(self.schema)
        return self


class CollectionImportPayload(BaseModel):
    """Body for ``PUT /api/collections/import``."""

    collections: list[dict[str, Any]]
    deleteMissing: bool = False


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


def _fmt_dt(dt: datetime | None) -> str:
    """Format a datetime to PocketBase's API string format."""
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _schema_to_fields(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal schema format to PocketBase v0.23+ ``fields`` format.

    In the internal format, type-specific options are nested under an
    ``options`` key.  PocketBase v0.23+ flattens these to the top level
    of each field definition.  Standard PocketBase field properties
    (``hidden``, ``presentable``, ``system``, ``required``) are ensured.
    """
    result: list[dict[str, Any]] = []
    for field_def in schema:
        flat: dict[str, Any] = {}
        for k, v in field_def.items():
            if k == "options" and isinstance(v, dict):
                # Flatten options to top level
                flat.update(v)
            else:
                flat[k] = v
        # Ensure standard PocketBase field properties have defaults
        flat.setdefault("hidden", False)
        flat.setdefault("presentable", False)
        flat.setdefault("system", False)
        flat.setdefault("required", False)
        result.append(flat)
    return result


class CollectionResponse(BaseModel):
    """Single collection representation returned by the API.

    Returns both ``schema`` (internal nested-options format, for SDK
    compatibility) and ``fields`` (PocketBase v0.23+ flat format).
    Many PocketBase SDKs iterate over ``collection.schema`` so it
    must always be present as a list.
    """

    id: str
    name: str
    type: str
    system: bool
    schema: list[dict[str, Any]] = Field(default_factory=list)
    fields: list[dict[str, Any]] = Field(default_factory=list)
    indexes: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    listRule: str | None = None
    viewRule: str | None = None
    createRule: str | None = None
    updateRule: str | None = None
    deleteRule: str | None = None
    created: str = ""
    updated: str = ""

    model_config = {"protected_namespaces": ()}

    @classmethod
    def from_record(cls, record: Any) -> CollectionResponse:
        """Build a response from a ``CollectionRecord`` ORM instance."""
        raw_schema = record.schema if isinstance(record.schema, list) else []
        raw_options = record.options if isinstance(record.options, dict) else {}
        return cls(
            id=record.id,
            name=record.name,
            type=record.type,
            system=record.system,
            schema=raw_schema,
            fields=_schema_to_fields(raw_schema),
            indexes=record.indexes if isinstance(record.indexes, list) else [],
            options=raw_options,
            listRule=record.list_rule,
            viewRule=record.view_rule,
            createRule=record.create_rule,
            updateRule=record.update_rule,
            deleteRule=record.delete_rule,
            created=_fmt_dt(record.created),
            updated=_fmt_dt(record.updated),
        )


class CollectionListResponse(BaseModel):
    """Paginated list of collections."""

    page: int
    perPage: int
    totalItems: int
    totalPages: int
    items: list[CollectionResponse]
