# Shared reference: facet and constraint evidence

Every validation issue carries the constraint that produced it. Read the
evidence before advising; never guess the facet.

```python
report = schema.parse("doc.xml").report
for issue in report.issues:
    print(issue.code, issue.constraint)
```

- `pattern` — `issue.constraint.kind == "pattern"`; the real pattern is in
  `issue.constraint.expected`.
- `totalDigits` / `fractionDigits` — the bound is in the constraint.
- `enumeration` — the permitted values list.
- `missing-attribute` — the required attribute name is in the evidence; fix
  the exact occurrence the location points at.
- `order` / `declaration-child` — the content model position violated.
- `unknown-root` — no global declaration matches the root; the tree is
  unbindable and cannot be queried as a bound document.

Lexical violations report code `"value"` with the facet in `constraint`.

Aggregate `counts` (error/warning) are totals; `suppressed_count` reports how
many detail records were dropped by a collection budget. A verdict of zero
errors with a nonzero `suppressed_count` is incomplete, not clean.

## Mixed content

`mixed="true"` complex types allow character data between children. Stray
text is valid there; do not remove it. Re-serialization preserves text order
— warn when downstream consumers depend on whitespace fidelity.
