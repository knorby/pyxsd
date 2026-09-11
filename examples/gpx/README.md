# GPX example

A real geospatial format validated against the **official GPX 1.1
schema** (<https://www.topografix.com/gpx.asp>). The instance is a real,
public-domain ride recorded near Avignon, France: 200 trackpoints with
elevation, time, and Garmin TrackPointExtension heart-rate, cadence, and
temperature readings.

## Fetch the schema

The schema is fetched on demand rather than committed:

```console
$ uv run python download_schemas.py
```

This writes `schemas/gpx.xsd`. The example and its test are skipped when
it is absent.

## Run it

From this directory:

```console
$ uv run pyxsd -i instance.xml -s schemas/gpx.xsd -k --namespaces strict \
      -o /dev/null -t 'TrackStats()' -o track-stats.xml
```

`TrackStats` replaces the track with a summary tree computing
great-circle distance, elevation gain/loss, and the sensor averages:

```xml
<trackStats pointCount="200" distanceMeters="6455.2"
            elevationGain="127.0" elevationLoss="68.3"
            minElevation="21.9" maxElevation="84.1"
            avgHeartRate="149.8" maxHeartRate="178.0"
            avgCadence="83.9"/>
```

## What it exercises

- **Namespaced validation.** The document is parsed with
  `ParseModes.NAMESPACED` (`--namespaces strict`), so `gpx`, `trk`, and
  `trkpt` are matched by expanded name.
- **Wildcard pass-through.** GPX permits extension content from other
  namespaces with `xs:any namespace="##other" processContents="lax"`.
  The Garmin `TrackPointExtension` block is therefore bound generically;
  `TrackStats` reads `hr`/`cad`/`atemp` from that generic subtree, which
  is why the summary can report sensor values without any schema for
  them.
- **Required attributes and fixed values.** `gpx@version` is
  `use="required" fixed="1.1"`, and every `trkpt` requires `lat`/`lon`.

The schema is covered by `tests/test_example_applications.py`; the
sample data is from <https://www.viewmygpx.com/sample-gpx-files/> and is
released under CC0.
