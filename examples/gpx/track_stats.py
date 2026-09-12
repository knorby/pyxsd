"""Compute distance, elevation, and sensor statistics for a GPX track.

The example instance is a real, public-domain ride recorded near Avignon
(see ``README.md``). Its ``<extensions>`` block carries Garmin
TrackPointExtension heart-rate, cadence, and temperature values in a
foreign namespace that the GPX schema admits with ``xs:any
namespace="##other" processContents="lax"``; the transform reads those
generically-bound extension elements to show that pass-through working.

Run from this directory so pyxsd can find the module:

    uv run python download_schemas.py
    uv run pyxsd -i instance.xml -s schemas/gpx.xsd -k --namespaces strict \
        -o /dev/null -t 'TrackStats()' -o track-stats.xml

The transform replaces the document with a small ``trackStats`` summary
tree.
"""

import math
from itertools import pairwise

from pyxsd.transforms import Transform

EARTH_RADIUS_M = 6371000.0
SENSOR_NS = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"


def haversineMeters(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two WGS84 coordinates."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dPhi = math.radians(lat2 - lat1)
    dLambda = math.radians(lon2 - lon1)
    a = math.sin(dPhi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dLambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def descendantByLocalName(node, localName):
    """Returns the first descendant whose local name matches.

    Extension content is bound generically, so its ``_name_`` is the
    Clark form (``{namespace}local``); matching the local part keeps the
    transform independent of the extension namespace.
    """
    for child in getattr(node, "_children_", ()):
        name = getattr(child, "_name_", "") or ""
        if name.split("}")[-1] == localName:
            return child
        found = descendantByLocalName(child, localName)
        if found is not None:
            return found
    return None


def sensorValue(point, localName):
    """Returns a numeric sensor reading from a trackpoint, or ``None``."""
    node = descendantByLocalName(point, localName)
    if node is None or not node._value_:
        return None
    try:
        return float(node._value_[0])
    except (TypeError, ValueError):
        return None


class TrackStats(Transform):
    """Summarizes the points of every track segment in a GPX document.

    Distance and elevation deltas are computed pairwise *within* each
    ``<trkseg>``: the gap between the last point of one segment and the
    first point of the next is not a recorded movement. Point and
    sensor counts aggregate over the whole document.
    """

    def __init__(self, root):
        super().__init__(root)

    def __call__(self):
        # Points pair up only within their own segment: the straight
        # line between the end of one recording and the start of the
        # next is not a movement the rider made.
        segments = self.getElementsByName(self.root, "trkseg")
        segmentPoints = [self.getElementsByName(segment, "trkpt") for segment in segments]
        if not segmentPoints:
            segmentPoints = [self.getElementsByName(self.root, "trkpt")]
        points = [point for segment in segmentPoints for point in segment]

        distance = 0.0
        gain = 0.0
        loss = 0.0
        elevations = []
        for segment in segmentPoints:
            coords = []
            for point in segment:
                elevation = self.find("ele", point)
                coords.append(
                    (
                        float(point.lat),
                        float(point.lon),
                        float(elevation) if elevation is not None else None,
                    )
                )
            elevations.extend(ele for _, _, ele in coords if ele is not None)
            for (lat1, lon1, ele1), (lat2, lon2, ele2) in pairwise(coords):
                distance += haversineMeters(lat1, lon1, lat2, lon2)
                if ele1 is not None and ele2 is not None:
                    delta = ele2 - ele1
                    if delta > 0:
                        gain += delta
                    else:
                        loss -= delta

        heartRates = [hr for hr in (sensorValue(p, "hr") for p in points) if hr is not None]
        cadences = [cad for cad in (sensorValue(p, "cad") for p in points) if cad is not None]

        summary = self.makeElemObj("trackStats")
        summary._attribs_ = {
            "pointCount": str(len(points)),
            "distanceMeters": f"{distance:.1f}",
            "elevationGain": f"{gain:.1f}",
            "elevationLoss": f"{loss:.1f}",
            "minElevation": f"{min(elevations):.1f}" if elevations else "",
            "maxElevation": f"{max(elevations):.1f}" if elevations else "",
            "avgHeartRate": f"{sum(heartRates) / len(heartRates):.1f}" if heartRates else "",
            "maxHeartRate": f"{max(heartRates):.1f}" if heartRates else "",
            "avgCadence": f"{sum(cadences) / len(cadences):.1f}" if cadences else "",
        }
        return summary
