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
