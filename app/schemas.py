"""Pydantic request/response models (OpenAPI / Swagger)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class WithdrawRequest(BaseModel):
    sheet_nos: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Sheet numbers to take down in one atomic batch. Duplicates are "
            "ignored. The batch only takes effect when every sheet is active "
            "and remains fully covered afterwards."
        ),
        examples=[["ORTHO-001", "ORTHO-002"]],
    )


class UncoveredRect(BaseModel):
    min_x: float
    min_y: float
    max_x: float
    max_y: float


class WithdrawResponse(BaseModel):
    batch_no: str
    status: str
    withdrawn_sheets: list[str]
    withdrawn_count: int
    committed_at: str


class ErrorResponse(BaseModel):
    detail: str
    failed_sheet: str | None = None
    uncovered_rect: UncoveredRect | None = None


class ImageOut(BaseModel):
    sheet_no: str
    period: str
    resolution_mpp: float
    bbox: list[float]
    area_sqm: float
    file_path: str | None
    is_active: bool


class BatchOut(BaseModel):
    batch_no: str
    status: str
    requested_sheets: list[str]
    reason: str | None
    failed_sheet: str | None
    uncovered_rect: UncoveredRect | None
    created_at: str


class HealthResponse(BaseModel):
    status: str
    db_path: str
    catalog_path: str
    crs: str | None
    units: str | None
    active_images: int
    total_images: int
