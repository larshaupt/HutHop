"""Encoded polyline format (Google, precision 5) for compact route lines in WGS84, and the same delta encoding
for one value per point (heights in metres)."""


def _put(out: list[str], delta: int) -> None:
    value = ~(delta << 1) if delta < 0 else delta << 1
    while value >= 0x20:
        out.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    out.append(chr(value + 63))


def _take(text: str, index: int) -> tuple[int, int]:
    """One signed delta starting at `index`; returns (delta, next index)."""
    shift = result = 0
    while True:
        b = ord(text[index]) - 63
        index += 1
        result |= (b & 0x1F) << shift
        shift += 5
        if b < 0x20:
            break
    return (~(result >> 1) if result & 1 else result >> 1), index


def encode(points: list[tuple[float, float]], precision: int = 5) -> str:
    """Encode (lat, lon) points."""
    factor = 10**precision
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        _put(out, ilat - prev_lat)
        _put(out, ilon - prev_lon)
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def decode(text: str, precision: int = 5) -> list[tuple[float, float]]:
    factor = 10**precision
    points, index, lat, lon = [], 0, 0, 0
    while index < len(text):
        d_lat, index = _take(text, index)
        d_lon, index = _take(text, index)
        lat += d_lat
        lon += d_lon
        points.append((lat / factor, lon / factor))
    return points


def encode_values(values: list[int]) -> str:
    """Encode whole numbers, e.g. the height in metres of each point of a route line."""
    out, prev = [], 0
    for v in values:
        _put(out, v - prev)
        prev = v
    return "".join(out)


def decode_values(text: str) -> list[int]:
    values, index, v = [], 0, 0
    while index < len(text):
        delta, index = _take(text, index)
        v += delta
        values.append(v)
    return values
