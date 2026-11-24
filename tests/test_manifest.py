"""Image-selection manifest tests: registration, replay, review."""
from __future__ import annotations

import json

from conftest import DEFAULT_IMAGES, _img
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _register(client, period, tiles):
    return client.post(
        "/manifests", json={"period": period, "tiles": tiles}
    )


def _review(client, manifest_no):
    return client.post(f"/manifests/{manifest_no}/review")


def _zones(body, tile_no):
    tile = next(t for t in body["tiles"] if t["tile_no"] == tile_no)
    return tile["zones"]


def _rewrite(client, catalog_paths, images, **meta):
    _, write = catalog_paths
    write(images, **meta)
    resp = client.post("/admin/reinitialize", params={"force": True})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def test_manifest_selects_finest_image_per_point(client):
    # B/C are finer halves of A; D is away from the tile.
    resp = _register(client, "2024Q3", [{"tile_no": "T1", "bbox": [0, 0, 100, 100]}])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["manifest_no"].startswith("IS-")
    assert body["tile_count"] == 1
    zones = _zones(body, "T1")
    assert [z["sheet_no"] for z in zones] == ["B", "C"]
    assert all(z["resolution_mpp"] == 0.25 for z in zones)
    assert all(z["file_path"].endswith(".tif") for z in zones)


def test_resolution_tie_breaks_on_ascending_sheet_no(catalog_paths, client):
    _rewrite(
        client,
        catalog_paths,
        [
            _img("Z01", [0, 0, 100, 100], 0.5),
            _img("A99", [0, 0, 100, 100], 0.5),
        ],
    )
    resp = _register(client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}])
    zones = _zones(resp.json(), "T")
    assert len(zones) == 1
    assert zones[0]["sheet_no"] == "A99"


def test_blank_areas_are_reported(client):
    # Tile extends past the catalog coverage on the right.
    resp = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 150, 100]}]
    )
    assert resp.status_code == 201
    tile = resp.json()["tiles"][0]
    assert tile["is_blank"] is False
    assert tile["blank_area_sqm"] == 50 * 100
    blank = [z for z in tile["zones"] if z["is_blank"]]
    assert len(blank) == 1
    assert blank[0]["sheet_no"] is None
    assert blank[0]["bbox"] == [100.0, 0.0, 150.0, 100.0]


def test_fully_blank_tile_is_marked(client):
    resp = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [5000, 5000, 5100, 5100]}]
    )
    assert resp.status_code == 201
    tile = resp.json()["tiles"][0]
    assert tile["is_blank"] is True
    assert tile["blank_area_sqm"] == 100 * 100
    assert tile["zones"][0]["is_blank"] is True


def test_only_active_images_of_the_period_are_used(client):
    assert client.post("/withdrawals", json={"sheet_nos": ["A"]}).status_code == 200
    resp = _register(client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}])
    assert [z["sheet_no"] for z in _zones(resp.json(), "T")] == ["B", "C"]


def test_other_periods_are_ignored(client, catalog_paths):
    _rewrite(
        client,
        catalog_paths,
        [
            _img("OLD", [0, 0, 100, 100], 0.1, period="2020Q1"),
            _img("NEW", [0, 0, 100, 100], 0.5, period="2024Q3"),
        ],
    )
    resp = _register(client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}])
    assert [z["sheet_no"] for z in _zones(resp.json(), "T")] == ["NEW"]


def test_unknown_period_is_rejected(client):
    resp = _register(client, "1999Q9", [{"tile_no": "T", "bbox": [0, 0, 10, 10]}])
    assert resp.status_code == 422
    assert "period" in resp.json()["detail"]


def test_duplicate_tile_no_is_rejected(client):
    resp = _register(
        client,
        "2024Q3",
        [
            {"tile_no": "T", "bbox": [0, 0, 10, 10]},
            {"tile_no": "T", "bbox": [10, 0, 20, 10]},
        ],
    )
    assert resp.status_code == 422


def test_degenerate_tile_is_rejected(client):
    resp = _register(client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 0, 10]}])
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def test_manifest_can_be_replayed_by_number(client):
    created = _register(
        client,
        "2024Q3",
        [
            {"tile_no": "T1", "bbox": [0, 0, 100, 100]},
            {"tile_no": "T2", "bbox": [5000, 5000, 5100, 5100]},
        ],
    ).json()
    manifest_no = created["manifest_no"]
    replay = client.get(f"/manifests/{manifest_no}")
    assert replay.status_code == 200
    assert replay.json() == created
    listed = client.get("/manifests").json()
    assert listed[0]["manifest_no"] == manifest_no
    assert listed[0]["tile_count"] == 2


def test_manifest_survives_restart(client, catalog_paths):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    from app import database
    from app.main import app as fastapi_app

    database.initialize_database()  # new process would do the same
    with TestClient(fastapi_app) as restarted:
        replay = restarted.get(f"/manifests/{created['manifest_no']}")
    assert replay.status_code == 200
    assert replay.json() == created


def test_force_rebuild_does_not_scan_or_drop_manifests(client):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    resp = client.post("/admin/reinitialize", params={"force": True})
    assert resp.status_code == 200
    replay = client.get(f"/manifests/{created['manifest_no']}")
    assert replay.status_code == 200
    assert replay.json() == created


def test_unknown_manifest_404(client):
    assert client.get("/manifests/NOPE").status_code == 404
    assert client.post("/manifests/NOPE/review").status_code == 404


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def test_review_without_changes_reports_nothing(client):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    review = _review(client, created["manifest_no"])
    assert review.status_code == 200, review.text
    body = review.json()
    assert body["status"] == "unchanged"
    assert body["comparable"] is True
    assert body["changed_tile_count"] == 0
    assert body["changed_tiles"] == []


def test_review_reports_sheet_swap(client, catalog_paths):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    _rewrite(
        client,
        catalog_paths,
        [
            _img("B", [0, 0, 50, 100], 0.1),
            _img("C", [50, 0, 100, 100], 0.25),
        ],
    )
    body = _review(client, created["manifest_no"]).json()
    assert body["status"] == "changed"
    changed = body["changed_tiles"][0]
    assert changed["tile_no"] == "T"
    region = changed["changed_regions"][0]
    assert region["bbox"] == [0, 0, 50, 100]
    assert region["old"]["sheet_no"] == "B"
    assert region["new"]["sheet_no"] == "B"
    # Same sheet number but finer resolution still means re-cut.
    assert region["old"]["resolution_mpp"] != region["new"]["resolution_mpp"]


def test_review_reports_file_path_change(client, catalog_paths):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    _rewrite(
        client,
        catalog_paths,
        [
            _img("B", [0, 0, 50, 100], 0.25, path="/new/B.tif"),
            _img("C", [50, 0, 100, 100], 0.25),
        ],
    )
    body = _review(client, created["manifest_no"]).json()
    region = body["changed_tiles"][0]["changed_regions"][0]
    assert region["old"]["file_path"] != region["new"]["file_path"]
    assert region["new"]["file_path"] == "/new/B.tif"


def test_review_reports_blank_to_image_swap(client, catalog_paths):
    # Register a manifest over a tile that is partly blank.
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 150, 100]}]
    ).json()
    assert any(
        z["is_blank"] for z in _zones(created, "T")
    )
    _rewrite(
        client,
        catalog_paths,
        [
            _img("B", [0, 0, 50, 100], 0.25),
            _img("C", [50, 0, 100, 100], 0.25),
            _img("NEW", [100, 0, 150, 100], 0.25),
        ],
    )
    body = _review(client, created["manifest_no"]).json()
    region = body["changed_tiles"][0]["changed_regions"][0]
    assert region["bbox"] == [100, 0, 150, 100]
    assert region["old"] is None
    assert region["new"]["sheet_no"] == "NEW"


def test_review_reports_image_to_blank_swap_on_removal(client, catalog_paths):
    # D is the only image under the tile; removing it makes the tile blank.
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [200, 0, 300, 100]}]
    ).json()
    _rewrite(
        client,
        catalog_paths,
        [
            _img("A", [0, 0, 100, 100], 0.5),
            _img("B", [0, 0, 50, 100], 0.25),
            _img("C", [50, 0, 100, 100], 0.25),
        ],
    )
    body = _review(client, created["manifest_no"]).json()
    assert body["status"] == "changed"
    region = body["changed_tiles"][0]["changed_regions"][0]
    assert region["old"]["sheet_no"] == "D"
    assert region["new"] is None


def test_review_reports_removed_image(client, catalog_paths):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    # Drop the finer A -> B/C selection is now A alone at coarser resolution.
    _rewrite(
        client,
        catalog_paths,
        [_img("A", [0, 0, 100, 100], 0.5)],
    )
    body = _review(client, created["manifest_no"]).json()
    region = body["changed_tiles"][0]["changed_regions"][0]
    assert region["old"]["sheet_no"] == "B"
    assert region["new"]["sheet_no"] == "A"


def test_zone_boundary_change_with_same_service_is_not_a_rechange(
    client, catalog_paths
):
    # A alone serves the whole tile. A coarser image is added that splits
    # zones but never wins: the zone layout changes, service does not.
    _rewrite(client, catalog_paths, [_img("A", [0, 0, 100, 100], 0.5)])
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    _rewrite(
        client,
        catalog_paths,
        [
            _img("A", [0, 0, 100, 100], 0.5),
            _img("COARSE", [0, 0, 100, 50], 2.0),
        ],
    )
    body = _review(client, created["manifest_no"]).json()
    assert body["status"] == "unchanged"
    assert body["changed_sheets"] == ["COARSE"]
    assert body["recomputed_tile_count"] == 1
    assert body["changed_tiles"] == []


def test_review_only_recomputes_tiles_intersecting_changed_images(
    client, catalog_paths
):
    created = _register(
        client,
        "2024Q3",
        [
            {"tile_no": "T1", "bbox": [0, 0, 100, 100]},
            {"tile_no": "T2", "bbox": [200, 0, 300, 100]},
        ],
    ).json()
    # Change only the imagery under T2 by adding a finer sheet there.
    _rewrite(
        client,
        catalog_paths,
        [
            _img("A", [0, 0, 100, 100], 0.5),
            _img("B", [0, 0, 50, 100], 0.25),
            _img("C", [50, 0, 100, 100], 0.25),
            _img("D", [200, 0, 300, 100], 0.5),
            _img("E", [250, 0, 300, 100], 0.25),
            _img("FINE", [200, 0, 250, 100], 0.1),
        ],
    )
    body = _review(client, created["manifest_no"]).json()
    assert body["candidate_tiles"] == ["T2"]
    assert body["recomputed_tile_count"] == 1
    assert [t["tile_no"] for t in body["changed_tiles"]] == ["T2"]


def test_review_ignores_changes_in_other_periods(client, catalog_paths):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    _rewrite(
        client,
        catalog_paths,
        DEFAULT_IMAGES + [_img("OLD", [0, 0, 100, 100], 0.01, period="2001Q1")],
    )
    body = _review(client, created["manifest_no"]).json()
    assert body["changed_sheets"] == []
    assert body["recomputed_tile_count"] == 0
    assert body["status"] == "unchanged"


def test_review_persists_and_can_be_read_back(client):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    body = _review(client, created["manifest_no"]).json()
    fetched = client.get(f"/reviews/{body['review_no']}")
    assert fetched.status_code == 200
    assert fetched.json() == body
    history = client.get(f"/manifests/{created['manifest_no']}/reviews").json()
    assert [r["review_no"] for r in history] == [body["review_no"]]


def test_crs_or_units_change_is_not_comparable(client, catalog_paths):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    _rewrite(
        client,
        catalog_paths,
        [_img("A", [0, 0, 100, 100], 0.5)],
        crs="EPSG:4326",
    )
    resp = _review(client, created["manifest_no"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "not possible" in detail["detail"]
    assert detail["review_no"].startswith("RV-")
    # The review itself is still replayable.
    stored = client.get(f"/reviews/{detail['review_no']}").json()
    assert stored["status"] == "not_comparable"
    assert stored["comparable"] is False
    assert stored["changed_tiles"] == []


def test_unit_change_is_not_comparable(client, catalog_paths):
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    _rewrite(
        client,
        catalog_paths,
        [_img("A", [0, 0, 100, 100], 0.5)],
        units="degree",
    )
    assert _review(client, created["manifest_no"]).status_code == 409


def test_withdrawal_is_detected_by_review(client, catalog_paths):
    # Two equal-resolution duplicates: X wins on ascending sheet number.
    _rewrite(
        client,
        catalog_paths,
        [
            _img("X", [0, 0, 100, 100], 0.5),
            _img("Y", [0, 0, 100, 100], 0.5),
        ],
    )
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    assert [z["sheet_no"] for z in _zones(created, "T")] == ["X"]

    assert client.post("/withdrawals", json={"sheet_nos": ["X"]}).status_code == 200
    body = _review(client, created["manifest_no"]).json()
    assert body["status"] == "changed"
    assert body["changed_sheets"] == ["X"]
    region = body["changed_tiles"][0]["changed_regions"][0]
    assert region["old"]["sheet_no"] == "X"
    assert region["new"]["sheet_no"] == "Y"
    # The stored manifest itself is untouched by the withdrawal.
    replay = client.get(f"/manifests/{created['manifest_no']}").json()
    assert [z["sheet_no"] for z in _zones(replay, "T")] == ["X"]


def test_new_period_image_does_not_affect_review(client, catalog_paths):
    # Adding imagery for a different period is not a change for this manifest.
    created = _register(
        client, "2024Q3", [{"tile_no": "T", "bbox": [0, 0, 100, 100]}]
    ).json()
    _rewrite(
        client,
        catalog_paths,
        DEFAULT_IMAGES + [_img("NEWP", [0, 0, 100, 100], 0.01, period="2025Q1")],
    )
    body = _review(client, created["manifest_no"]).json()
    assert body["status"] == "unchanged"
    assert body["candidate_tiles"] == []


def test_review_of_tile_outside_changed_area_is_skipped(client, catalog_paths):
    created = _register(
        client,
        "2024Q3",
        [
            {"tile_no": "T1", "bbox": [0, 0, 100, 100]},
            {"tile_no": "T2", "bbox": [200, 0, 300, 100]},
            {"tile_no": "T3", "bbox": [5000, 5000, 5100, 5100]},
        ],
    ).json()
    # A new image far away from every tile: no tile is recomputed.
    _rewrite(
        client,
        catalog_paths,
        DEFAULT_IMAGES + [_img("FAR", [9000, 9000, 9100, 9100], 0.1)],
    )
    body = _review(client, created["manifest_no"]).json()
    assert body["changed_sheets"] == ["FAR"]
    assert body["candidate_tiles"] == []
    assert body["recomputed_tile_count"] == 0
    assert body["status"] == "unchanged"


# ---------------------------------------------------------------------------
# demo catalog data files
# ---------------------------------------------------------------------------

def test_demo_catalog_v2_is_a_meaningful_change_set():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "data"
    v1 = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    v2 = json.loads((root / "catalog_v2.json").read_text(encoding="utf-8"))
    assert v1["crs"] == v2["crs"] and v1["units"] == v2["units"]
    a = {i["sheet_no"]: i for i in v1["images"]}
    b = {i["sheet_no"]: i for i in v2["images"]}
    added = set(b) - set(a)
    removed = set(a) - set(b)
    changed = {
        s for s in set(a) & set(b)
        if (a[s]["bbox"], a[s]["resolution_mpp"], a[s]["file_path"])
        != (b[s]["bbox"], b[s]["resolution_mpp"], b[s]["file_path"])
    }
    # The v2 file must exercise addition, removal, resolution and path change.
    assert added and removed and changed
    assert any(a[s]["file_path"] != b[s]["file_path"] for s in changed)
    assert any(a[s]["resolution_mpp"] != b[s]["resolution_mpp"] for s in changed)
