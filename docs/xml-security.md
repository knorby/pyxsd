# XML security and untrusted input

pyxsd is a schema processor, not a security boundary. Its defaults favor speed
and compatibility with the stdlib parser. Protections are opt-in, finite, and
documented here so a caller can choose them deliberately.

The short version:

- **Default parsing is the stdlib.** `Schema.compile(..., xml_policy=None)` and
  `Schema.parse(..., xml_policy=None)` use `xml.etree.ElementTree` with its
  current DTD and entity behavior. `defusedxml` is installed, but it is used only
  when a policy selects it.
- **No built-in profile fetches external DTDs or entities.** The stdlib backend
  does not retrieve external references, and `XmlParserPolicy.protected()` and
  `.strict()` reject them.
- **Protections are opt-in per document.** `XmlParserPolicy.protected()` and
  `.strict()` select a defusedxml-backed parser. `XmlLimits` adds byte, depth,
  and element budgets. Neither becomes a process default.
- **Remote XSD loading is opt-in.** `RemoteResourcePolicy` is the only built-in
  way to retrieve a schema over HTTPS. Local and offline operation is the
  default, and there is no persistent remote cache.
- **XPath is code, not a sandbox.** Queries and schema assertions run in-process.
  The MCP server bounds them; the core library does not impose a timeout.

## Parser profiles

`pyxsd.XmlParserPolicy` is an immutable value. Its flags require
`use_defusedxml=True`; asking for declaration denial with the stdlib parser raises
`ValueError` rather than silently ignoring the request.

| Profile | Backend | DOCTYPE | Entity declarations | External DTD/entity references |
| --- | --- | --- | --- | --- |
| `XmlParserPolicy.default()` (or `None`) | stdlib ElementTree | allowed | allowed, expanded | never retrieved |
| `XmlParserPolicy.protected()` | defusedxml | allowed, except `SYSTEM`/`PUBLIC` identifiers | forbidden | forbidden |
| `XmlParserPolicy.strict()` | defusedxml | forbidden | forbidden | forbidden |

`protected()` is the compatibility-minded profile: a harmless internal
`<!DOCTYPE note [...]>` with an `ATTLIST` still parses, but an entity declaration
or any external identifier is refused. `strict()` is the profile for documents
that need no DTD at all.

A denial raises `pyxsd.ResourcePolicyError`, a subclass of
`pyxsd.PyXSDError`. Defusedxml's own exceptions are converted to that error with
the original exception as `__cause__`; the shared declaration guards raise
`ResourcePolicyError` directly. Malformed XML still raises
`xml.etree.ElementTree.ParseError`, which `Schema.parse` maps to `PyXSDError`.

`XmlLimits` is independent of the parser profile. Every bound is inclusive and
counted on the input as supplied:

- `max_bytes` counts a file's or binary stream's bytes. A text stream is counted
  by its UTF-8 encoding, not its character count.
- `max_depth` counts element nesting; the document element is depth 1.
- `max_elements` counts start tags.

```python
import pyxsd

profile = pyxsd.XmlParserPolicy.strict()
limits = pyxsd.XmlLimits(max_bytes=4 * 1024 * 1024, max_depth=64, max_elements=200_000)

schema = pyxsd.Schema.compile("inventory.xsd", xml_policy=profile, xml_limits=limits)
document = schema.parse("inventory.xml", xml_policy=profile, xml_limits=limits)
```

The compile-time policy applies to the main schema and every included or
imported schema it loads. It is compile-scoped: a later `Schema.parse` does not
inherit it, so pass the policy again for instance documents. The module-level
`pyxsd.parse(xml, xsd=..., xml_policy=..., xml_limits=...)` convenience applies
the same values to both the instance and the schema compile.

These parser options cannot be combined with a custom `resource_policy`.
A resource policy's `parse()` owns XML parsing and is trusted application code;
see [resource policies](#resource-policies). A pre-parsed
`xml.etree.ElementTree.Element` is also a caller-trusted boundary: no parser
options are applied to it, so validate or harden it before handing it over.

## DTDs, entities, and Expat

The default backend processes the internal DTD subset as far as Expat exposes
it: internal entity declarations are expanded, and `ATTLIST` defaults can apply.
It installs no external-resource resolver, so an external subset or external
entity is not fetched. The document is nevertheless untrusted input: an internal
entity can expand exponentially, and entity replacement text can consume memory.
That is what `protected()` and `strict()` are for.

DTD syntax and entity declarations are different decisions:

- A plain DOCTYPE with no entity or external identifier is accepted by
  `protected()`.
- Any entity declaration is refused by `protected()` and `strict()`.
- Any DOCTYPE at all is refused by `strict()`.

The parser protections are Python-level declaration interception. They do not
replace a patched Expat. Expat has had memory-safety and entity-expansion
vulnerabilities, so run pyxsd on a current CPython patch release with the
operating system's Expat security updates. No parser profile makes an outdated
Expat safe. XML 1.1 documents remain unsupported.

## Resource policies

`pyxsd.ResourcePolicy` is a protocol for callers that need to control reads:
`read(source, base=..., purpose=...)` supplies original bytes, and
`parse(data, context)` parses them. `base` is the canonical name of the including
document, not its directory. `purpose` is one of:

- `"schema"` — a caller-supplied schema, a `namespace_schemas` entry, or an
  ordinary schema dependency;
- `"schema-hint"` — a schema the instance selected through `xsi:schemaLocation`
  or `xsi:noNamespaceSchemaLocation`;
- `"instance"` — the instance document itself.

A policy is trusted application code, not a sandbox. It is responsible for
confining paths, applying parser hardening, and recording namespace evidence.
Denials should raise `ResourcePolicyError`; an `OSError` is treated as an
unavailable resource and can be downgraded to an advisory warning during schema
composition.

The default loader resolves local filesystem paths. An untrusted instance can
still name a local schema through its `schemaLocation` hint, and a schema can
name local `xs:include`/`xs:import` files; those reads are normal behavior, not
an authorization decision. If paths must be confined to a directory or a virtual
workspace, supply a `ResourcePolicy` that enforces that boundary. Do not treat a
local file path as trusted merely because it came from XML.

## Remote XSD loading

`pyxsd.RemoteResourcePolicy` is the only built-in remote loader. Selecting it
authorizes the application's remote root schema; it is never enabled implicitly.

```python
import pyxsd

remote = pyxsd.RemoteResourcePolicy(
    allowed_origins={"https://schemas.example.org"},
    allow_local_remote=False,
    allow_remote_hints=False,
    limits=pyxsd.RemoteLimits(
        response_bytes=4 * 1024 * 1024,
        closure_bytes=16 * 1024 * 1024,
        resources=32,
        depth=8,
    ),
)
schema = pyxsd.Schema.compile(
    "https://schemas.example.org/orders/orders.xsd",
    resource_policy=remote,
)
```

### Trust rules

- Every retrieval is **HTTPS**.
- The explicit main schema of `pyxsd.compile`/`Schema.compile` is the one
  authorized root read.
- A dependency of a remote document may stay on the requesting document's
  origin or the root's origin. Any other origin must be listed in
  `allowed_origins` (an HTTPS origin, not a URL).
- An instance `xsi:schemaLocation`/`xsi:noNamespaceSchemaLocation` that names a
  remote schema additionally requires `allow_remote_hints=True`.
- A local schema (or an anonymous memory stream) may reference a remote
  dependency only with `allow_local_remote=True`.
- Remote instance documents are refused. Remote loading is limited to schemas.
- A remote document cannot pivot to a local path, a `file:` URL, or another
  non-HTTPS URL. A remote schema never loads a Python overlay.
- Private or non-global resolved addresses are refused unless
  `allow_private_addresses=True`.
- Redirects are followed one request at a time. The scheme, origin, and
  addresses are revalidated before each hop, up to `limits.redirects` hops.

Once a remote policy is selected, transport and resolver failures abort the
compile, including for a document-authored `xs:include`/`xs:import`. Only the
no-policy path keeps the offline advisory behavior for a missing schema
dependency.

### Budgets and lifetime

`RemoteLimits` defaults are finite. `response_bytes` bounds one response;
`closure_bytes` bounds every downloaded body for one compilation, including
redirect and error bodies. `resources` bounds unique URLs, `depth` bounds
dependency levels, and `redirects` is a closure-wide total, not a
per-resource cap. `connect_timeout`, `read_timeout`, and `request_seconds` bound
connection, each socket read, and the whole request. `xml_depth` and
`xml_elements` bound the strict parser that consumes a downloaded resource.

Each in-flight compilation gets its own closure state, so concurrent compiles
can share one policy without sharing origins or budgets. A direct `read()` call
outside a compilation uses one fallback closure. There is **no persistent remote
cache**: every compilation retrieves what it needs again.

### DNS checking is not rebinding immunity

The policy calls `resolver(host, port)` before every request and rejects
non-global addresses by default. The transport then resolves the hostname again
to establish the connection. A DNS answer can therefore change between the check
and the connection. The preconnection check raises the bar; it does not provide
DNS-rebinding immunity. If that distinction matters, supply a custom
`RemoteTransport` that connects only to the address that was checked.

`RemoteTransport` is the retrieval seam: `fetch(url, *, limits)` performs one
HTTPS request and returns a `RemoteResponse` without following redirects. The
default is `HttpClientTransport`. Both `transport` and `resolver` are policy
constructor arguments; replacing them is how a caller records, audits, or
constrains retrieval.

## XPath and denial-of-service limits

`Document.xpath` evaluates an XPath expression in-process over a projection of
the bound tree. Identity constraints and `xs:assert`/`xs:assertion` evaluate
their supported XPath subsets during validation. These are not sandboxes:

- A caller-supplied XPath expression is code. A pathological expression or a
  large tree can consume CPU and memory, and the core library imposes no
  wall-clock timeout on it.
- XSD identity-constraint XPath is restricted to the specified subset. The
  `xs:assert`/`xs:assertion` subset excludes external-resource functions such as
  `fn:doc` and `fn:collection`; unsupported expressions are reported and not
  evaluated.
- The MCP server rejects `doc()`, `document()`, `collection()`,
  `uri-collection()`, `json-doc()`, `unparsed-text()`, and
  `unparsed-text-lines()` calls in `pyxsd_query_document`, and applies its own
  compile/query deadline, page, depth, element, and output budgets. That is a
  service boundary, not a guarantee for library callers.
- To contain untrusted queries or schemas, run them behind a process boundary
  with an external timeout, or validate/limit the inputs before querying.

## MCP servers

The packaged MCP server is local, offline, and does not use
`RemoteResourcePolicy`. Every schema and instance document from the input
capture is parsed with `defusedxml` and `forbid_dtd=True`,
`forbid_entities=True`, and `forbid_external=True`, under the operator's XML
byte, depth, and element limits.
The `pyxsd[mcp]` extra no longer declares `defusedxml` separately because it is a
base dependency. See {doc}`mcp-deployment` for the worker and transport
boundaries.

## What these controls do not cover

- Python overlays, mixin extensions, and transforms are executable application
  code. They run with the caller's privileges and are outside the XML loading
  contract.
- A custom `ResourcePolicy` or `RemoteTransport` is trusted code. Its own
  authorization bugs are not fixed by pyxsd.
- Parsing and validation can still be expensive for a large document within its
  budgets. Choose limits for the host, not only for the format.
- Reporting is lax by design: recoverable errors produce a tree and a report
  rather than aborting. A report is not a security decision, and a tree built
  from invalid input is not trusted data.
