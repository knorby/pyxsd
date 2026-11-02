# Bad Data Doctor: MCP diagnosis

Use the real, unchanged CC0 [HL7 CCD 2 document](../documents/README.md) to investigate imperfect XML without silently repairing it.

```sh
uv run python examples/healthcare/download_sources.py
uv run --extra mcp python examples/healthcare/bad_data_doctor/doctor.py
```

The standard [MCP Python SDK 2.x Client](https://github.com/modelcontextprotocol/python-sdk/blob/v2.3.0/docs/client/index.md) launches a local stdio server and manages its lifecycle. No AI key, network service, or trusted-local execution is used. Source inputs are read-only. The automated command uses `--no-persist`; report artifacts last only until that subprocess exits. For interactive retained reports, launch your MCP client with:

```sh
uv run --extra mcp pyxsd-mcp --source-root "$PWD/examples/healthcare"
```

Check capabilities, including the artifact store location, before proceeding. Workspace release does not delete retained reports; use `pyxsd_delete_artifact` explicitly when finished.

## Expected evidence

At the pinned revisions, the CCD produces 10 errors plus a schema composition warning: `order` and `unexpected-element` errors include unfamiliar `sdtc:raceCode` content. Counts are reconnaissance evidence, not a compatibility promise. The command pages through all findings, inspects the first located one, and prints exact occurrence XML plus curated guidance. Use `--issue N` to inspect a particular zero-based finding (including schema findings), or `--document relative/path.xml` for another file within the healthcare root.

An invalid document is a successful MCP validation response with `valid=false`. `invalid_xml`, `schema_incomplete`, missing input, and response-budget faults are different failures. The client rejects suppressed or spilled issue responses rather than presenting an incomplete inventory. A valid response has no report artifact; the server omits its diagnostic counts, so the displayed zero counts mean no invalid report was returned, not proof that no warnings occurred.

Follow [the walkthrough](walkthrough.md) to classify findings, inspect declarations, and validate a new candidate after an external edit. Neither the server nor this client has a general repair tool. This checks base CDA XSD, not C-CDA template/Schematron rules or clinical correctness.
