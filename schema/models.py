from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    write_markdown_package: bool = True
    package_name: str | None = None


class SearchResponse(BaseModel):
    query: str
    generated_at: datetime
    total_results: int
    results: list[dict[str, Any]]
    package: dict[str, str] | None = None
    query_id: str | None = None
    timings: dict[str, Any] | None = None
    source_statuses: list[dict[str, Any]] | None = None
