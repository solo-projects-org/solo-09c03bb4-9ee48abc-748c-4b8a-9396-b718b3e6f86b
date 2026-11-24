"""SQLite persistence: catalog initialization, queries, take-down transactions.

Concurrency model
-----------------
Each request gets its own connection (SQLite objects are not shared across
threads). Every write runs inside an explicit ``BEGIN IMMEDIATE``
transaction, which takes the database write lock up front. Re-validation of
coverage and the status update therefore happen atomically while no other
writer can commit, so a batch that was valid when checked is still valid
when it lands. Locked-database errors are retried with backoff.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config
from .geometry import GeometryError, Rect, find_uncovered_rectangle
from .selection import (
    CatalogImage,
    Zone,
    affected_tile_nos,
    compare_zones,
    detect_changed_images,
    select_zones,
    selection_for_tile,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_no    TEXT NOT NULL UNIQUE,
    period      TEXT NOT NULL,
    min_x       REAL NOT NULL,
    min_y       REAL NOT NULL,
    max_x       REAL NOT NULL,
    max_y       REAL NOT NULL,
    resolution  REAL NOT NULL,
    file_path   TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS withdrawal_batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no       TEXT NOT NULL UNIQUE,
    status         TEXT NOT NULL CHECK (status IN ('committed', 'rejected')),
    requested_sheets TEXT NOT NULL,
    reason         TEXT,
    failed_sheet   TEXT,
    uncovered_rect TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Immutable catalog snapshots. A manifest records the snapshot that was
-- current when it was registered; a review records the snapshot it compared
-- against, so both sides of a comparison are reproducible after restarts.
CREATE TABLE IF NOT EXISTS catalog_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_snapshot_images (
    snapshot_id INTEGER NOT NULL
                REFERENCES catalog_snapshots(id) ON DELETE CASCADE,
    sheet_no    TEXT NOT NULL,
    period      TEXT NOT NULL,
    min_x       REAL NOT NULL,
    min_y       REAL NOT NULL,
    max_x       REAL NOT NULL,
    max_y       REAL NOT NULL,
    resolution  REAL NOT NULL,
    file_path   TEXT,
    is_active   INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, sheet_no)
);

CREATE TABLE IF NOT EXISTS image_selection_manifests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_no        TEXT NOT NULL UNIQUE,
    period             TEXT NOT NULL,
    crs                TEXT NOT NULL,
    units              TEXT NOT NULL,
    release_snapshot_id INTEGER NOT NULL
                       REFERENCES catalog_snapshots(id),
    tile_count         INTEGER NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS image_selection_tiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_id   INTEGER NOT NULL
                  REFERENCES image_selection_manifests(id) ON DELETE CASCADE,
    tile_no       TEXT NOT NULL,
    min_x         REAL NOT NULL,
    min_y         REAL NOT NULL,
    max_x         REAL NOT NULL,
    max_y         REAL NOT NULL,
    blank_area_sqm REAL NOT NULL,
    is_blank      INTEGER NOT NULL,
    UNIQUE (manifest_id, tile_no)
);

CREATE TABLE IF NOT EXISTS image_selection_zones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tile_id     INTEGER NOT NULL
                REFERENCES image_selection_tiles(id) ON DELETE CASCADE,
    zone_order  INTEGER NOT NULL,
    sheet_no    TEXT,               -- NULL marks a blank zone
    resolution  REAL,
    file_path   TEXT,
    min_x       REAL NOT NULL,
    min_y       REAL NOT NULL,
    max_x       REAL NOT NULL,
    max_y       REAL NOT NULL,
    area_sqm    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS manifest_reviews (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    review_no           TEXT NOT NULL UNIQUE,
    manifest_id         INTEGER NOT NULL
                        REFERENCES image_selection_manifests(id) ON DELETE CASCADE,
    snapshot_id         INTEGER NOT NULL REFERENCES catalog_snapshots(id),
    comparable          INTEGER NOT NULL,
    status              TEXT NOT NULL,
    crs                 TEXT NOT NULL,
    units               TEXT NOT NULL,
    message             TEXT NOT NULL,
    changed_sheets      TEXT NOT NULL,
    candidate_tiles     TEXT NOT NULL,
    changed_tiles       TEXT NOT NULL,
    recomputed_tile_count INTEGER NOT NULL,
    changed_tile_count  INTEGER NOT NULL,
    reviewed_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_images_period ON images(period);
CREATE INDEX IF NOT EXISTS idx_snapshot_images_period
    ON catalog_snapshot_images(period);
"""


class CatalogError(ValueError):
    """Malformed catalog JSON."""


@dataclass(frozen=True)
class ImageRecord:
    sheet_no: str
    period: str
    rect: Rect
    resolution: float
    file_path: str | None
    is_active: bool


@dataclass(frozen=True)
class ManifestRecord:
    manifest_no: str
    period: str
    crs: str
    units: str
    release_snapshot_id: int
    tile_count: int
    created_at: str


@dataclass(frozen=True)
class ManifestTileRecord:
    tile_no: str
    rect: Rect
    blank_area_sqm: float
    is_blank: bool
    zones: list["Zone"]


# CatalogImage is the selection module's read model; catalog rows from the
# live table and from frozen snapshots are adapted to it explicitly.


def catalog_image_from_row(row: sqlite3.Row) -> CatalogImage:
    return CatalogImage(
        sheet_no=row["sheet_no"],
        period=row["period"],
        rect=Rect(row["min_x"], row["min_y"], row["max_x"], row["max_y"]),
        resolution=row["resolution"],
        file_path=row["file_path"],
        is_active=bool(row["is_active"]),
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.db_path(),
        timeout=config.BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # autocommit mode; transactions are explicit
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def retry_on_locked(operation, *args, attempts: int = 5, **kwargs):
    """Retry ``operation`` on transient SQLite write-lock contention."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation(*args, **kwargs)
        except sqlite3.OperationalError as exc:  # database is locked
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(0.05 * (2**attempt))
    assert last_error is not None
    raise last_error


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serializing write transaction: acquires the write lock immediately."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Initialization from JSON
# ---------------------------------------------------------------------------

def _require(obj: dict[str, Any], key: str, sheet_no: str) -> Any:
    if key not in obj:
        raise CatalogError(f"image '{sheet_no}' is missing field '{key}'")
    return obj[key]


def _parse_image(obj: Any) -> tuple[str, str, float, float, float, float, float, str | None]:
    if not isinstance(obj, dict):
        raise CatalogError(f"catalog entry is not an object: {obj!r}")
    sheet_no = str(_require(obj, "sheet_no", "<unknown>"))
    period = str(_require(obj, "period", sheet_no))
    bbox = _require(obj, "bbox", sheet_no)
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise CatalogError(
            f"image '{sheet_no}': bbox must be [min_x, min_y, max_x, max_y]"
        )
    min_x, min_y, max_x, max_y = (float(v) for v in bbox)
    # Validates the rectangle; raises GeometryError on degenerate input.
    Rect(min_x, min_y, max_x, max_y)
    resolution = float(_require(obj, "resolution_mpp", sheet_no))
    if resolution <= 0:
        raise CatalogError(f"image '{sheet_no}': resolution must be positive")
    file_path = obj.get("file_path")
    return sheet_no, period, min_x, min_y, max_x, max_y, resolution, file_path


def initialize_database(force: bool = False) -> dict[str, Any]:
    """(Re)build the SQLite catalog from the JSON file.

    Idempotent: with ``force=False`` an already-initialized database is left
    untouched. The JSON file is only read; image files themselves are never
    opened, moved or deleted.
    """
    db = config.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    with open(config.catalog_path(), "r", encoding="utf-8") as fh:
        catalog = json.load(fh)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("images"), list):
        raise CatalogError("catalog JSON must be an object with an 'images' list")

    parsed = [_parse_image(item) for item in catalog["images"]]
    seen: set[str] = set()
    for row in parsed:
        if row[0] in seen:
            raise CatalogError(f"duplicate sheet_no in catalog: {row[0]}")
        seen.add(row[0])

    already = db.exists()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        seeded = conn.execute(
            "SELECT COUNT(*) AS n FROM catalog_meta WHERE key = 'seeded'"
        ).fetchone()["n"]
        if seeded and not force:
            return {
                "initialized": False,
                "reason": "database already initialized; use force=true to rebuild",
                "image_count": conn.execute(
                    "SELECT COUNT(*) AS n FROM images"
                ).fetchone()["n"],
            }

        with immediate_transaction(conn):
            conn.execute("DELETE FROM images")
            conn.execute("DELETE FROM withdrawal_batches")
            conn.execute("DELETE FROM catalog_meta")
            conn.executemany(
                """
                INSERT INTO images
                    (sheet_no, period, min_x, min_y, max_x, max_y,
                     resolution, file_path, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                parsed,
            )
            meta = {
                "seeded": "1",
                "crs": str(catalog.get("crs", "unspecified")),
                "units": str(catalog.get("units", "metre")),
                "source_catalog": str(config.catalog_path()),
                "seeded_at": utc_now_iso(),
            }
            conn.executemany(
                "INSERT INTO catalog_meta(key, value) VALUES (?, ?)",
                list(meta.items()),
            )
    finally:
        conn.close()

    return {
        "initialized": True,
        "rebuilt": already,
        "image_count": len(parsed),
        "crs": catalog.get("crs", "unspecified"),
    }


def ensure_initialized() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        seeded = conn.execute(
            "SELECT COUNT(*) AS n FROM catalog_meta WHERE key = 'seeded'"
        ).fetchone()["n"]
    finally:
        conn.close()
    if not seeded:
        initialize_database()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _snapshot_catalog(conn: sqlite3.Connection, reason: str) -> int:
    """Freeze the whole current catalog into an immutable snapshot."""
    cursor = conn.execute(
        "INSERT INTO catalog_snapshots(reason, created_at) VALUES (?, ?)",
        (reason, utc_now_iso()),
    )
    snapshot_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO catalog_snapshot_images
            (snapshot_id, sheet_no, period, min_x, min_y, max_x, max_y,
             resolution, file_path, is_active)
        SELECT ?, sheet_no, period, min_x, min_y, max_x, max_y,
               resolution, file_path, is_active
        FROM images
        ORDER BY sheet_no
        """,
        (snapshot_id,),
    )
    return snapshot_id


def _rows_to_snapshot_images(
    conn: sqlite3.Connection, snapshot_id: int, period: str
) -> list[CatalogImage]:
    return [
        CatalogImage(
            sheet_no=row["sheet_no"],
            period=row["period"],
            rect=Rect(row["min_x"], row["min_y"], row["max_x"], row["max_y"]),
            resolution=row["resolution"],
            file_path=row["file_path"],
            is_active=bool(row["is_active"]),
        )
        for row in conn.execute(
            """
            SELECT * FROM catalog_snapshot_images
            WHERE snapshot_id = ? AND period = ?
            ORDER BY sheet_no
            """,
            (snapshot_id, period),
        )
    ]


def catalog_meta_row(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["key"]: r["value"]
        for r in conn.execute("SELECT key, value FROM catalog_meta")
    }


def _row_to_record(row: sqlite3.Row) -> ImageRecord:
    return ImageRecord(
        sheet_no=row["sheet_no"],
        period=row["period"],
        rect=Rect(row["min_x"], row["min_y"], row["max_x"], row["max_y"]),
        resolution=row["resolution"],
        file_path=row["file_path"],
        is_active=bool(row["is_active"]),
    )


def list_images(active_only: bool) -> list[ImageRecord]:
    conn = connect()
    try:
        sql = "SELECT * FROM images"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY sheet_no"
        return [_row_to_record(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def get_image(sheet_no: str) -> ImageRecord | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM images WHERE sheet_no = ?", (sheet_no,)
        ).fetchone()
        return _row_to_record(row) if row else None
    finally:
        conn.close()


def list_batches(limit: int) -> list[sqlite3.Row]:
    conn = connect()
    try:
        return list(
            conn.execute(
                "SELECT * FROM withdrawal_batches ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )
    finally:
        conn.close()


def catalog_metadata() -> dict[str, str]:
    conn = connect()
    try:
        return {
            r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM catalog_meta")
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Batch take-down
# ---------------------------------------------------------------------------

class WithdrawalValidationError(Exception):
    """Business-rule failure; the batch must not take effect (HTTP 409)."""

    def __init__(
        self,
        reason: str,
        failed_sheet: str | None = None,
        uncovered_rect: dict[str, float] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failed_sheet = failed_sheet
        self.uncovered_rect = uncovered_rect


def _record_batch(
    conn: sqlite3.Connection,
    batch_no: str,
    status: str,
    requested: list[str],
    reason: str | None,
    failed_sheet: str | None,
    uncovered_rect: dict[str, float] | None,
) -> None:
    conn.execute(
        """
        INSERT INTO withdrawal_batches
            (batch_no, status, requested_sheets, reason, failed_sheet,
             uncovered_rect, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_no,
            status,
            json.dumps(requested, ensure_ascii=False),
            reason,
            failed_sheet,
            json.dumps(uncovered_rect) if uncovered_rect else None,
            utc_now_iso(),
        ),
    )


def _next_batch_no(conn: sqlite3.Connection) -> str:
    year = datetime.now(timezone.utc).strftime("%Y%m%d")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM withdrawal_batches"
    ).fetchone()
    return f"WD-{year}-{row['n'] + 1:05d}"


def _insert_rejected_audit(
    requested: list[str],
    reason: str,
    failed_sheet: str | None,
    uncovered_rect: dict[str, float] | None,
) -> None:
    """Persist a rejected batch in its own transaction.

    Validation failures roll back the (empty) image-update transaction; the
    audit trail must survive, so it is written through a fresh connection.
    """
    audit_conn = connect()
    try:
        with immediate_transaction(audit_conn):
            _record_batch(
                audit_conn,
                _next_batch_no(audit_conn),
                "rejected",
                requested,
                reason,
                failed_sheet,
                uncovered_rect,
            )
    finally:
        audit_conn.close()


def withdraw_batch(requested_sheets: list[str]) -> dict[str, Any]:
    """Validate and apply one take-down batch atomically.

    Raises ``WithdrawalValidationError`` when any sheet is missing/offline or
    the surviving, sufficiently fine imagery leaves a coverage gap; in that
    case nothing in the images table changes. A rejected-batch audit row is
    still persisted in a separate transaction.
    """
    # Preserve request order for the audit trail while evaluating each sheet once.
    unique_sheets = list(dict.fromkeys(requested_sheets))
    conn = connect()
    try:
        validation_error: WithdrawalValidationError | None = None
        result: dict[str, Any] | None = None
        try:
            with immediate_transaction(conn):
                rows = {
                    r["sheet_no"]: r
                    for r in conn.execute("SELECT * FROM images")
                }
                active = {
                    sheet: _row_to_record(row)
                    for sheet, row in rows.items()
                    if row["is_active"] == 1
                }

                # Rule 1: every requested sheet must currently be on the shelf.
                for sheet in unique_sheets:
                    record = active.get(sheet)
                    if record is None:
                        known = sheet in rows
                        reason = (
                            "sheet already withdrawn"
                            if known
                            else "sheet not found in catalog"
                        )
                        raise WithdrawalValidationError(
                            reason=reason, failed_sheet=sheet
                        )

                # Rule 2: after this batch, each sheet must still be fully
                # covered by other surviving images whose m/px is <= its own.
                survivors = {
                    sheet: rec
                    for sheet, rec in active.items()
                    if sheet not in set(unique_sheets)
                }
                for sheet in unique_sheets:
                    target = active[sheet]
                    coverer_records = [
                        rec
                        for rec in survivors.values()
                        if rec.resolution <= target.resolution + 1e-12
                    ]
                    try:
                        gap = find_uncovered_rectangle(
                            target.rect, [rec.rect for rec in coverer_records]
                        )
                    except GeometryError as exc:
                        raise WithdrawalValidationError(reason=str(exc))
                    if gap is not None:
                        rect_payload = {
                            "min_x": gap.min_x,
                            "min_y": gap.min_y,
                            "max_x": gap.max_x,
                            "max_y": gap.max_y,
                        }
                        raise WithdrawalValidationError(
                            reason="coverage gap after withdrawal",
                            failed_sheet=sheet,
                            uncovered_rect=rect_payload,
                        )

                # All checks passed inside the same write transaction: apply.
                conn.executemany(
                    "UPDATE images SET is_active = 0 WHERE sheet_no = ?",
                    [(sheet,) for sheet in unique_sheets],
                )
                batch_no = _next_batch_no(conn)
                _record_batch(
                    conn, batch_no, "committed",
                    unique_sheets, None, None, None,
                )
                result = {
                    "batch_no": batch_no,
                    "status": "committed",
                    "withdrawn_sheets": unique_sheets,
                    "withdrawn_count": len(unique_sheets),
                    "committed_at": utc_now_iso(),
                }
        except WithdrawalValidationError as exc:
            validation_error = exc

        if validation_error is not None:
            _insert_rejected_audit(
                unique_sheets,
                validation_error.reason,
                validation_error.failed_sheet,
                validation_error.uncovered_rect,
            )
            raise validation_error
        assert result is not None
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Image-selection manifests ("取图清单")
# ---------------------------------------------------------------------------

class ManifestError(ValueError):
    """Business failure for manifest registration/review (HTTP 422)."""


class UnknownPeriod(ManifestError):
    """The requested survey period has no catalog rows at all."""


class ManifestNotFound(ManifestError):
    """No manifest with that number."""


@dataclass(frozen=True)
class TileInput:
    tile_no: str
    rect: Rect


def _manifest_no(conn: sqlite3.Connection) -> str:
    year = datetime.now(timezone.utc).strftime("%Y%m%d")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM image_selection_manifests"
    ).fetchone()
    return f"IS-{year}-{row['n'] + 1:05d}"


def _row_to_manifest(row: sqlite3.Row) -> ManifestRecord:
    return ManifestRecord(
        manifest_no=row["manifest_no"],
        period=row["period"],
        crs=row["crs"],
        units=row["units"],
        release_snapshot_id=row["release_snapshot_id"],
        tile_count=row["tile_count"],
        created_at=row["created_at"],
    )


def _load_manifest(
    conn: sqlite3.Connection, manifest_no: str
) -> tuple[ManifestRecord, list[ManifestTileRecord]] | None:
    row = conn.execute(
        "SELECT * FROM image_selection_manifests WHERE manifest_no = ?",
        (manifest_no,),
    ).fetchone()
    if row is None:
        return None
    manifest = _row_to_manifest(row)
    tiles: list[ManifestTileRecord] = []
    for tile_row in conn.execute(
        """
        SELECT * FROM image_selection_tiles
        WHERE manifest_id = ?
        ORDER BY min_y, min_x, tile_no
        """,
        (row["id"],),
    ):
        zones = [
            Zone(
                Rect(z["min_x"], z["min_y"], z["max_x"], z["max_y"]),
                (z["sheet_no"], z["resolution"], z["file_path"]),
            )
            for z in conn.execute(
                """
                SELECT * FROM image_selection_zones
                WHERE tile_id = ? ORDER BY zone_order
                """,
                (tile_row["id"],),
            )
        ]
        tiles.append(
            ManifestTileRecord(
                tile_no=tile_row["tile_no"],
                rect=Rect(
                    tile_row["min_x"], tile_row["min_y"],
                    tile_row["max_x"], tile_row["max_y"],
                ),
                blank_area_sqm=tile_row["blank_area_sqm"],
                is_blank=bool(tile_row["is_blank"]),
                zones=zones,
            )
        )
    return manifest, tiles


def _insert_manifest(
    conn: sqlite3.Connection,
    manifest_no: str,
    period: str,
    meta: dict[str, str],
    snapshot_id: int,
    selections: list[tuple[str, Rect, list[Zone], float, bool]],
) -> None:
    cursor = conn.execute(
        """
        INSERT INTO image_selection_manifests
            (manifest_no, period, crs, units, release_snapshot_id,
             tile_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            manifest_no,
            period,
            meta.get("crs", "unspecified"),
            meta.get("units", "metre"),
            snapshot_id,
            len(selections),
            utc_now_iso(),
        ),
    )
    manifest_id = int(cursor.lastrowid)
    for tile_no, rect, zones, blank_area, is_blank in selections:
        tile_cursor = conn.execute(
            """
            INSERT INTO image_selection_tiles
                (manifest_id, tile_no, min_x, min_y, max_x, max_y,
                 blank_area_sqm, is_blank)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest_id, tile_no, rect.min_x, rect.min_y,
                rect.max_x, rect.max_y, blank_area, int(is_blank),
            ),
        )
        tile_id = int(tile_cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO image_selection_zones
                (tile_id, zone_order, sheet_no, resolution, file_path,
                 min_x, min_y, max_x, max_y, area_sqm)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    tile_id, order, zone.sheet_no, zone.resolution_mpp,
                    zone.file_path, zone.rect.min_x, zone.rect.min_y,
                    zone.rect.max_x, zone.rect.max_y, zone.rect.area,
                )
                for order, zone in enumerate(zones)
            ],
        )


def create_manifest(
    period: str, tiles: list[TileInput]
) -> tuple[ManifestRecord, list[ManifestTileRecord]]:
    """Register a selection manifest against the catalog as of *now*.

    Selection, snapshot capture and persistence happen inside one
    ``BEGIN IMMEDIATE`` transaction, so the manifest and its ``release``
    snapshot refer to exactly the same instant of the live catalog.
    """
    conn = connect()
    try:
        with immediate_transaction(conn):
            meta = catalog_meta_row(conn)
            images = [
                catalog_image_from_row(r)
                for r in conn.execute("SELECT * FROM images ORDER BY sheet_no")
            ]
            if not any(img.period == period for img in images):
                raise UnknownPeriod(
                    f"survey period '{period}' does not exist in the catalog"
                )
            snapshot_id = _snapshot_catalog(conn, "manifest-release")
            computed = [
                selection_for_tile(tile.tile_no, tile.rect, period, images)
                for tile in tiles
            ]
            manifest_no = _manifest_no(conn)
            _insert_manifest(
                conn, manifest_no, period, meta, snapshot_id, computed
            )
    finally:
        conn.close()
    loaded = get_manifest(manifest_no)
    assert loaded is not None
    return loaded


def get_manifest(
    manifest_no: str,
) -> tuple[ManifestRecord, list[ManifestTileRecord]] | None:
    conn = connect()
    try:
        return _load_manifest(conn, manifest_no)
    finally:
        conn.close()


def list_manifests(limit: int) -> list[ManifestRecord]:
    conn = connect()
    try:
        return [
            _row_to_manifest(r)
            for r in conn.execute(
                "SELECT * FROM image_selection_manifests "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        ]
    finally:
        conn.close()


def _review_no(conn: sqlite3.Connection) -> str:
    year = datetime.now(timezone.utc).strftime("%Y%m%d")
    row = conn.execute("SELECT COUNT(*) AS n FROM manifest_reviews").fetchone()
    return f"RV-{year}-{row['n'] + 1:05d}"


def review_manifest(manifest_no: str) -> dict[str, Any]:
    """Compare a stored manifest with the catalog as of *now*.

    The review period is the manifest's own survey period: the stored zones
    were selected from that period's imagery. Only tiles whose footprint
    intersects the changed coverage (old or new) of images that changed in
    that period are recomputed. A difference in CRS or units makes the
    comparison impossible; that is reported explicitly and no recomputation
    is attempted. The review and the snapshot it compared against are
    persisted, so the result can be re-read after a restart.
    """
    conn = connect()
    try:
        with immediate_transaction(conn):
            loaded = _load_manifest(conn, manifest_no)
            if loaded is None:
                raise ManifestNotFound(f"manifest '{manifest_no}' not found")
            manifest, tiles = loaded
            review_period = manifest.period
            meta = catalog_meta_row(conn)
            live_images = [
                catalog_image_from_row(r)
                for r in conn.execute("SELECT * FROM images ORDER BY sheet_no")
            ]
            snapshot_id = _snapshot_catalog(conn, "manifest-review")

            crs_matches = meta.get("crs") == manifest.crs
            units_matches = meta.get("units") == manifest.units
            comparable = crs_matches and units_matches

            changed_tiles: list[dict[str, Any]] = []
            changed_sheets: list[str] = []
            candidate_tile_nos: list[str] = []
            recomputed = 0

            if comparable:
                release_images = _rows_to_snapshot_images(
                    conn, manifest.release_snapshot_id, review_period
                )
                changed = detect_changed_images(
                    review_period, release_images, live_images
                )
                changed_sheets = [c.sheet_no for c in changed]
                candidate_tile_nos = affected_tile_nos(
                    [(t.tile_no, t.rect) for t in tiles], changed
                )
                candidate_set = set(candidate_tile_nos)
                for tile in tiles:
                    if tile.tile_no not in candidate_set:
                        continue
                    recomputed += 1
                    new_zones = select_zones(
                        tile.rect, live_images, review_period
                    )
                    regions = compare_zones(tile.rect, tile.zones, new_zones)
                    if not regions:
                        continue
                    changed_tiles.append(
                        {
                            "tile_no": tile.tile_no,
                            "bbox": [
                                tile.rect.min_x, tile.rect.min_y,
                                tile.rect.max_x, tile.rect.max_y,
                            ],
                            "changed_area_sqm": round(
                                sum(r.rect.area for r in regions), 9
                            ),
                            "changed_regions": [
                                {
                                    "bbox": [
                                        r.rect.min_x, r.rect.min_y,
                                        r.rect.max_x, r.rect.max_y,
                                    ],
                                    "area_sqm": r.rect.area,
                                    "old": _winner_payload(r.old),
                                    "new": _winner_payload(r.new),
                                }
                                for r in regions
                            ],
                        }
                    )

            if not comparable:
                status = "not_comparable"
                message = (
                    "coordinate system or units changed since the manifest "
                    "was registered; comparison is not possible"
                )
            elif changed_tiles:
                status = "changed"
                message = (
                    f"{len(changed_tiles)} of {len(tiles)} tiles need "
                    "re-cutting"
                )
            else:
                status = "unchanged"
                message = "no tile needs re-cutting"

            review_no = _review_no(conn)
            conn.execute(
                """
                INSERT INTO manifest_reviews
                    (review_no, manifest_id, snapshot_id, comparable, status,
                     crs, units, message, changed_sheets, candidate_tiles,
                     changed_tiles, recomputed_tile_count,
                     changed_tile_count, reviewed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_no,
                    _manifest_id(conn, manifest_no),
                    snapshot_id,
                    int(comparable),
                    status,
                    meta.get("crs", "unspecified"),
                    meta.get("units", "metre"),
                    message,
                    json.dumps(changed_sheets),
                    json.dumps(candidate_tile_nos),
                    json.dumps(changed_tiles),
                    recomputed,
                    len(changed_tiles),
                    utc_now_iso(),
                ),
            )
    finally:
        conn.close()
    return get_review(review_no)


def _manifest_id(conn: sqlite3.Connection, manifest_no: str) -> int:
    row = conn.execute(
        "SELECT id FROM image_selection_manifests WHERE manifest_no = ?",
        (manifest_no,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _winner_payload(winner: tuple) -> dict[str, Any] | None:
    sheet_no, resolution, file_path = winner
    if sheet_no is None:
        return None
    return {
        "sheet_no": sheet_no,
        "resolution_mpp": resolution,
        "file_path": file_path,
    }


def _review_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "review_no": row["review_no"],
        "manifest_no": row["manifest_no"],
        "status": row["status"],
        "comparable": bool(row["comparable"]),
        "crs": row["crs"],
        "units": row["units"],
        "message": row["message"],
        "changed_sheets": json.loads(row["changed_sheets"]),
        "candidate_tiles": json.loads(row["candidate_tiles"]),
        "recomputed_tile_count": row["recomputed_tile_count"],
        "changed_tile_count": row["changed_tile_count"],
        "changed_tiles": json.loads(row["changed_tiles"]),
        "reviewed_at": row["reviewed_at"],
    }


def get_review(review_no: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT r.*, m.manifest_no
            FROM manifest_reviews r
            JOIN image_selection_manifests m ON m.id = r.manifest_id
            WHERE r.review_no = ?
            """,
            (review_no,),
        ).fetchone()
        return _review_row_to_dict(row) if row else None
    finally:
        conn.close()


def list_reviews(manifest_no: str | None, limit: int) -> list[dict[str, Any]]:
    conn = connect()
    try:
        sql = """
            SELECT r.*, m.manifest_no
            FROM manifest_reviews r
            JOIN image_selection_manifests m ON m.id = r.manifest_id
        """
        params: list[Any] = []
        if manifest_no is not None:
            sql += " WHERE m.manifest_no = ?"
            params.append(manifest_no)
        sql += " ORDER BY r.id DESC LIMIT ?"
        params.append(limit)
        return [_review_row_to_dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()
