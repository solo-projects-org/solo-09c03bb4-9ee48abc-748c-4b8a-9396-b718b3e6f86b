"""Runtime configuration.

All paths come from environment variables so the same image can run in
Docker (compose) or directly on the host during local testing.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw) if raw else default


def db_path() -> Path:
    return _env_path("IMAGERY_DB_PATH", BASE_DIR / "data" / "imagery.db")


def catalog_path() -> Path:
    return _env_path("IMAGERY_CATALOG_PATH", BASE_DIR / "data" / "catalog.json")


# SQLite busy timeout in milliseconds: another writer may hold the
# IMMEDIATE lock while it re-validates coverage; wait instead of failing.
BUSY_TIMEOUT_MS = int(os.environ.get("IMAGERY_BUSY_TIMEOUT_MS", "8000"))

# Guard against pathological catalogs producing enormous grids.
MAX_GRID_CELLS = int(os.environ.get("IMAGERY_MAX_GRID_CELLS", "2000000"))

# Request safety limit for image-selection manifests.
MAX_MANIFEST_TILES = int(os.environ.get("IMAGERY_MAX_MANIFEST_TILES", "1000"))
