"""Unit tests for the pure zone-selection / comparison module."""
from __future__ import annotations

import random

from app.geometry import Rect
from app.selection import (
    BLANK,
    CatalogImage,
    Zone,
    affected_tile_nos,
    compare_zones,
    detect_changed_images,
    select_zones,
)


def _img(sheet, bbox, res, period="P", active=True, path=None):
    return CatalogImage(
        sheet_no=sheet,
        period=period,
        rect=Rect(*bbox),
        resolution=res,
        file_path=path or f"/{sheet}.tif",
        is_active=active,
    )


def _area(zones):
    return sum(z.rect.area for z in zones)


def test_finest_wins_and_blank_fills_rest():
    tile = Rect(0, 0, 100, 100)
    zones = select_zones(
        tile,
        [
            _img("COARSE", [0, 0, 100, 100], 1.0),
            _img("FINE", [0, 0, 50, 100], 0.5),
        ],
        "P",
    )
    assert _area(zones) == tile.area
    assert {(z.sheet_no, z.rect.as_tuple()) for z in zones} == {
        ("FINE", (0, 0, 50, 100)),
        ("COARSE", (50, 0, 100, 100)),
    }


def test_tie_break_is_ascending_sheet_number():
    tile = Rect(0, 0, 10, 10)
    zones = select_zones(
        tile,
        [_img("B", [0, 0, 10, 10], 0.5), _img("A", [0, 0, 10, 10], 0.5)],
        "P",
    )
    assert len(zones) == 1 and zones[0].sheet_no == "A"


def test_other_period_and_inactive_are_ignored():
    tile = Rect(0, 0, 10, 10)
    zones = select_zones(
        tile,
        [
            _img("OLD", [0, 0, 10, 10], 0.01, period="OTHER"),
            _img("OFF", [0, 0, 10, 10], 0.01, active=False),
        ],
        "P",
    )
    assert len(zones) == 1
    assert zones[0].winner == BLANK
    assert zones[0].is_blank


def test_zones_partition_tile_exactly_with_random_rectangles():
    rng = random.Random(20241124)
    for _ in range(20):
        tile = Rect(0, 0, 100, 100)
        images = []
        for i in range(rng.randint(1, 8)):
            x0 = rng.uniform(-20, 80)
            y0 = rng.uniform(-20, 80)
            images.append(
                _img(
                    f"S{i:02d}",
                    [x0, y0, x0 + rng.uniform(5, 70), y0 + rng.uniform(5, 70)],
                    rng.choice([0.1, 0.25, 0.5, 1.0]),
                )
            )
        zones = select_zones(tile, images, "P")
        # Exact tiling: total area equals tile area (no overlap, no hole).
        assert abs(_area(zones) - tile.area) < 1e-6
        # Every zone lies inside the tile.
        for z in zones:
            assert z.rect.min_x >= tile.min_x - 1e-9
            assert z.rect.max_x <= tile.max_x + 1e-9
            assert z.rect.min_y >= tile.min_y - 1e-9
            assert z.rect.max_y <= tile.max_y + 1e-9


def test_selection_matches_brute_force_at_sample_points():
    rng = random.Random(7)
    tile = Rect(0, 0, 100, 100)
    images = [
        _img("A", [0, 0, 60, 100], 0.5),
        _img("B", [40, 0, 100, 60], 0.25),
        _img("C", [0, 40, 50, 100], 0.25),
        _img("D", [20, 20, 80, 80], 0.5),
    ]
    zones = select_zones(tile, images, "P")
    for _ in range(500):
        x, y = rng.uniform(0.001, 99.999), rng.uniform(0.001, 99.999)
        covering = [
            img for img in images
            if img.rect.min_x <= x <= img.rect.max_x
            and img.rect.min_y <= y <= img.rect.max_y
        ]
        expected = BLANK
        if covering:
            best = min(covering, key=lambda i: (i.resolution, i.sheet_no))
            expected = best.winner
        got = [
            z.winner for z in zones
            if z.rect.min_x <= x <= z.rect.max_x
            and z.rect.min_y <= y <= z.rect.max_y
        ]
        assert got == [expected], (x, y, got, expected)


def test_zone_layout_change_with_same_service_compares_equal():
    tile = Rect(0, 0, 100, 100)
    old = [Zone(tile, ("A", 0.5, "/a.tif"))]
    # Two zones but the same sheet/resolution/path on both sides.
    new = [
        Zone(Rect(0, 0, 40, 100), ("A", 0.5, "/a.tif")),
        Zone(Rect(40, 0, 100, 100), ("A", 0.5, "/a.tif")),
    ]
    assert compare_zones(tile, old, new) == []


def test_compare_reports_only_the_changed_area():
    tile = Rect(0, 0, 100, 100)
    old = [Zone(tile, ("A", 0.5, "/a.tif"))]
    new = [
        Zone(Rect(0, 0, 30, 100), ("B", 0.25, "/b.tif")),
        Zone(Rect(30, 0, 100, 100), ("A", 0.5, "/a.tif")),
    ]
    regions = compare_zones(tile, old, new)
    assert len(regions) == 1
    assert regions[0].rect == Rect(0, 0, 30, 100)
    assert regions[0].old[0] == "A"
    assert regions[0].new[0] == "B"


def test_blank_to_image_and_image_to_blank_are_changes():
    tile = Rect(0, 0, 100, 100)
    old = [Zone(tile, BLANK)]
    new = [Zone(tile, ("A", 0.5, "/a.tif"))]
    regions = compare_zones(tile, old, new)
    assert regions and regions[0].old == BLANK and regions[0].new[0] == "A"
    back = compare_zones(tile, new, old)
    assert back and back[0].new == BLANK


def test_detect_changed_images_covers_add_remove_modify_state_and_period():
    old = [
        _img("KEEP", [0, 0, 1, 1], 1.0),
        _img("MOD", [0, 0, 1, 1], 1.0),
        _img("PATH", [0, 0, 1, 1], 1.0),
        _img("GONE", [0, 0, 1, 1], 1.0),
        _img("OFF", [0, 0, 1, 1], 1.0),
        _img("LEAVE", [0, 0, 1, 1], 1.0),
    ]
    new = [
        _img("KEEP", [0, 0, 1, 1], 1.0),
        _img("MOD", [0, 0, 1, 1], 0.5),
        _img("PATH", [0, 0, 1, 1], 1.0, path="/moved/PATH.tif"),
        _img("OFF", [0, 0, 1, 1], 1.0, active=False),
        _img("LEAVE", [0, 0, 1, 1], 1.0, period="Q2"),
        _img("NEW", [0, 0, 1, 1], 1.0),
    ]
    changed = {c.sheet_no: c for c in detect_changed_images("P", old, new)}
    assert set(changed) == {"MOD", "PATH", "GONE", "OFF", "LEAVE", "NEW"}
    assert changed["MOD"].old.resolution == 1.0
    assert changed["MOD"].new.resolution == 0.5
    assert changed["GONE"].new is None
    assert changed["NEW"].old is None


def test_affected_tiles_uses_old_and_new_footprints():
    changed = detect_changed_images(
        "P",
        [_img("X", [0, 0, 10, 10], 1.0)],
        [_img("X", [100, 100, 110, 110], 1.0)],
    )
    tiles = [
        ("OLD_AREA", Rect(5, 5, 10, 10)),
        ("NEW_AREA", Rect(100, 100, 105, 105)),
        ("NOWHERE", Rect(50, 50, 60, 60)),
        ("EDGE_ONLY", Rect(10, 0, 20, 10)),
    ]
    assert affected_tile_nos(tiles, changed) == ["OLD_AREA", "NEW_AREA"]
