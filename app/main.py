"""FastAPI application: batch orthophoto take-down service.

Endpoints
---------
GET  /health                      service + catalog info
POST /withdrawals                 atomic batch take-down
GET  /images                      catalog query (active by default)
GET  /images/{sheet_no}           one sheet
GET  /withdrawals                 audit trail of committed/rejected batches
POST /admin/reinitialize          rebuild SQLite from the JSON catalog

Interactive docs: /docs (Swagger UI), /redoc.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import config, database
from .schemas import (
    BatchOut,
    ErrorResponse,
    HealthResponse,
    ImageOut,
    WithdrawRequest,
    WithdrawResponse,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.ensure_initialized()
    yield


app = FastAPI(
    title="Orthophoto Take-down Service",
    version="1.0.0",
    description=(
        "Batch withdrawal of redundant orthophotos. A batch is rejected "
        "atomically if any sheet is already offline or if the surviving "
        "imagery of equal-or-finer resolution leaves a coverage gap."
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
