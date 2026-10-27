"""Audit records are structured, pseudonymous and free of source payloads."""

import json
import logging

from pyxsd.mcp.audit import AuditLogger


def test_audit_pseudonyms_metrics_and_redaction(caplog):
    events = []
    audit = AuditLogger(metrics=events.append)
    with caplog.at_level(logging.INFO, logger="pyxsd.mcp.audit"):
        audit.emit(
            operation="validate_document",
            owner_id="alice@example.test",
            status="ok",
            duration=0.25,
            workspace_id="workspace",
            revision="revision",
            scope="pyxsd:read",
            cache="hit",
            input_digest="a" * 64,
        )
        audit.emit(
            operation="query_document", owner_id="alice@example.test", status="error", duration=1.0
        )
        audit.emit(
            operation="query_document", owner_id="bob@example.test", status="error", duration=2.0
        )
    first = json.loads(caplog.records[0].message)
    second = json.loads(caplog.records[1].message)
    third = json.loads(caplog.records[2].message)
    assert first["operation"] == "validate_document"
    assert first["owner"] == second["owner"] != third["owner"]
    assert first["cache"] == "hit" and first["scope"] == "pyxsd:read"
    assert first["input_digest"] == "a" * 64
    assert first["duration_ms"] == 250
    assert len(events) == 3 and events[0] == first
    rendered = caplog.text + json.dumps(events)
    for forbidden in (
        "alice@example.test",
        "bob@example.test",
        "<root>",
        "secret.xml",
        "access_token",
    ):
        assert forbidden not in rendered


def test_metrics_backend_is_optional_and_cannot_change_verdicts():
    def broken(_event):
        raise RuntimeError("backend credentials must not leak")

    audit = AuditLogger(metrics=broken)
    audit.emit(operation="validate_document", owner_id="owner", status="ok", duration=0.0)
