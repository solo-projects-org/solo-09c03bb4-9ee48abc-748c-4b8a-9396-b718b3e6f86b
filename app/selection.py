"""Per-tile image selection ("取图") and manifest comparison.

Selection rule
--------------
For every point of a tile, the image used is the on-shelf image of the
requested survey period covering that point with the smallest
metres-per-pixel; ties are broken by ascending sheet number. The winning
images therefore partition the tile into *zones*. Points that no on-shelf
image covers form blank zones.

Everything here is pure geometry/metadata: image files are never opened.

Comparison rule
---------------
Two selections are considered equivalent when every positive-area part of
the tile is served by the same sheet number, resolution and file path
(blank being "no sheet"). A changed zone layout with identical service is
*not* a change, exactly as required for re-cutting decisions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from . import config
from .geometry import Rect, intersect

# A winner is (sheet_no, resolution_mpp, file_path). Blank is all-None.
Winner = tuple[str | None, float | None, str | None]
BLANK: Winner = (None, None, None)


class SelectionError(ValueError):
    """The tile selection cannot be represented within configured limits."""


@dataclass(frozen=True)
class CatalogImage:
    """One catalog row used for selection (snapshot or live)."""

    sheet_no: str
    period: str
    rect: Rect
    resolution: float
    file_path: str | None
    is_active: bool

    @property
    def winner(self) -> Winner:
        return (self.sheet_no, self.resolution, self.file_path)


@dataclass(frozen=True)
class Zone:
    rect: Rect
    winner: Winner

    @property
    def sheet_no(self) -> str | None:
        return self.winner[0]

    @property
    def resolution_mpp(self) -> float | None:
        return self.winner[1]

    @property
    def file_path(self) -> str | None:
        return self.winner[2]

    @property
    def is_blank(self) -> bool:
        return self.winner[0] is None


@dataclass(frozen=True)
class ChangedImage:
    sheet_no: str
    old: CatalogImage | None
    new: CatalogImage | None


@dataclass(frozen=True)
class ChangedRegion:
    rect: Rect
    old: Winner
    new: Winner


def _grid_edges(
    tile: Rect, rects: Iterable[Rect]
) -> tuple[list[float], list[float]]:
    xs = {tile.min_x, tile.max_x}
    ys = {tile.min_y, tile.max_y}
    for rect in rects:
        xs.add(rect.min_x)
        xs.add(rect.max_x)
        ys.add(rect.min_y)
        ys.add(rect.max_y)
    x_edges = sorted(xs)
    y_edges = sorted(ys)
    if len(x_edges) * len(y_edges) > config.MAX_GRID_CELLS:
        raise SelectionError(
            "selection grid exceeds configured safety limit "
            f"({config.MAX_GRID_CELLS} cells)"
        )
    return x_edges, y_edges


def _merge_cells(
    cells: list[tuple[float, float, float, float, object]]
) -> list[tuple[Rect, object]]:
    """Merge same-key adjacent cells into non-overlapping rectangles.

    Two passes (horizontal runs, then vertical runs) keep the result exact:
    every output rectangle covers only cells of its own key and the outputs
    stay disjoint. Zones may be split more finely than strictly necessary,
    which is harmless because comparison is done on coverage, not on shape.
    """
    if not cells:
        return []

    rows: dict[tuple[object, float, float], list[tuple[float, float]]] = {}
    for x0, y0, x1, y1, key in cells:
        rows.setdefault((key, y0, y1), []).append((x0, x1))

    horizontal: list[tuple[object, float, float, float, float]] = []
    for (key, y0, y1), spans in rows.items():
        spans.sort()
        cur_x0, cur_x1 = spans[0]
        for x0, x1 in spans[1:]:
            if x0 == cur_x1:
                cur_x1 = x1
            else:
                horizontal.append((key, y0, y1, cur_x0, cur_x1))
                cur_x0, cur_x1 = x0, x1
        horizontal.append((key, y0, y1, cur_x0, cur_x1))

    columns: dict[tuple[object, float, float], list[tuple[float, float]]] = {}
    for key, y0, y1, x0, x1 in horizontal:
        columns.setdefault((key, x0, x1), []).append((y0, y1))

    merged: list[tuple[Rect, object]] = []
    for (key, x0, x1), spans in columns.items():
        spans.sort()
        cur_y0, cur_y1 = spans[0]
        for y0, y1 in spans[1:]:
            if y0 == cur_y1:
                cur_y1 = y1
            else:
                merged.append((Rect(x0, cur_y0, x1, cur_y1), key))
                cur_y0, cur_y1 = y0, y1
        merged.append((Rect(x0, cur_y0, x1, cur_y1), key))

    merged.sort(
        key=lambda item: (
            item[0].min_y,
            item[0].min_x,
            item[0].max_y,
            item[0].max_x,
            repr(item[1]),
        )
    )
    return merged


def select_zones(
    tile: Rect, images: Iterable[CatalogImage], period: str
) -> list[Zone]:
    """Partition ``tile`` by the finest on-shelf image of ``period``.

    Inactive rows and rows from other survey periods are ignored. Ties on
    resolution are broken by ascending ``sheet_no``.
    """
    candidates: list[tuple[CatalogImage, Rect]] = []
    for img in images:
        if not img.is_active or img.period != period:
            continue
        overlap = intersect(tile, img.rect)
        if overlap is not None:
            candidates.append((img, overlap))

    x_edges, y_edges = _grid_edges(
        tile, [overlap for _, overlap in candidates]
    )

    cells: list[tuple[float, float, float, float, Winner]] = []
    for i in range(len(x_edges) - 1):
        x0, x1 = x_edges[i], x_edges[i + 1]
        if not x1 > x0:
            continue
        cx = (x0 + x1) / 2.0
        for j in range(len(y_edges) - 1):
            y0, y1 = y_edges[j], y_edges[j + 1]
            if not y1 > y0:
                continue
            cy = (y0 + y1) / 2.0
            best: CatalogImage | None = None
            best_key: tuple[float, str] | None = None
            for img, overlap in candidates:
                if (
                    overlap.min_x <= cx <= overlap.max_x
                    and overlap.min_y <= cy <= overlap.max_y
                ):
                    key = (img.resolution, img.sheet_no)
                    if best_key is None or key < best_key:
                        best_key = key
                        best = img
            winner = best.winner if best is not None else BLANK
            cells.append((x0, y0, x1, y1, winner))

    return [Zone(rect, key) for rect, key in _merge_cells(cells)]


def selection_for_tile(
    tile_no: str,
    tile: Rect,
    period: str,
    images: Iterable[CatalogImage],
) -> tuple[str, Rect, list[Zone], float, bool]:
    """Return ``(tile_no, tile, zones, blank_area, fully_blank)``."""
    zones = select_zones(tile, images, period)
    blank_area = sum(z.rect.area for z in zones if z.is_blank)
    fully_blank = math.isclose(
        blank_area, tile.area, rel_tol=1e-9, abs_tol=1e-9
    )
    return tile_no, tile, zones, blank_area, fully_blank


def detect_changed_images(
    period: str,
    old_images: Iterable[CatalogImage],
    new_images: Iterable[CatalogImage],
) -> list[ChangedImage]:
    """Images added, removed or modified in ``period`` between two snapshots.

    Presence, footprint, resolution, file path and on/off-shelf state all
    count. A period change makes the image appear in one side only and is
    therefore reported as well.
    """
    old_period = {img.sheet_no: img for img in old_images if img.period == period}
    new_period = {img.sheet_no: img for img in new_images if img.period == period}

    changed: list[ChangedImage] = []
    for sheet_no in sorted(set(old_period) | set(new_period)):
        old = old_period.get(sheet_no)
        new = new_period.get(sheet_no)
        if old is None or new is None:
            changed.append(ChangedImage(sheet_no, old, new))
            continue
        if (
            old.rect != new.rect
            or old.resolution != new.resolution
            or old.file_path != new.file_path
            or old.is_active != new.is_active
        ):
            changed.append(ChangedImage(sheet_no, old, new))
    return changed


def affected_tile_nos(
    tiles: Iterable[tuple[str, Rect]], changed: Iterable[ChangedImage]
) -> list[str]:
    """Tiles with a positive-area overlap with any changed image coverage.

    Both the *old* and the *new* footprint count, so removals (old only) and
    additions (new only) are covered.
    """
    areas: list[Rect] = []
    for item in changed:
        if item.old is not None:
            areas.append(item.old.rect)
        if item.new is not None:
            areas.append(item.new.rect)

    hit: list[str] = []
    for tile_no, tile in tiles:
        if any(intersect(tile, area) is not None for area in areas):
            hit.append(tile_no)
    return hit


def _winner_at(zones: list[Zone], x: float, y: float) -> Winner:
    for zone in zones:
        if zone.rect.min_x <= x <= zone.rect.max_x and (
            zone.rect.min_y <= y <= zone.rect.max_y
        ):
            return zone.winner
    return BLANK


def compare_zones(
    tile: Rect, old_zones: list[Zone], new_zones: list[Zone]
) -> list[ChangedRegion]:
    """Positive-area parts of ``tile`` whose serving image differs.

    The comparison is coverage-based: zone boundaries are overlaid and each
    resulting cell is checked by its centre point. A different zone layout
    that still serves every point with the same sheet/resolution/path yields
    no changed regions.
    """
    rects = [z.rect for z in old_zones] + [z.rect for z in new_zones]
    x_edges, y_edges = _grid_edges(tile, rects)

    cells: list[tuple[float, float, float, float, tuple[Winner, Winner]]] = []
    for i in range(len(x_edges) - 1):
        x0, x1 = x_edges[i], x_edges[i + 1]
        if not x1 > x0:
            continue
        cx = (x0 + x1) / 2.0
        for j in range(len(y_edges) - 1):
            y0, y1 = y_edges[j], y_edges[j + 1]
            if not y1 > y0:
                continue
            cy = (y0 + y1) / 2.0
            old = _winner_at(old_zones, cx, cy)
            new = _winner_at(new_zones, cx, cy)
            if old != new:
                cells.append((x0, y0, x1, y1, (old, new)))

    return [
        ChangedRegion(rect, key[0], key[1])  # type: ignore[index]
        for rect, key in _merge_cells(cells)
    ]

