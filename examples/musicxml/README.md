# MusicXML example

A real document format: a short melody in a **hand-authored subset** of
the partwise MusicXML structure (`score-partwise` → `part` → `measure`
→ `note`). The schema is not the official MusicXML XSD — it is a small,
reviewable subset written for this example, with no namespace. (The real
MusicXML XSDs are large; see <https://www.w3.org/2021/06/musicxml40/>
for the specification.)

The melody is the opening theme of Beethoven's *Ode to Joy* (public
domain), reduced to quarter/half notes and one rest.

## Run it

From this directory:

```console
$ uv run pyxsd -i instance.xml -s schema.xsd -k -o /dev/null \
      -t 'NoteStats()' -o note-stats.xml
```

`NoteStats` replaces the score with a summary tree:

```xml
<noteStats totalNotes="15" totalDuration="16" rests="1"
           lowestPitch="60" highestPitch="67"/>
```

Pitches use MIDI-style numbering (C4 = 60). The example validates
cleanly in strict mode, so it is also a conformance test — see
`tests/test_example_applications.py`.
