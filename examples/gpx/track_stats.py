"""Compute distance and elevation statistics for a GPX track.

Run from this directory so pyxsd can find the module:

    uv run pyxsd -i instance.xml -s schema.xsd -k -o /dev/null \
        -t 'TrackStats()' -o track-stats.xml

The transform replaces the track with a small ``trackStats`` summary
tree.
"""

import math
from itertools import pairwise

from pyxsd.transforms import Transform

EARTH_RADIUS_M = 6371000.0


def haversineMeters(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two WGS84 coordinates."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dPhi = math.radians(lat2 - lat1)
    dLambda = math.radians(lon2 - lon1)
    a = math.sin(dPhi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dLambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class TrackStats(Transform):
    """Summarizes the points of every track segment in a GPX document."""

    def __init__(self, root):
        super().__init__(root)

    def __call__(self):
        points = self.getElementsByName(self.root, "trkpt")
        coords = []
        for point in points:
            elevation = self.find("ele", point)
            coords.append(
                (
                    float(point.lat),
                    float(point.lon),
                    float(elevation) if elevation is not None else None,
                )
            )

        distance = 0.0
        gain = 0.0
        loss = 0.0
        elevations = [ele for _, _, ele in coords if ele is not None]
        for (lat1, lon1, ele1), (lat2, lon2, ele2) in pairwise(coords):
            distance += haversineMeters(lat1, lon1, lat2, lon2)
            if ele1 is not None and ele2 is not None:
                delta = ele2 - ele1
                if delta > 0:
                    gain += delta
                else:
                    loss -= delta

        summary = self.makeElemObj("trackStats")
        summary._attribs_ = {
            "pointCount": str(len(coords)),
            "distanceMeters": f"{distance:.1f}",
            "elevationGain": f"{gain:.1f}",
            "elevationLoss": f"{loss:.1f}",
            "minElevation": f"{min(elevations):.1f}" if elevations else "",
            "maxElevation": f"{max(elevations):.1f}" if elevations else "",
        }
        return summary
