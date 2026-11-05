# Server-rendered UBL invoices (EN 16931 / XRechnung)

Bind a real B2B e-invoice against the official OASIS **UBL 2.3** schema and
render it as one self-contained HTML page or a PDF report — both from the
same extracted facts, both server-side.

pyxsd compiles the schema once, validates and binds the document into a
typed Python tree, and the renderer is plain Python. A malformed invoice
produces diagnostics instead of a blank page.

## Run it

From the repository root:

```sh
# 1. Verify/install the pinned schema and sample corpus.
#    (Repeating this command verifies the cache without network access.)
uv run python examples/xrechnung/download_sources.py --only ubl-schema --only ubl-data

# 2. HTML invoice -> out/invoice.html
uv run python examples/xrechnung/render_html.py

# 3. PDF invoice (optional extra) -> out/invoice.pdf
uv run --group examples-reportlab python examples/xrechnung/render_pdf.py
```

Both scripts accept any UBL invoice and an explicit destination:

```sh
uv run python examples/xrechnung/render_html.py path/to/invoice.xml --out out/custom.html
uv run --group examples-reportlab python examples/xrechnung/render_pdf.py path/to/invoice.xml --out out/custom.pdf
```

Without an input argument the HTML renderer picks the first file of the
pinned KoSIT XRechnung suite
(`.cache/ubl-data/.../01.01a-INVOICE_ubl.xml`). HTML rendering needs only
pyxsd; the PDF path needs the `examples-reportlab` group. Compiling UBL 2.3
prints a handful of harmless descriptor-collision notices
(`element descriptor for 'Condition' was bound as 'Condition|2'`): they come
from the xmldsig/XAdES schemas that the UBL extension wildcard pulls in.

## Serve

The family also includes a local FastAPI use for curated rendering and
submitted invoice validation. It reuses this family's pinned UBL schema,
sample corpus, and HTML/PDF renderers:

```sh
uv run --group examples-fastapi --group examples-reportlab \
    uvicorn examples.xrechnung.server:app --port 8000
```

Open <http://127.0.0.1:8000/> for the sample browser.
`/render/01.01a-INVOICE_ubl` returns HTML; add `?format=pdf` for the PDF
report. Submit an invoice for diagnostics and the exact lexical payable
amount:

```sh
curl --data-binary @invoice.xml -H 'Content-Type: application/xml' \
    http://127.0.0.1:8000/validate
```

XSD validation does not establish EN 16931 or Schematron business-rule
conformance. This is a local example, not a deployment recipe for
untrusted uploads.

Tests:

```sh
uv run pytest tests/test_xrechnung_examples.py tests/test_xrechnung_server.py -q
uv run --group examples-reportlab pytest tests/test_xrechnung_examples.py \
    tests/test_xrechnung_server.py -q
```

## What the example shows

**One schema, real CIUS documents.** The pinned entry point is
`maindoc/UBL-Invoice-2.3.xsd`; the whole import closure is local and
relative, so compilation is offline. XRechnung 3.0 UBL invoices are authored
for UBL 2.1, but the Invoice namespace is version-neutral
(`...:Invoice-2`) and 2.3 accepts them unchanged: all 39 files in the
pinned KoSIT suite parse with zero errors.

**API pattern for this schema.** Descriptor reads such as
`document.root.ID` return `None` on this ref-based UBL binding even though
binding and XPath succeed. `invoice_facts()` therefore reads
`document.to_dict(typed=False)` and its Clark-notation keys; `document.xpath(expr,
namespaces=NS)` works too if you prefer queries. This is the same finding the
[research notes](../../../docs/superpowers/research/invoice.md) recorded.

**Exact decimals, formatting at the edge.** Amounts and quantities stay the
document's exact lexical strings in the facts: the canonical XRechnung file
writes `336.9`, so `invoice_facts(...)["totals"]["payable"] == "336.9"`, not
`336.90` and not a float. `money()` applies two decimals only when building
HTML or PDF. Quantities keep their UN/ECE `unitCode` (`XPP`, `C62`, `H87`)
so renderings cannot silently lose what the number means.

**Escaped, deterministic HTML.** Every interpolated value goes through
`common.render.esc`; HTML output is byte-identical across runs (no
timestamps, no dictionary-order leaks). PDF content is stable, but reportlab
writes a creation-date metadata field, so PDF bytes are not expected to
match across runs.

## Interfaces

```python
# examples/xrechnung/render_html.py
def invoice_facts(document) -> dict   # JSON-safe primitives
def render_html(document) -> str      # one complete HTML document
def money(value) -> str               # exact string -> "336.90" (render only)
def percent(value) -> str             # exact string -> "19"

# examples/xrechnung/render_pdf.py
def render_pdf(document, dest: Path) -> Path
```

`invoice_facts` returns `id`, `issue_date`, `due_date`, `type_code`,
`currency`, `buyer_reference`, `notes`, `supplier` and `customer` (`name`,
`address` as display lines, `tax_id`, `endpoint`, `endpoint_scheme`),
`totals` (`line_extension`, `tax_exclusive`, `tax_inclusive`, `payable`),
`allowances` (`charge`, `amount`, `reason`), `tax_subtotals` (`category`,
`percent`, `taxable_amount`, `tax_amount`), and `lines` (`id`,
`description`, `quantity`, `unit_code`, `line_extension`, `price`). Missing
optional elements are `None`; a line without `cbc:Description` falls back to
the item name.

## XSD-valid is not EN 16931 / Schematron-conformant

Read this before reusing the report as a compliance answer. The German and
European business rules (`BR-*`, `BR-DE-*`, `BR-CO-*`,
`PEPPOL-EN16931-*`) are **Schematron**, a different layer from the XSD.
Validation against `UBL-Invoice-2.3.xsd` proves structural validity —
elements, order, cardinality, lexical datatypes — not that the invoice is a
legal XRechnung. Documents that fail XRechnung rule validation routinely
pass the XSD, and a CIUS can only narrow, never widen, the schema.

For rule validation use KoSIT's validator with
[`validator-configuration-xrechnung`](https://github.com/itplr-kosit/validator-configuration-xrechnung);
pyxsd does not implement Schematron.

## Limits

- Only the XSD is compiled; Schematron, signature validation, and PDF/A-3
  packaging are out of scope.
- `ext:UBLExtensions` and signature fragments are not rendered.
- CII (`rsm:CrossIndustryInvoice`, ZUGFeRD/Factur-X) is a different document
  model and does not validate against any UBL schema.
- The PDF uses reportlab's built-in Type 1 fonts: Latin-1 text (including
  German umlauts and ß) renders; other scripts need a TTF font.
- Amounts are displayed with two decimals and no thousands separator;
  currency comes from `cbc:DocumentCurrencyCode`.

## Sources and attribution

Schema and sample provenance, revisions, and SHA-256 digests are pinned in
[`../sources.toml`](../sources.toml). License terms and the verbatim OASIS
redistribution notice are quoted in [`SOURCES.md`](SOURCES.md):

- OASIS UBL 2.3 XSDs — OASIS copyright; unmodified redistribution is
  permitted with the notice retained.
- KoSIT XRechnung test suite (`v2026-08-31`) — Apache-2.0 sample data.

Neither the schema nor the sample corpus is committed; both live in the
gitignored `.cache/`.
