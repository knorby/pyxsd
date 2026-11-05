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
The family's pinned manifest (`sources.toml`, SHA-256 verified, license
evidence in `SOURCES.md`) installs it under the gitignored `.cache/` and
feeds every workflow in this family:

```console
$ uv run python examples/musicxml/download_sources.py
```

## Run it

From the repository root:

```console
$ uv run python examples/musicxml/note_stats.py
```

The command writes `examples/musicxml/out/note-stats.xml` (override with
`--output`). `NoteStats` replaces the score with a summary tree:

```xml
<noteStats totalNotes="165" totalDuration="288" rests="0"
           parts="4" measures="40"
           lowestPitch="42" highestPitch="76"/>
```

Pitches use MIDI-style numbering (C4 = 60). The score validates with no
errors against MusicXML 4.0, so the example is also a conformance test —
see `tests/test_example_applications.py` (the test skips when the schema
has not been fetched).

## Serve

The family also includes a local FastAPI use for curated synthesis and
submitted score validation. It reuses this family's pinned schema, the
committed score, and the WAV/preview renderers:

```sh
uv run python examples/musicxml/download_sources.py
uv run --group examples-fastapi uvicorn examples.musicxml.server:app --port 8000
```

Open <http://127.0.0.1:8000/> for the sample browser.
`/render/chorale` returns WAV audio; add `?format=svg` for the preview.
Submit a score for diagnostics and its merged sounding-note count:

```sh
curl --data-binary @score.musicxml -H 'Content-Type: application/xml' \
    http://127.0.0.1:8000/validate
```

Uploads use the strict parser profile, so the committed score's legacy
external DOCTYPE is refused there; the trusted curated render still reads
the original file. This is a local example, not a deployment recipe for
untrusted uploads.

## What it exercises

- Real-world schema loading: `musicxml.xsd` (~1,400 complex types) with
  cross-namespace `xs:import` of the XML and XLink schemas.
- Namespaced validation of a genuine 51 KB score: four parts, 40 measures,
  165 notes, chordal harmony.
- The transform API on local names: `getElementsByName(root, "note")` and
  `find("pitch", note)` work even though element names are Clark-qualified
  in namespaced mode.

## Hear it: WAV synthesis and score preview

MusicXML in, audio and a score preview out — pyxsd validates the score
against the pinned MusicXML 4.0 schema first, and the renderers read the
bound tree, so a missing or mistyped pitch does not reach the WAV: what
you hear is what pyxsd accepted. Both scripts are stdlib-only:

```console
$ uv run python examples/musicxml/synthesize.py [-o out.wav]
$ uv run python examples/musicxml/render_preview.py [-o out.svg]
```

Each parses `examples/musicxml/instance.xml` by default, prints the
validation verdict, and writes under `examples/musicxml/out/` unless
`-o` says otherwise. `synthesize.py` writes a 16-bit mono 22.05 kHz WAV;
`render_preview.py` writes a standalone SVG.

How it works:

1. **Validate.** `common.schemas.compile_schema()` compiles the pinned
   4.0 XSD once per process; `schema.parse(...)` binds the score. The
   bundled chorale validates with zero errors.
2. **Read notes.** `synthesize.notes(document)` walks the partwise tree
   and returns `{part, midi, start_q, dur_q}` spans in quarter notes:
   `<duration>` divided by the current `<divisions>` (re-read at every
   `<attributes>`), `<forward>` advancing time as silence, and tie pairs
   merged into one attack.
3. **Schedule.** `synthesize.schedule(notes, tempo_qpm=96.0)` converts
   quarters to seconds (`60 / tempo`) and MIDI numbers to frequencies
   with A4 = 440 Hz.
4. **Synthesize.** `synthesize.synthesize(document, dest)` adds a sine
   plus second and third harmonics per note under a 15 ms linear attack
   and a release clamped to 30% of the note, peak-normalizes to 0.89,
   and writes PCM with `wave` + `array`. No randomness or timestamps:
   repeated runs are byte-identical.
5. **Preview.** `render_preview.render_preview(document, width=1000)`
   draws one five-line staff per part, measure bars, noteheads at their
   diatonic `step`/`octave` positions, written `♯`/`♭` markers for
   `alter`, and tie arcs.

The score is Bach's *Christ unser Herr zum Jordan kam*, BWV 66.6: four
parts (S/A/T/B), 4/4, divisions 2, `<sound tempo="96"/>`, 165 notated
notes in 10 measures. The two tie pairs merge to **163 sounding spans**,
and the last note ends at 36 quarters — **22.5 seconds**. The final
`<forward>` is score padding, not sound, so it is not rendered.

### Why the walk uses `_children_` instead of child attributes

MusicXML's `note` content model is a choice (`pitch`/`unpitched`/`rest`)
with repeated `tie` children, and pyxsd binds those through reference
sites. On such schemas, child-attribute access can legitimately return
`None` for `<pitch>` and `<tie>` even though the bound children exist.
This workflow therefore reads the documented `XMLNode` structure —
`_name_`, `_children_`, `_value_`, `_attribs_` — which is exact and in
document order. Element *attributes* (`step`, `alter`, `octave`,
`duration`, `tie/@type`, `sound/@tempo`) still bind normally.

### Interfaces for the server

```python
notes(document) -> list[dict]
# [{part: "P1", midi: 69, start_q: 1.0, dur_q: 0.5}, ...]  # quarters

schedule(notes, *, tempo_qpm=96.0) -> list[tuple[float, float, float]]
# [(t0_seconds, dur_seconds, freq_hz), ...]

synthesize(document, dest: Path, *, tempo_qpm=96.0, sample_rate=22_050) -> Path

render_preview(document, *, width=1000) -> str   # SVG text
```

The family server calls these same functions; nothing here reads the
network or writes outside `dest`.

### Synthesis limitations

- **Fixed tempo.** The default is 96 qpm (as in this score); `--tempo`
  overrides it. There is no tempo-map support (no mid-score tempo
  changes, no fermatas — the score's six fermatas are ignored).
- **No repeats or voltas.** The bundled score has none; a score with
  `repeat`/`ending` elements would need navigation expansion first.
- **No dynamics or articulation modelling**, no per-instrument voices,
  no velocity, no reverb. Every note is the same additive sine timbre.
- **Ties only.** `<tied>` notation is ignored (only sounding `<tie>` is
  read); slurs/beams/stems are not drawn. The preview has no key
  signature, so every `alter` gets an explicit accidental marker.
- **Audio ends with the last note.** Trailing rest/forward padding in
  the score is not padded into the WAV.

## Attribution

The score is by J. S. Bach (1685–1750) and is in the public domain. The
file is taken from the music21 corpus, whose software is BSD-licensed.
The MusicXML 4.0 XSD set is Copyright © 2004-2021 the Contributors to
the MusicXML Specification, published by the W3C Music Notation
Community Group under the [W3C Community Final Specification Agreement
(FSA)](https://www.w3.org/community/about/agreements/final/); the pinned
revision and verbatim terms are quoted in [`SOURCES.md`](SOURCES.md).
