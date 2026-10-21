"""Route-line geometry of tours: how much of a tour retraces its own trail, and its overall shape.

Each edge's route line gives (footprint(), computed by build_edges and stored packed on the edge, or decoded
lazily from the route line for edges stored before that)
  - a footprint: the sorted ids of the CELL_M grid cells it passes through, sampled every STEP_M, so two edges on
    the same trail share cells and their overlap is |cells_a ∩ cells_b| (about CELL_M metres of trail per cell);
  - SAMPLES evenly spaced points, enough to judge the shape of a whole tour.
Footprints keeps them per edge (the graph is cached across requests), with a coarse COARSE_CELLS footprint to skip
pairs of edges that can't share a cell, and caches pair overlaps.
"""

import math
import zlib

import numpy as np

from app.planner import polyline

CELL_M = 40.0
STEP_M = 10.0
SAMPLES = 8
# Cell ids fit in 32 bits: (x - CELL_X0) << CELL_Y_BITS | (y - CELL_Y0), x and y in cells of local metres. Covers
# 45.3–48.3° N; cells outside are clipped to the edge of that range (the trail network is Swiss only).
CELL_X0, CELL_Y0, CELL_Y_BITS = 11_000, 126_000, 13
COARSE_CELLS = 25  # cells per side of a coarse cell (1 km), for the quick "can't overlap" check
MAX_CACHED_PAIRS = 400_000  # edge-pair overlaps kept (~40 MB); the cache starts over when full
M_PER_DEG_LAT = 111_200.0
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(46.6))  # local metres around the middle of Switzerland


def footprint(geometry: str) -> tuple[np.ndarray, np.ndarray]:
    """Cell ids (sorted, unique, int32) and SAMPLES points (local metres) of an encoded route line."""
    pts = np.array(polyline.decode(geometry or ""), dtype=float).reshape(-1, 2)
    xy = np.column_stack((pts[:, 1] * M_PER_DEG_LON, pts[:, 0] * M_PER_DEG_LAT))
    if len(xy) < 2:
        return np.zeros(0, dtype=np.int32), np.repeat(xy, SAMPLES, axis=0) if len(xy) else np.zeros((0, 2))
    seg = np.hypot(*np.diff(xy, axis=0).T)
    along = np.concatenate(([0.0], np.cumsum(seg)))
    dense = np.arange(0, along[-1] + STEP_M, STEP_M)
    x, y = np.interp(dense, along, xy[:, 0]), np.interp(dense, along, xy[:, 1])
    cx = np.clip(np.floor(x / CELL_M).astype(np.int64) - CELL_X0, 0, (1 << (31 - CELL_Y_BITS)) - 1)
    cy = np.clip(np.floor(y / CELL_M).astype(np.int64) - CELL_Y0, 0, (1 << CELL_Y_BITS) - 1)
    cells = np.unique((cx << CELL_Y_BITS | cy).astype(np.int32))
    at = np.linspace(0, along[-1], SAMPLES)
    return cells, np.column_stack((np.interp(at, along, xy[:, 0]), np.interp(at, along, xy[:, 1])))


def pack(cells: np.ndarray, samples: np.ndarray) -> tuple[bytes, bytes]:
    """Compact blobs for the edge table: cell ids delta-encoded and compressed (~400 bytes), samples as float64."""
    return zlib.compress(np.diff(cells, prepend=np.int32(0)).astype(np.int32).tobytes()), \
        samples.astype(np.float64).tobytes()


def unpack(cells: bytes, samples: bytes) -> tuple[np.ndarray, np.ndarray]:
    return np.cumsum(np.frombuffer(zlib.decompress(cells), dtype=np.int32), dtype=np.int32), \
        np.frombuffer(samples, dtype=np.float64).reshape(-1, 2)


class Footprints:
    def __init__(self, geometry: dict[int, str], packed: dict[int, tuple[bytes, bytes]] | None = None):
        self.geometry = geometry  # encoded route lines, for edges without a stored footprint
        self.packed = packed if packed is not None else {}
        self._cells: dict[int, np.ndarray] = {}
        self._coarse: dict[int, frozenset[int]] = {}
        self._samples: dict[int, np.ndarray] = {}
        self._parts: dict[int, tuple | None] = {}  # per edge: what shape() needs (see _decode)
        self._overlap: dict[int, int] = {}

    def _decode(self, edge_id: int) -> None:
        stored = self.packed.get(edge_id)
        cells, samples = unpack(*stored) if stored else footprint(self.geometry.get(edge_id) or "")
        self._cells[edge_id], self._samples[edge_id] = cells, samples
        mask = (1 << CELL_Y_BITS) - 1
        coarse = ((cells >> CELL_Y_BITS) // COARSE_CELLS) << CELL_Y_BITS | (cells & mask) // COARSE_CELLS
        self._coarse[edge_id] = frozenset(np.unique(coarse).tolist())
        # For shape(): the samples' own length and shoelace sum, and the first and last point, as plain floats.
        if len(samples):
            x, y = samples[:, 0].tolist(), samples[:, 1].tolist()
            length = sum(math.hypot(x[j + 1] - x[j], y[j + 1] - y[j]) for j in range(len(x) - 1))
            shoelace = sum(x[j] * y[j + 1] - x[j + 1] * y[j] for j in range(len(x) - 1))
            self._parts[edge_id] = (len(x), length, shoelace, x[0], y[0], x[-1], y[-1])
        else:
            self._parts[edge_id] = None

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
        key = (a << 32 | b) if a < b else (b << 32 | a)
        n = self._overlap.get(key)
        if n is None:
            if len(self._overlap) >= MAX_CACHED_PAIRS:
                self._overlap.clear()  # pairs recur mostly within one search, so starting over costs little
            big, small = self.cells(a), self.cells(b)
            if self._coarse[a].isdisjoint(self._coarse[b]) or not len(small) or not len(big):
                n = 0
            else:
                if len(big) < len(small):
                    big, small = small, big
                i = np.searchsorted(big, small)
                i[i == len(big)] = 0
                n = int(np.count_nonzero(big[i] == small))
            self._overlap[key] = n
        return n

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
        some area too. Length and area (shoelace formula) are summed from per-edge parts plus the joins between
        consecutive edges, which is the same as computing them on all samples joined together."""
        n = length = shoelace = 0.0
        first = last = None
        for e in edge_ids:
            if e not in self._parts:
                self._decode(e)
            part = self._parts[e]
            if part is None:
                continue
            count, part_length, part_shoelace, x0, y0, x1, y1 = part
            if last is not None:  # join from the previous edge's last sample to this edge's first
                length += math.hypot(x0 - last[0], y0 - last[1])
                shoelace += last[0] * y0 - x0 * last[1]
            else:
                first = (x0, y0)
            n += count
            length += part_length
            shoelace += part_shoelace
            last = (x1, y1)
        if n < 3 or length <= 0:
            return 0.0
        gap = math.hypot(last[0] - first[0], last[1] - first[1])
        progress = gap / length
        area = 0.5 * abs(shoelace + last[0] * first[1] - first[0] * last[1])
        perimeter = length + gap
        roundness = 4 * math.pi * area / perimeter**2
        loop = max(0.0, 1 - progress / LOOP_PROGRESS)
        return max(0.0, min(1.0, max(progress / PROGRESS_GOOD, loop * roundness / ROUNDNESS_GOOD)))


# Calibrated on all 2–3 night tours found in Switzerland (Oct 2026): median progress 0.6, 90th percentile 0.77.
PROGRESS_GOOD = 0.75   # a traverse whose start and end are this share of its length apart scores 1
LOOP_PROGRESS = 0.3    # below this, a tour counts as (partly) a loop, fully at 0
ROUNDNESS_GOOD = 0.45  # a loop this round scores 1 (a circle is 1, a square 0.79, a 3:1 rectangle 0.59)
