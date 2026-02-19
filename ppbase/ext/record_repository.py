"""Repository-style record access helpers for extensions and hooks."""

from __future__ import annotations

from typing import Any

from ppbase.db.engine import get_engine
from ppbase.services.record_service import (
    create_record,
    delete_record,
    get_all_collections,
    get_record,
    list_records,
    resolve_collection,
    update_record,
)


class RecordRepository:
    """Small async repository for one collection."""

    def __init__(self, collection_id_or_name: str, *, engine: Any | None = None) -> None:
        target = str(collection_id_or_name or "").strip()
        if not target:
            raise ValueError("collection_id_or_name cannot be empty.")
        self.collection_id_or_name = target
        self._engine = engine

    @property
    def engine(self) -> Any:
        return self._engine or get_engine()

    async def resolve_collection(self):
        collection = await resolve_collection(self.engine, self.collection_id_or_name)
        if collection is None:
            raise ValueError(
                f"Missing collection with id or name '{self.collection_id_or_name}'."
            )
        return collection

    async def get(self, record_id: str, *, fields: str | None = None):
        collection = await self.resolve_collection()
        return await get_record(self.engine, collection, str(record_id), fields=fields)

    async def list(
        self,
        *,
        page: int = 1,
        per_page: int = 30,
        sort: str | None = None,
        filter: str | None = None,
        fields: str | None = None,
        skip_total: bool = False,
        request_context: dict[str, Any] | None = None,
    ):
        collection = await self.resolve_collection()
        return await list_records(
            self.engine,
            collection,
            page=page,
            per_page=per_page,
            sort=sort,
            filter_str=filter,
            fields=fields,
            skip_total=skip_total,
            request_context=request_context,
        )

    async def create(
        self,
        data: dict[str, Any],
        *,
        files: dict[str, list[tuple[str, bytes]]] | None = None,
    ):
        collection = await self.resolve_collection()
        return await create_record(self.engine, collection, dict(data), files=files)

    async def update(
        self,
        record_id: str,
        data: dict[str, Any],
        *,
        files: dict[str, list[tuple[str, bytes]]] | None = None,
    ):
        collection = await self.resolve_collection()
        return await update_record(
            self.engine,
            collection,
            str(record_id),
            dict(data),
            files=files,
        )

    async def delete(self, record_id: str) -> bool:
        collection = await self.resolve_collection()
        all_collections = await get_all_collections(self.engine)
        return await delete_record(
            self.engine,
            collection,
            str(record_id),
            all_collections=all_collections,
        )
