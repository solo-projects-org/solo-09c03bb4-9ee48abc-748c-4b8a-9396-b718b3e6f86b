"""FastAPI application: batch orthophoto take-down + image-selection service.

Endpoints
---------
GET  /health                      service + catalog info
POST /withdrawals                 atomic batch take-down
GET  /images                      catalog query (active by default)
GET  /images/{sheet_no}           one sheet
GET  /withdrawals                 audit trail of committed/rejected batches
POST /admin/reinitialize          rebuild SQLite from the JSON catalog
POST /manifests                   register an image-selection manifest (取图清单)
GET  /manifests                   recent manifests
GET  /manifests/{manifest_no}     replay a stored manifest
POST /manifests/{manifest_no}/review   compare manifest with current catalog
GET  /manifests/{manifest_no}/reviews  review history of one manifest
GET  /reviews/{review_no}         one stored review

Interactive docs: /docs (Swagger UI), /redoc.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import config, database
from .geometry import GeometryError, Rect
from .selection import SelectionError
from .schemas import (
    BatchOut,
    ChangedImageOut,
    ChangedRegionOut,
    ChangedTileOut,
    ErrorResponse,
    HealthResponse,
    ImageOut,
    ManifestOut,
    ManifestRequest,
    ManifestSummaryOut,
    ReviewOut,
    TileSelectionOut,
    WithdrawRequest,
    WithdrawResponse,
    ZoneOut,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.ensure_initialized()
    yield


app = FastAPI(
    title="Orthophoto Take-down & Image-Selection Service",
    version="1.1.0",
    description=(
        "Batch withdrawal of redundant orthophotos, plus take-off manifests "
        "('取图清单') that state, per tile, which on-shelf image of a survey "
        "period serves each part (finest m/px wins, ties by ascending sheet "
        "number; blanks are explicit). Manifests are replayable by number and "
        "can be compared with the current catalog to list only the tiles "
        "that need re-cutting."
    ),
    lifespan=lifespan,
)


def _image_out(rec: database.ImageRecord) -> ImageOut:
    r = rec.rect
    return ImageOut(
        sheet_no=rec.sheet_no,
        period=rec.period,
        resolution_mpp=rec.resolution,
        bbox=[r.min_x, r.min_y, r.max_x, r.max_y],
        area_sqm=r.area,
        file_path=rec.file_path,
        is_active=rec.is_active,
    )


@app.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    meta = database.catalog_metadata()
    active = len(database.list_images(active_only=True))
    total = len(database.list_images(active_only=False))
    return HealthResponse(
        status="ok",
        db_path=str(config.db_path()),
        catalog_path=str(config.catalog_path()),
        crs=meta.get("crs"),
        units=meta.get("units"),
        active_images=active,
        total_images=total,
    )


@app.post(
    "/withdrawals",
    response_model=WithdrawResponse,
    status_code=200,
    tags=["withdrawals"],
    responses={
        409: {"model": ErrorResponse, "description": "Batch rejected atomically"},
        422: {"description": "Invalid request body"},
    },
)
def withdraw(body: WithdrawRequest) -> WithdrawResponse:
    try:
        result = database.retry_on_locked(
            database.withdraw_batch, body.sheet_nos
        )
    except database.WithdrawalValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": exc.reason,
                "failed_sheet": exc.failed_sheet,
                "uncovered_rect": exc.uncovered_rect,
            },
        )
    except sqlite3.OperationalError as exc:  # lock waited out all retries
        raise HTTPException(status_code=503, detail=f"database busy: {exc}")
    return WithdrawResponse(**result)


@app.get("/images", response_model=list[ImageOut], tags=["catalog"])
def get_images(
    active: bool = Query(
        True, description="true = on-shelf images only; false = full catalog"
    )
) -> list[ImageOut]:
    return [_image_out(rec) for rec in database.list_images(active_only=active)]


@app.get(
    "/images/{sheet_no}",
    response_model=ImageOut,
    tags=["catalog"],
    responses={404: {"model": ErrorResponse}},
)
def get_image(sheet_no: str) -> ImageOut:
    rec = database.get_image(sheet_no)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"sheet '{sheet_no}' not found")
    return _image_out(rec)


@app.get("/withdrawals", response_model=list[BatchOut], tags=["withdrawals"])
def get_batches(
    limit: int = Query(50, ge=1, le=500, description="most recent N batches")
) -> list[BatchOut]:
    out: list[BatchOut] = []
    for row in database.list_batches(limit):
        rect = json.loads(row["uncovered_rect"]) if row["uncovered_rect"] else None
        out.append(
            BatchOut(
                batch_no=row["batch_no"],
                status=row["status"],
                requested_sheets=json.loads(row["requested_sheets"]),
                reason=row["reason"],
                failed_sheet=row["failed_sheet"],
                uncovered_rect=rect,
                created_at=row["created_at"],
            )
        )
    return out


@app.post("/admin/reinitialize", tags=["system"])
def reinitialize(
    force: bool = Query(
        False, description="true: drop and rebuild the catalog from JSON"
    )
) -> JSONResponse:
    try:
        return JSONResponse(database.initialize_database(force=force))
    except (database.CatalogError, FileNotFoundError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"bad catalog: {exc}")


# ---------------------------------------------------------------------------
# Image-selection manifests ("取图清单")
# ---------------------------------------------------------------------------

def _zone_out(zone) -> ZoneOut:
    return ZoneOut(
        sheet_no=zone.sheet_no,
        resolution_mpp=zone.resolution_mpp,
        file_path=zone.file_path,
        bbox=[
            zone.rect.min_x, zone.rect.min_y,
            zone.rect.max_x, zone.rect.max_y,
        ],
        area_sqm=zone.rect.area,
        is_blank=zone.is_blank,
    )


def _manifest_out(
    manifest: database.ManifestRecord,
    tiles: list[database.ManifestTileRecord],
) -> ManifestOut:
    return ManifestOut(
        manifest_no=manifest.manifest_no,
        period=manifest.period,
        crs=manifest.crs,
        units=manifest.units,
        tile_count=manifest.tile_count,
        created_at=manifest.created_at,
        tiles=[
            TileSelectionOut(
                tile_no=tile.tile_no,
                bbox=[
                    tile.rect.min_x, tile.rect.min_y,
                    tile.rect.max_x, tile.rect.max_y,
                ],
                area_sqm=tile.rect.area,
                is_blank=tile.is_blank,
                blank_area_sqm=tile.blank_area_sqm,
                zones=[_zone_out(zone) for zone in tile.zones],
            )
            for tile in tiles
        ],
    )


def _parse_tiles(body: ManifestRequest) -> list[database.TileInput]:
    seen: set[str] = set()
    parsed: list[database.TileInput] = []
    for item in body.tiles:
        if item.tile_no in seen:
            raise HTTPException(
                status_code=422,
                detail=f"duplicate tile_no in request: {item.tile_no}",
            )
        seen.add(item.tile_no)
        min_x, min_y, max_x, max_y = (float(v) for v in item.bbox)
        try:
            rect = Rect(min_x, min_y, max_x, max_y)
        except GeometryError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"tile '{item.tile_no}': {exc}",
            )
        parsed.append(database.TileInput(tile_no=item.tile_no, rect=rect))
    if len(parsed) > config.MAX_MANIFEST_TILES:
        raise HTTPException(
            status_code=422,
            detail=(
                "too many tiles in one manifest "
                f"(limit {config.MAX_MANIFEST_TILES})"
            ),
        )
    return parsed


@app.post(
    "/manifests",
    response_model=ManifestOut,
    status_code=201,
    tags=["manifests"],
    responses={
        422: {"description": "Invalid tile or period"},
        503: {"model": ErrorResponse, "description": "Database busy"},
    },
)
def create_manifest(body: ManifestRequest) -> ManifestOut:
    tiles = _parse_tiles(body)
    try:
        manifest, stored_tiles = database.retry_on_locked(
            database.create_manifest, body.period, tiles
        )
    except SelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except database.UnknownPeriod as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database busy: {exc}")
    return _manifest_out(manifest, stored_tiles)


@app.get(
    "/manifests",
    response_model=list[ManifestSummaryOut],
    tags=["manifests"],
)
def list_manifests(
    limit: int = Query(50, ge=1, le=500),
) -> list[ManifestSummaryOut]:
    return [
        ManifestSummaryOut(
            manifest_no=m.manifest_no,
            period=m.period,
            crs=m.crs,
            units=m.units,
            tile_count=m.tile_count,
            created_at=m.created_at,
        )
        for m in database.list_manifests(limit)
    ]


@app.get(
    "/manifests/{manifest_no}",
    response_model=ManifestOut,
    tags=["manifests"],
    responses={404: {"description": "Unknown manifest number"}},
)
def get_manifest(manifest_no: str) -> ManifestOut:
    loaded = database.get_manifest(manifest_no)
    if loaded is None:
        raise HTTPException(
            status_code=404, detail=f"manifest '{manifest_no}' not found"
        )
    manifest, tiles = loaded
    return _manifest_out(manifest, tiles)


def _review_out(review: dict[str, Any]) -> ReviewOut:
    return ReviewOut(
        review_no=review["review_no"],
        manifest_no=review["manifest_no"],
        status=review["status"],
        comparable=review["comparable"],
        crs=review["crs"],
        units=review["units"],
        message=review["message"],
        changed_sheets=review["changed_sheets"],
        candidate_tiles=review["candidate_tiles"],
        recomputed_tile_count=review["recomputed_tile_count"],
        changed_tile_count=review["changed_tile_count"],
        changed_tiles=[
            ChangedTileOut(
                tile_no=tile["tile_no"],
                bbox=tile["bbox"],
                changed_area_sqm=tile["changed_area_sqm"],
                changed_regions=[
                    ChangedRegionOut(
                        bbox=region["bbox"],
                        area_sqm=region["area_sqm"],
                        old=(
                            ChangedImageOut(**region["old"])
                            if region["old"] is not None
                            else None
                        ),
                        new=(
                            ChangedImageOut(**region["new"])
                            if region["new"] is not None
                            else None
                        ),
                    )
                    for region in tile["changed_regions"]
                ],
            )
            for tile in review["changed_tiles"]
        ],
        reviewed_at=review["reviewed_at"],
    )


@app.post(
    "/manifests/{manifest_no}/review",
    response_model=ReviewOut,
    tags=["manifests"],
    responses={
        404: {"description": "Unknown manifest number"},
        409: {"description": "Catalog CRS/units changed; comparison impossible"},
        503: {"model": ErrorResponse, "description": "Database busy"},
    },
)
def review_manifest(manifest_no: str) -> ReviewOut:
    try:
        review = database.retry_on_locked(
            database.review_manifest, manifest_no
        )
    except database.ManifestNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except SelectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database busy: {exc}")
    if not review["comparable"]:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": review["message"],
                "manifest_no": manifest_no,
                "review_no": review["review_no"],
                "status": review["status"],
            },
        )
    return _review_out(review)


@app.get(
    "/manifests/{manifest_no}/reviews",
    response_model=list[ReviewOut],
    tags=["manifests"],
    responses={404: {"description": "Unknown manifest number"}},
)
def list_manifest_reviews(
    manifest_no: str,
    limit: int = Query(50, ge=1, le=500),
) -> list[ReviewOut]:
    if database.get_manifest(manifest_no) is None:
        raise HTTPException(
            status_code=404, detail=f"manifest '{manifest_no}' not found"
        )
    return [
        _review_out(r)
        for r in database.list_reviews(manifest_no, limit)
    ]


@app.get(
    "/reviews/{review_no}",
    response_model=ReviewOut,
    tags=["manifests"],
    responses={404: {"description": "Unknown review number"}},
)
def get_review(review_no: str) -> ReviewOut:
    review = database.get_review(review_no)
    if review is None:
        raise HTTPException(
            status_code=404, detail=f"review '{review_no}' not found"
        )
    return _review_out(review)
