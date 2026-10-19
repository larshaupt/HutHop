"""Route-line geometry of tours: how much of a tour retraces its own trail, and its overall shape.

Each edge's route line is decoded once (lazily; the graph is cached across requests) into
  - a footprint: the set of CELL_M grid cells it passes through, sampled every STEP_M, so two edges on the same
    trail share cells and their overlap is |cells_a ∩ cells_b| (about CELL_M metres of trail per cell);
  - SAMPLES evenly spaced points, enough to judge the shape of a whole tour.
"""

import math

import numpy as np

from app.planner import polyline

CELL_M = 40.0
STEP_M = 10.0
SAMPLES = 8
M_PER_DEG_LAT = 111_200.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(46.6))  # local metres around the middle of Switzerland


class Footprints:
    def __init__(self, geometry: dict[int, str]):
        self.geometry = geometry
        self._cells: dict[int, np.ndarray] = {}
        self._samples: dict[int, np.ndarray] = {}
        self._overlap: dict[tuple[int, int], int] = {}

    def _decode(self, edge_id: int) -> None:
        pts = np.array(polyline.decode(self.geometry.get(edge_id) or ""), dtype=float).reshape(-1, 2)
        xy = np.column_stack((pts[:, 1] * M_PER_DEG_LON, pts[:, 0] * M_PER_DEG_LAT))
        if len(xy) < 2:
            self._cells[edge_id] = np.zeros(0, dtype=np.int64)
            self._samples[edge_id] = np.repeat(xy, SAMPLES, axis=0) if len(xy) else np.zeros((0, 2))
            return
        seg = np.hypot(*np.diff(xy, axis=0).T)
        along = np.concatenate(([0.0], np.cumsum(seg)))
        dense = np.arange(0, along[-1] + STEP_M, STEP_M)
        x, y = np.interp(dense, along, xy[:, 0]), np.interp(dense, along, xy[:, 1])
        cells = np.floor(x / CELL_M).astype(np.int64) * 1_000_000 + np.floor(y / CELL_M).astype(np.int64)
        self._cells[edge_id] = np.unique(cells)
        at = np.linspace(0, along[-1], SAMPLES)
        self._samples[edge_id] = np.column_stack((np.interp(at, along, xy[:, 0]), np.interp(at, along, xy[:, 1])))

    def cells(self, edge_id: int) -> np.ndarray:
        if edge_id not in self._cells:
            self._decode(edge_id)
        return self._cells[edge_id]

    def samples(self, edge_id: int) -> np.ndarray:
        if edge_id not in self._samples:
            self._decode(edge_id)
        return self._samples[edge_id]

    def overlap(self, a: int, b: int) -> int:
        """Grid cells shared by two edges' route lines."""
        key = (a, b) if a < b else (b, a)
        if key not in self._overlap:
            self._overlap[key] = len(np.intersect1d(self.cells(a), self.cells(b), assume_unique=True))
        return self._overlap[key]

    def repeated(self, earlier: list[int], edge_id: int) -> float:
        """Share (0–1) of an edge's route line already walked on the earlier edges of a tour."""
        total = len(self.cells(edge_id))
        if not total or not earlier:
            return 0.0
        return min(1.0, sum(self.overlap(e, edge_id) for e in earlier) / total)

    def retraced(self, edge_ids: list[int]) -> float:
        """Share (0–1) of a whole tour's trail walked a second time."""
        total = sum(len(self.cells(e)) for e in edge_ids)
        if not total:
            return 0.0
        return min(1.0, sum(self.repeated(edge_ids[:i], e) * len(self.cells(e)) for i, e in enumerate(edge_ids)) / total)

    def shape(self, edge_ids: list[int]) -> float:
        """0–1: how much the tour looks like a traverse (A to B) or a loop, rather than an out-and-back or a zig-zag.

        Uses the sampled route: progress = straight distance start→end / route length (a traverse), and
        roundness = 4π·area / perimeter² of the route closed back to its start (1 for a circle). Roundness only
        counts for tours that end near their start (LOOP_PROGRESS), since closing any curved traverse encloses
        some area too."""
        pts = np.concatenate([self.samples(e) for e in edge_ids])
        if len(pts) < 3:
            return 0.0
        length = float(np.hypot(*np.diff(pts, axis=0).T).sum())
        if length <= 0:
            return 0.0
        progress = float(np.hypot(*(pts[-1] - pts[0]))) / length
        closed = np.vstack((pts, pts[:1]))
        area = 0.5 * abs(float(np.dot(closed[:-1, 0], closed[1:, 1]) - np.dot(closed[1:, 0], closed[:-1, 1])))
        perimeter = length + float(np.hypot(*(pts[-1] - pts[0])))
        roundness = 4 * math.pi * area / perimeter**2
        loop = max(0.0, 1 - progress / LOOP_PROGRESS)
        return max(0.0, min(1.0, max(progress / PROGRESS_GOOD, loop * roundness / ROUNDNESS_GOOD)))


# Calibrated on all 2–3 night tours found in Switzerland (Oct 2026): median progress 0.6, 90th percentile 0.77.
PROGRESS_GOOD = 0.75   # a traverse whose start and end are this share of its length apart scores 1
LOOP_PROGRESS = 0.3    # below this, a tour counts as (partly) a loop, fully at 0
ROUNDNESS_GOOD = 0.45  # a loop this round scores 1 (a circle is 1, a square 0.79, a 3:1 rectangle 0.59)
