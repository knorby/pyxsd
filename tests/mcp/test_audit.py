"""Audit records are structured and free of source payloads."""

import json
import logging

from pyxsd.mcp.audit import AuditLogger


def test_audit_events_include_metrics_and_redaction(caplog):
    events = []
    audit = AuditLogger(metrics=events.append)
    with caplog.at_level(logging.INFO, logger="pyxsd.mcp.audit"):
        audit.emit(
            operation="validate_document",
            status="ok",
            duration=0.25,
            workspace_id="workspace",
            generation=2,
            cache="hit",
            input_digest="a" * 64,
        )
        audit.emit(operation="query_document", status="error", duration=1.0)
    first = json.loads(caplog.records[0].message)
    second = json.loads(caplog.records[1].message)
    assert first["operation"] == "validate_document"
    assert first["workspace"] == "workspace" and first["generation"] == 2
    assert first["cache"] == "hit"
    assert first["input_digest"] == "a" * 64
    assert first["duration_ms"] == 250
    assert "workspace" not in second and "generation" not in second
    assert len(events) == 2 and events[0] == first
    rendered = caplog.text + json.dumps(events)
    for forbidden in (
        "alice@example.test",
        "<root>",
        "secret.xml",
        "access_token",
    ):
        assert forbidden not in rendered


def test_metrics_backend_is_optional_and_cannot_change_verdicts():
    def broken(_event):
        raise RuntimeError("backend credentials must not leak")

    audit = AuditLogger(metrics=broken)
    audit.emit(operation="validate_document", status="ok", duration=0.0)
