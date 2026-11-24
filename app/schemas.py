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


# ---------------------------------------------------------------------------
# Image-selection manifests ("取图清单")
# ---------------------------------------------------------------------------

class TileIn(BaseModel):
    tile_no: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Caller-assigned tile identifier, unique within one request.",
        examples=["TILE-0001"],
    )
    bbox: list[float] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="[min_x, min_y, max_x, max_y] in the catalog CRS.",
        examples=[[500000, 3000000, 501000, 3001000]],
    )


class ManifestRequest(BaseModel):
    period: str = Field(
        ...,
        min_length=1,
        description="Survey period whose on-shelf imagery is used.",
        examples=["2024Q3"],
    )
    tiles: list[TileIn] = Field(
        ...,
        min_length=1,
        description=(
            "Rectangular tiles to build a take-off manifest for. Blank "
            "areas (no on-shelf image) are reported but never fail a request."
        ),
    )


class ZoneOut(BaseModel):
    sheet_no: str | None = Field(
        None, description="Chosen sheet number; null for a blank zone."
    )
    resolution_mpp: float | None = None
    file_path: str | None = None
    bbox: list[float]
    area_sqm: float
    is_blank: bool


class TileSelectionOut(BaseModel):
    tile_no: str
    bbox: list[float]
    area_sqm: float
    is_blank: bool = Field(
        False, description="True when no on-shelf image covers any part."
    )
    blank_area_sqm: float
    zones: list[ZoneOut]


class ManifestOut(BaseModel):
    manifest_no: str
    period: str
    crs: str
    units: str
    tile_count: int
    created_at: str
    tiles: list[TileSelectionOut]


class ManifestSummaryOut(BaseModel):
    manifest_no: str
    period: str
    crs: str
    units: str
    tile_count: int
    created_at: str


class ChangedImageOut(BaseModel):
    sheet_no: str
    resolution_mpp: float | None = None
    file_path: str | None = None


class ChangedRegionOut(BaseModel):
    bbox: list[float]
    area_sqm: float
    old: ChangedImageOut | None = Field(
        None, description="Previously chosen image; null means blank."
    )
    new: ChangedImageOut | None = Field(
        None, description="Currently chosen image; null means blank."
    )


class ChangedTileOut(BaseModel):
    tile_no: str
    bbox: list[float]
    changed_area_sqm: float
    changed_regions: list[ChangedRegionOut]


class ReviewOut(BaseModel):
    review_no: str
    manifest_no: str
    status: str = Field(
        ...,
        description=(
            "changed | unchanged | not_comparable. "
            "'not_comparable' means the CRS or units changed."
        ),
    )
    comparable: bool
    crs: str
    units: str
    message: str
    changed_sheets: list[str] = Field(
        ..., description="Images added, removed or modified since registration."
    )
    candidate_tiles: list[str] = Field(
        ..., description="Tiles intersecting changed old/new coverage."
    )
    recomputed_tile_count: int
    changed_tile_count: int
    changed_tiles: list[ChangedTileOut]
    reviewed_at: str


class ManifestErrorDetail(BaseModel):
    detail: str
    manifest_no: str | None = None
