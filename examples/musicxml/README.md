# MusicXML example

A real document format validated against the **official MusicXML 4.0 XSD**,
in namespaced mode. The instance is Johann Sebastian Bach's four-part
chorale *Christ unser Herr zum Jordan kam*, BWV 66.6, a public-domain work
distributed with the BSD-licensed
[music21](https://github.com/cuthbertLab/music21) corpus.

MusicXML is namespace-less (no `targetNamespace`), but its schema imports
the W3C XML and XLink namespaces, so the example also exercises
cross-namespace imports.

## Fetch the schema

The MusicXML 4.0 schema set is large (~380 KB), so it is not committed.
Fetch it once:

```console
$ uv run python examples/musicxml/download_schemas.py
```

That downloads the MusicXML 4.0 release archive and extracts
`musicxml.xsd`, `xml.xsd`, and `xlink.xsd` into `schemas/` (gitignored),
rewriting the archive's absolute XML/XLink import URLs to the local files.

## Run it

From the repository root:

```console
$ uv run pyxsd -i examples/musicxml/instance.xml \
      -s examples/musicxml/schemas/musicxml.xsd \
      --namespaces strict -k -o /dev/null \
      -t 'NoteStats()' -o note-stats.xml
```

`NoteStats` replaces the score with a summary tree:

```xml
<noteStats totalNotes="165" totalDuration="288" rests="0"
           parts="4" measures="40"
           lowestPitch="42" highestPitch="76"/>
```

Pitches use MIDI-style numbering (C4 = 60). The score validates with no
errors against MusicXML 4.0, so the example is also a conformance test —
see `tests/test_example_applications.py` (the test skips when the schema
has not been fetched).

## What it exercises

- Real-world schema loading: `musicxml.xsd` (~1,400 complex types) with
  cross-namespace `xs:import` of the XML and XLink schemas.
- Namespaced validation of a genuine 51 KB score: four parts, 40 measures,
  165 notes, chordal harmony.
- The transform API on local names: `getElementsByName(root, "note")` and
  `find("pitch", note)` work even though element names are Clark-qualified
  in namespaced mode.

## Attribution

The score is by J. S. Bach (1685–1750) and is in the public domain. The
file is taken from the music21 corpus, whose software is BSD-licensed.
