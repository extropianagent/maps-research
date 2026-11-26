"""Bounding boxes and adaptive subdivision.

Search APIs cap results per query (Google Text Search: 60). The only way to see
past the cap is to ask the same question about smaller areas. `BBox.split()` is
the quadtree step; retrieval.py decides when to take it.

Boxes that cross the antimeridian are not supported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_008.8
M_PER_DEG_LAT = math.pi * EARTH_RADIUS_M / 180.0


@dataclass(frozen=True)
class BBox:
    south: float
    west: float
    north: float
    east: float

    def __post_init__(self) -> None:
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError(f"invalid latitudes: {self.south}, {self.north}")
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError(f"invalid longitudes (antimeridian unsupported): {self.west}, {self.east}")

    @classmethod
    def parse(cls, raw: str) -> "BBox":
        """Parse 'south,west,north,east'."""
        parts = [float(p) for p in raw.split(",")]
        if len(parts) != 4:
            raise ValueError("bbox must be 'south,west,north,east'")
        return cls(*parts)

    @classmethod
    def around(cls, lat: float, lng: float, radius_m: float) -> "BBox":
        """Square box enclosing a circle of radius_m around a point."""
        dlat = radius_m / M_PER_DEG_LAT
        dlng = radius_m / (M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-6))
        return cls(
            max(lat - dlat, -90.0), max(lng - dlng, -180.0), min(lat + dlat, 90.0), min(lng + dlng, 180.0)
        )

    @property
    def center(self) -> tuple[float, float]:
        return ((self.south + self.north) / 2, (self.west + self.east) / 2)

    @property
    def height_m(self) -> float:
        return (self.north - self.south) * M_PER_DEG_LAT

    @property
    def width_m(self) -> float:
        lat = self.center[0]
        return (self.east - self.west) * M_PER_DEG_LAT * math.cos(math.radians(lat))

    @property
    def circumradius_m(self) -> float:
        """Radius of the smallest circle centred on the box that covers it."""
        return math.hypot(self.width_m, self.height_m) / 2

    def contains(self, lat: float | None, lng: float | None) -> bool:
        if lat is None or lng is None:
            return False
        return self.south <= lat <= self.north and self.west <= lng <= self.east

    def split(self) -> list["BBox"]:
        """Four equal quadrants."""
        mlat, mlng = self.center
        return [
            BBox(self.south, self.west, mlat, mlng),
            BBox(self.south, mlng, mlat, self.east),
            BBox(mlat, self.west, self.north, mlng),
            BBox(mlat, mlng, self.north, self.east),
        ]

    def key(self) -> str:
        return f"{self.south:.5f},{self.west:.5f},{self.north:.5f},{self.east:.5f}"

    def polygon(self) -> list[tuple[float, float]]:
        """Closed ring of (lat, lng) corners, counter-clockwise."""
        return [
            (self.south, self.west),
            (self.south, self.east),
            (self.north, self.east),
            (self.north, self.west),
            (self.south, self.west),
        ]


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))
