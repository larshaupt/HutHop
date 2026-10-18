"""Swiss hiking time (Schweizer Wanderwege, "Wanderzeitberechnung" version 2020.2).

Minutes per horizontal km as a 15th-degree polynomial in slope (percent), applied piece by piece
along a 3D line. 4.2 km/h on the flat; moderate descents are about as fast as flat walking.
"""

import numpy as np

COEFFS = np.array([
    14.271, 0.36992, 0.025922, -0.0014384, 3.2105e-05, 8.1542e-06, -9.0261e-08, -2.0757e-08,
    1.0192e-10, 2.8588e-11, -5.7466e-14, -2.1842e-14, 1.5176e-17, 8.6894e-18, -1.3584e-21, -1.4026e-21,
])
MAX_SLOPE = 40.0  # the polynomial is unrealistic beyond ±40 %; time grows linearly with slope there
FLAT_MIN_PER_KM = COEFFS[0]
SAMPLE_M = 20.0  # merge vertices into ~20 m pieces so elevation noise doesn't create fake steep slopes


def min_per_km(slope_pct: np.ndarray | float) -> np.ndarray:
    slope = np.asarray(slope_pct, dtype=float)
    clamped = np.clip(slope, -MAX_SLOPE, MAX_SLOPE)
    pace = np.polynomial.polynomial.polyval(clamped, COEFFS)
    return pace * np.maximum(1.0, np.abs(slope) / MAX_SLOPE)


def pieces(coords: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Horizontal lengths and height differences of ~SAMPLE_M pieces along a 3D line (N×3, metres)."""
    keep = [0]
    start = coords[0, :2]
    for i in range(1, len(coords)):
        if np.hypot(*(coords[i, :2] - start)) >= SAMPLE_M or i == len(coords) - 1:
            keep.append(i)
            start = coords[i, :2]
    pts = coords[keep]
    dl = np.hypot(*np.diff(pts[:, :2], axis=0).T)
    dz = np.diff(pts[:, 2])
    nonzero = dl > 0
    return dl[nonzero], dz[nonzero]


def line_metrics(coords: np.ndarray) -> dict:
    """Length, ascent, descent and hiking time in both directions of one 3D line."""
    dl, dz = pieces(coords)
    if not len(dl):
        return {"length": 0.0, "up": 0.0, "down": 0.0, "t_fwd": 0.0, "t_back": 0.0}
    slope = dz / dl * 100
    return {
        "length": float(dl.sum()),
        "up": float(dz[dz > 0].sum()),
        "down": float(-dz[dz < 0].sum()),
        "t_fwd": float((dl / 1000 * min_per_km(slope)).sum()),
        "t_back": float((dl / 1000 * min_per_km(-slope)).sum()),
    }
