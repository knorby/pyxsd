# Shared example helpers

`sources.py` verifies downloaded archives and installed cache files. `schemas.py` owns the compile options; callers compile once and reuse the returned schemas. `cda_datatypes.py` adds neutral identifier, timestamp, coding, and quantity views through `Extensions`. These helpers are example code, not a pyxsd package API.

Timestamp values retain their original precision alongside their lexical form. FHIR requires seconds in a clock value: lexical `:00` padding does not establish that the source recorded seconds. Timezone-free clocks stay timezone-free. Quantities use `Decimal`, and `nullFlavor` is not a zero or an empty clinical value.

Known OIDs use canonical system URIs; other OIDs use `urn:oid:`, and UUID roots use `urn:uuid:`. Unrecognized local roots have no inferred system URI. The identifier record still preserves the original root; conversion reports or rejects an unusable identifier rather than inventing a system.
