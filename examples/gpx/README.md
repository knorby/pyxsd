# GPX example

A real geospatial format: a **hand-authored subset** of GPX 1.1
(`gpx` → `trk` → `trkseg` → `trkpt`, with `ele` and `lat`/`lon`). The
schema is a small subset written for this example, not the official GPX
schema (see <https://www.topografix.com/gpx.asp>); it uses no namespace.

## Run it

From this directory:

```console
$ uv run pyxsd -i instance.xml -s schema.xsd -k -o /dev/null \
      -t 'TrackStats()' -o track-stats.xml
```

`TrackStats` replaces the track with a summary tree computing
great-circle distance and elevation gain/loss:

```xml
<trackStats pointCount="5" distanceMeters="280.5"
            elevationGain="10.0" elevationLoss="5.0"
            minElevation="100.0" maxElevation="108.0"/>
```

The example validates cleanly in strict mode and is covered by
`tests/test_example_applications.py`.
