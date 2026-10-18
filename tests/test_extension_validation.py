"""Tests for the ``__pyxsd_validate__`` mixin validation hook.

A mixin may carry a convention method ``__pyxsd_validate__(self)`` that
adds validation findings without touching base XSD validation. Binding
invokes it after base validation on successfully constructed values
only: element instances at the end of ``SchemaBase.makeInstanceFromTag``
and converted attribute values in ``Attribute.__set__``. A ``str``
finding becomes an error with code ``extension-validation``; a returned
:class:`~pyxsd.validation.ValidationIssue` keeps its own severity and
code with ``phase`` forced to ``"instance"`` and ``element`` filled from
the declaring name when it carries none. A raising hook becomes one
``extension-error`` issue and binding continues.

The thread example throughout: simpleType ``ServiceUri`` restricting
``xs:anyURI`` (its lexical space still validated by the base type),
carrying a mixin with a plain ``fetch()`` method and the hook, used as
element ``endpoint`` text and attribute ``ref`` on element ``resource``.
"""

from io import StringIO

from pyxsd.binding import ParseModes
from pyxsd.extensions import Extensions
from pyxsd.schema import Schema
from pyxsd.validation import IssueSeverity, ValidationIssue

XSD = "http://www.w3.org/2001/XMLSchema"
NS = "http://example.com/u"

SCHEMA = f"""<xs:schema xmlns:xs="{XSD}" xmlns:m="{NS}"
    targetNamespace="{NS}" elementFormDefault="qualified">
  <xs:simpleType name="ServiceUri">
    <xs:restriction base="xs:anyURI">
      <xs:pattern value="\\S*"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="Endpoint">
    <xs:simpleContent>
      <xs:extension base="m:ServiceUri"/>
    </xs:simpleContent>
  </xs:complexType>
  <xs:complexType name="Resource">
    <xs:simpleContent>
      <xs:extension base="m:ServiceUri">
        <xs:attribute name="ref" type="m:ServiceUri"/>
      </xs:extension>
    </xs:simpleContent>
  </xs:complexType>
  <xs:element name="endpoint" type="m:Endpoint"/>
  <xs:element name="resource" type="m:Resource"/>
</xs:schema>"""


def _uri_mixin():
    """A fresh mixin: plain method plus the validation hook."""

    class UriMixin:
        calls = 0

        def fetch(self):
            return f"fetched:{self}"

        def __pyxsd_validate__(self):
            UriMixin.calls += 1
            parsed = self.to_parsed_uri()
            findings = []
            if not parsed.scheme and not parsed.netloc:
                findings.append(f"the service URI {str(self)!r} has no host")
            if parsed.scheme == "http":
                findings.append(
                    ValidationIssue(
                        IssueSeverity.WARNING,
                        "insecure-scheme",
                        f"the service URI {str(self)!r} uses plain http",
                    )
                )
            return findings

    return UriMixin


def _compiled(mixin, **compile_kwargs):
    ext = Extensions()
    ext.for_type("ServiceUri", mixin=mixin)
    return Schema.compile(StringIO(SCHEMA), extensions=ext, **compile_kwargs)


def _parse(mixin, xml, **compile_kwargs):
    return _compiled(mixin, **compile_kwargs).parse(StringIO(xml % NS))


class TestElementValueHook:
    def test_element_value_hook(self):
        """A relative URI passes base anyURI validation; the mixin adds
        one ``extension-validation`` error on the instance report."""
        mixin = _uri_mixin()
        doc = _parse(mixin, '<m:endpoint xmlns:m="%s">relative/path</m:endpoint>')
        hook_issues = [i for i in doc.report.issues if i.code == "extension-validation"]
        assert len(hook_issues) == 1
        issue = hook_issues[0]
        assert issue.severity is IssueSeverity.ERROR
        assert issue.phase == "instance"
        # The element filled from the declaring element's name.
        assert issue.element == "endpoint"
        assert issue.message == "the service URI 'relative/path' has no host"
        # Base anyURI validation ran intact: the value is lexically
        # valid, so no base ``value`` issue was recorded.
        assert not [i for i in doc.report.issues if i.code == "value"]
        assert doc.root is not None

    def test_hook_runs_once_per_instance(self):
        """One element construction, one hook call: the simple-content
        value is built inside ``makeInstanceFromTag`` and the throwaway
        content-type value is never hooked."""
        mixin = _uri_mixin()
        _parse(mixin, '<m:endpoint xmlns:m="%s">relative/path</m:endpoint>')
        assert mixin.calls == 1


class TestAttributeValueHook:
    def test_attribute_value_hook(self):
        """The same mixin finding reaches the report through the
        attribute path: ``ref="relative/path"`` converts successfully
        and the hook runs on the converted value."""
        mixin = _uri_mixin()
        doc = _parse(
            mixin,
            '<m:resource xmlns:m="%s" ref="relative/path">https://host/x</m:resource>',
        )
        hook_issues = [i for i in doc.report.issues if i.code == "extension-validation"]
        assert len(hook_issues) == 1
        issue = hook_issues[0]
        assert issue.severity is IssueSeverity.ERROR
        assert issue.phase == "instance"
        # Filled from the declaring attribute's name.
        assert issue.element == "ref"
        assert issue.message == "the service URI 'relative/path' has no host"
        # The element text is a valid https URI: no element hook finding.
        assert not [i for i in doc.report.issues if i.code == "extension-error"]
        assert doc.root is not None


class TestBaseValidationStillRuns:
    def test_base_failure_reports_value_and_skips_hook(self):
        """A value violating the XSD type itself (the ``\\S*`` pattern)
        produces the base ``value`` error, and the hook does not fire on
        the unvalidated fallback instance."""
        mixin = _uri_mixin()
        doc = _parse(mixin, '<m:endpoint xmlns:m="%s">bad uri</m:endpoint>')
        base = [i for i in doc.report.issues if i.code == "value"]
        assert len(base) == 1
        assert base[0].severity is IssueSeverity.ERROR
        assert [i for i in doc.report.issues if i.code.startswith("extension")] == []
        assert mixin.calls == 0

    def test_raw_fallback_skips_hook(self):
        """Under the ``raw`` invalid-value policy the invalid value is
        bound as an unvalidated string; the hook still does not fire."""
        mixin = _uri_mixin()
        doc = _parse(
            mixin,
            '<m:endpoint xmlns:m="%s">bad uri</m:endpoint>',
            mode=ParseModes.LAX,
        )
        assert len([i for i in doc.report.issues if i.code == "value"]) == 1
        assert [i for i in doc.report.issues if i.code.startswith("extension")] == []
        assert mixin.calls == 0


class TestWarningSeverityPassthrough:
    def test_returned_issue_keeps_severity_and_code(self):
        """A ``ValidationIssue`` finding keeps its own severity and
        code; ``phase`` is forced to ``instance`` and a missing
        ``element`` is filled from the declaring name, while an issue
        that names its own element keeps it."""
        mixin = _uri_mixin()
        doc = _parse(mixin, '<m:endpoint xmlns:m="%s">http://example.com/x</m:endpoint>')
        kept = [i for i in doc.report.issues if i.code == "insecure-scheme"]
        assert len(kept) == 1
        assert kept[0].severity is IssueSeverity.WARNING
        assert kept[0].phase == "instance"
        assert kept[0].element == "endpoint"
        assert kept[0].message == "the service URI 'http://example.com/x' uses plain http"
        # No error string was returned for a URI with a host.
        assert not [i for i in doc.report.issues if i.code == "extension-validation"]

    def test_returned_issue_keeps_its_own_element(self):
        class ElementNamedMixin:
            def __pyxsd_validate__(self):
                return [
                    ValidationIssue(
                        IssueSeverity.WARNING,
                        "insecure-scheme",
                        "named issue",
                        element="custom-element",
                    )
                ]

        doc = _parse(ElementNamedMixin, '<m:endpoint xmlns:m="%s">https://host/x</m:endpoint>')
        kept = [i for i in doc.report.issues if i.code == "insecure-scheme"]
        assert len(kept) == 1
        assert kept[0].element == "custom-element"


class TestHookExceptionIsolated:
    def test_raising_hook_becomes_one_extension_error(self):
        """A hook that raises is recorded as one ``extension-error``
        issue and the document still binds."""

        class BrokenMixin:
            def __pyxsd_validate__(self):
                raise RuntimeError("hook exploded")

        doc = _parse(BrokenMixin, '<m:endpoint xmlns:m="%s">https://host/x</m:endpoint>')
        errors = [i for i in doc.report.issues if i.code == "extension-error"]
        assert len(errors) == 1
        assert errors[0].severity is IssueSeverity.ERROR
        assert errors[0].phase == "instance"
        assert errors[0].element == "endpoint"
        assert "hook exploded" in errors[0].message
        # Binding continued: the root is present and its content bound.
        assert doc.root is not None
        assert str(doc.root) == "https://host/x"


class TestPlainMethodAlongsideHook:
    def test_fetch_callable_on_bound_values(self):
        """The plain ``fetch()`` method rides on the same mixin and is
        callable on both bound surfaces."""
        mixin = _uri_mixin()
        doc = _parse(
            mixin,
            '<m:resource xmlns:m="%s" ref="https://host/a">https://host/b</m:resource>',
        )
        assert doc.root.fetch() == "fetched:https://host/b"
        assert doc.root.ref.fetch() == "fetched:https://host/a"
        # Both URIs carry a host and use https: no hook findings.
        assert not [i for i in doc.report.issues if i.code.startswith("extension")]
        assert not doc.report.issues
