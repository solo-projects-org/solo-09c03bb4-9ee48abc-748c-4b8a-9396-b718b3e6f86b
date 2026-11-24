"""Exact rectangle coverage math.

All rectangles live in the *same* planar coordinate system (the catalog
declares which one). Rectangles are axis-aligned and stored as
``(min_x, min_y, max_x, max_y)``.

Coverage of a target rectangle by a set of candidate rectangles is decided
exactly with coordinate compression: every rectangle edge projects onto an
x/y grid, so inside any resulting cell every candidate either contains the
whole cell or misses it entirely. No rasterization tolerance is involved.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config


class GeometryError(ValueError):
    """Raised when rectangle data is malformed."""


@dataclass(frozen=True)
class Rect:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        if not (self.min_x < self.max_x and self.min_y < self.max_y):
            raise GeometryError(
                f"degenerate rectangle: min must be strictly smaller than max, "
                f"got {self!r}"
            )

    @property
    def area(self) -> float:
        return (self.max_x - self.min_x) * (self.max_y - self.min_y)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.min_x, self.min_y, self.max_x, self.max_y)


def rects_intersect(a: Rect, b: Rect) -> bool:
    return (
        a.min_x < b.max_x
        and a.max_x > b.min_x
        and a.min_y < b.max_y
        and a.max_y > b.min_y
    )


def intersect(a: Rect, b: Rect) -> Rect | None:
    """Return the overlap rectangle, or ``None`` when they do not overlap."""
    min_x = max(a.min_x, b.min_x)
    min_y = max(a.min_y, b.min_y)
    max_x = min(a.max_x, b.max_x)
    max_y = min(a.max_y, b.max_y)
    if min_x < max_x and min_y < max_y:
        return Rect(min_x, min_y, max_x, max_y)
    return None


def find_uncovered_rectangle(
    target: Rect, coverers: list[Rect]
) -> Rect | None:
    """Return one positive-area sub-rectangle of ``target`` not covered.

    The union of ``coverers`` is clipped against ``target``. If the union
    covers ``target`` completely the result is ``None``; otherwise the first
    uncovered grid cell (itself a valid rectangle in the same CRS) is
    returned. Cells with zero width or height are skipped, so coincident
    edges never produce spurious "gaps".
    """
    xs = {target.min_x, target.max_x}
    ys = {target.min_y, target.max_y}
    clipped: list[Rect] = []
    for rect in coverers:
        overlap = intersect(target, rect)
        if overlap is None:
            continue
        clipped.append(overlap)
        xs.add(overlap.min_x)
        xs.add(overlap.max_x)
        ys.add(overlap.min_y)
        ys.add(overlap.max_y)

    x_edges = sorted(xs)
    y_edges = sorted(ys)
    if len(x_edges) * len(y_edges) > config.MAX_GRID_CELLS:
        raise GeometryError(
            "coverage grid exceeds configured safety limit "
            f"({config.MAX_GRID_CELLS} cells)"
        )

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
            covered = any(
                r.min_x <= cx <= r.max_x and r.min_y <= cy <= r.max_y
                for r in clipped
            )
            if not covered:
                return Rect(x0, y0, x1, y1)
    return None
