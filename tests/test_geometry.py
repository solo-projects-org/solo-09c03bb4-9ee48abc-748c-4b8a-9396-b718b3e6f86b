"""Unit tests for exact rectangle-coverage math."""
from __future__ import annotations

import pytest

from app.geometry import GeometryError, Rect, find_uncovered_rectangle, intersect


def test_single_rect_full_coverage():
    target = Rect(0, 0, 100, 100)
    assert find_uncovered_rectangle(target, [Rect(0, 0, 100, 100)]) is None


def test_two_halves_cover_completely():
    target = Rect(0, 0, 100, 100)
    coverers = [Rect(0, 0, 50, 100), Rect(50, 0, 100, 100)]
    assert find_uncovered_rectangle(target, coverers) is None


def test_overhang_is_clipped_to_target():
    target = Rect(0, 0, 100, 100)
    # Coverer extends beyond all four edges but contains the whole target.
    assert find_uncovered_rectangle(
        target, [Rect(-50, -50, 150, 150)]
    ) is None


def test_partial_overlap_reports_gap_rectangle():
    target = Rect(0, 0, 100, 100)
    gap = find_uncovered_rectangle(target, [Rect(50, 0, 100, 100)])
    assert gap == Rect(0, 0, 50, 100)
    assert gap.area > 0


def test_interior_hole_is_detected():
    target = Rect(0, 0, 100, 100)
    # Top and bottom strips leave a 40 m band open in the middle.
    coverers = [
        Rect(0, 0, 100, 30),
        Rect(0, 70, 100, 100),
    ]
    gap = find_uncovered_rectangle(target, coverers)
    assert gap is not None
    assert gap.min_y == 30 and gap.max_y == 70
    assert gap.min_x == 0 and gap.max_x == 100


def test_non_aligned_edges_give_exact_gap():
    target = Rect(0, 0, 100, 100)
    gap = find_uncovered_rectangle(target, [Rect(0, 0, 90, 100)])
    assert gap == Rect(90, 0, 100, 100)


def test_disjoint_coverers_return_whole_target():
    target = Rect(0, 0, 100, 100)
    gap = find_uncovered_rectangle(target, [Rect(200, 200, 300, 300)])
    assert gap == target


def test_no_coverers_return_whole_target():
    target = Rect(0, 0, 100, 100)
    assert find_uncovered_rectangle(target, []) == target


def test_union_with_staggered_pieces_covers_completely():
    target = Rect(0, 0, 100, 100)
    coverers = [
        Rect(0, 0, 60, 60),
        Rect(60, 0, 100, 60),
        Rect(0, 60, 40, 100),
        Rect(40, 60, 100, 100),
    ]
    assert find_uncovered_rectangle(target, coverers) is None


def test_gap_is_always_a_positive_area_sub_rectangle_of_target():
    target = Rect(10, 20, 110, 120)
    gap = find_uncovered_rectangle(
        target,
        [Rect(10, 20, 60, 70), Rect(60, 70, 110, 120)],
    )
    assert gap is not None
    assert gap.min_x >= target.min_x and gap.max_x <= target.max_x
    assert gap.min_y >= target.min_y and gap.max_y <= target.max_y
    assert gap.area > 0


def test_coincident_edges_do_not_create_spurious_gaps():
    target = Rect(0, 0, 100, 100)
    coverers = [
        Rect(0, 0, 33.333, 100),
        Rect(33.333, 0, 77.777, 100),
        Rect(77.777, 0, 100, 100),
    ]
    assert find_uncovered_rectangle(target, coverers) is None


def test_degenerate_rectangle_rejected():
    with pytest.raises(GeometryError):
        Rect(0, 0, 0, 100)
    with pytest.raises(GeometryError):
        Rect(0, 0, 100, -5)


def test_intersect_helper():
    assert intersect(Rect(0, 0, 10, 10), Rect(5, 5, 15, 15)) == Rect(5, 5, 10, 10)
    assert intersect(Rect(0, 0, 10, 10), Rect(20, 20, 30, 30)) is None
    # Edge-only touch is not a positive-area intersection.
    assert intersect(Rect(0, 0, 10, 10), Rect(10, 0, 20, 10)) is None
