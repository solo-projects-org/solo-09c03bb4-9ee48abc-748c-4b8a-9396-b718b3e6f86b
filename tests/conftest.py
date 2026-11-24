"""Pytest fixtures: every test gets an isolated temp SQLite DB + catalog."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


def _img(sheet, bbox, res, period="2024Q3", path=None):
    return {
        "sheet_no": sheet,
        "period": period,
        "bbox": bbox,
        "resolution_mpp": res,
        "file_path": path or f"/data/tiles/{sheet}.tif",
    }


# Compact catalog exercising every rule:
# A        -> fully covered by finer halves B + C (removable)
# D        -> E only covers its eastern half (gap)
# F        -> G has the same footprint but is COARSER (resolution rule)
# H, I     -> duplicate 0.5 m/px sheets, J finer 0.25 m/px underneath
# K        -> fully covered by finer L (for multi-sheet success batches)
DEFAULT_IMAGES = [
    _img("A", [0, 0, 100, 100], 0.5),
    _img("B", [0, 0, 50, 100], 0.25),
    _img("C", [50, 0, 100, 100], 0.25),
    _img("D", [200, 0, 300, 100], 0.5),
    _img("E", [250, 0, 300, 100], 0.25),
    _img("F", [400, 0, 500, 100], 0.5),
    _img("G", [400, 0, 500, 100], 1.0),
    _img("H", [600, 0, 700, 100], 0.5),
    _img("I", [600, 0, 700, 100], 0.5),
    _img("J", [600, 0, 700, 100], 0.25),
    _img("K", [800, 0, 900, 100], 0.5),
    _img("L", [800, 0, 900, 100], 0.25),
]


@pytest.fixture
def catalog_paths(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    catalog_file = tmp_path / "catalog.json"
    monkeypatch.setenv("IMAGERY_DB_PATH", str(db_path))
    monkeypatch.setenv("IMAGERY_CATALOG_PATH", str(catalog_file))

    def write(images, **meta):
        payload = {"crs": "EPSG:32650", "units": "metre", "images": images}
        payload.update(meta)
        catalog_file.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    return catalog_file, write


@pytest.fixture
def client(catalog_paths):
    _, write = catalog_paths
    write(DEFAULT_IMAGES)
    from app import database
    from app.main import app

    database.initialize_database()
    with TestClient(app) as test_client:
        yield test_client
