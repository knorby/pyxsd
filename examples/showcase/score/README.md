# Score: hear the score you just validated

MusicXML in, audio and a score preview out — with pyxsd validating the
score against the official MusicXML 4.0 schema first. The renderers then
read the bound tree, so a missing or mistyped pitch does not reach the
WAV: what you hear is what pyxsd accepted.

Two commands, both stdlib-only (no audio or graphics dependency):

```console
$ uv run python examples/showcase/score/synthesize.py [-o out.wav]
$ uv run python examples/showcase/score/render_preview.py [-o out.svg]
```

Each script parses `examples/musicxml/instance.xml` by default, prints the
validation verdict, and writes under `examples/showcase/out/` unless `-o`
says otherwise. `synthesize.py` writes a 16-bit mono 22.05 kHz WAV;
`render_preview.py` writes a standalone SVG.

## Fetch the schema

The MusicXML 4.0 schema archive is pinned in `../sources.toml` and
downloaded on demand (never committed):

```console
$ uv run python examples/showcase/download_sources.py --only musicxml-schema
```

That installs `schema/musicxml.xsd` and the zip's `xml.xsd`/`xlink.xsd`
next to it under `examples/showcase/.cache/`.

## How it works

1. **Validate.** `common.schemas.compile_schema("musicxml")` compiles the
   pinned 4.0 XSD once per process; `schema.parse(...)` binds the score.
   The bundled chorale validates with zero errors.
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

## Interfaces for the server

```python
notes(document) -> list[dict]
# [{part: "P1", midi: 69, start_q: 1.0, dur_q: 0.5}, ...]  # quarters

schedule(notes, *, tempo_qpm=96.0) -> list[tuple[float, float, float]]
# [(t0_seconds, dur_seconds, freq_hz), ...]

synthesize(document, dest: Path, *, tempo_qpm=96.0, sample_rate=22_050) -> Path

render_preview(document, *, width=1000) -> str   # SVG text
```

`examples/showcase/server/app.py` calls the same two renderers; nothing
here reads the network or writes outside `dest`.

## Limitations

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
- **Audio ends with the last note.** Trailing rest/forward padding in the
  score is not padded into the WAV.

## Attribution

- **Schema:** MusicXML 4.0 XSD, Copyright © 2004-2021 the Contributors to
  the MusicXML Specification, published by the W3C Music Notation
  Community Group under the [W3C Community Final Specification
  Agreement (FSA)](https://www.w3.org/community/about/agreements/final/).
  The archive ships no separate LICENSE file; the notice above is quoted
  from the XSD header. It is downloaded on demand and not redistributed
  in this repository. See `../SOURCES.md` for the pinned revision and
  verbatim terms.
- **Score:** Johann Sebastian Bach (1685–1750), *Christ unser Herr zum
  Jordan kam*, BWV 66.6 — public domain. The XML file comes from the
  BSD-licensed music21 corpus; see `../SOURCES.md` and
  `examples/musicxml/README.md`.
