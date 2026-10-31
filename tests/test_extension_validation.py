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

import pytest

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

# The bare simple-typed flagship case: element ``endpoint`` typed by the
# extended simple type DIRECTLY (no simpleContent wrapper), both as a
# root element and as a child of a complex type. ``loose`` is typed
# ``xs:anySimpleType``, whose builtin class carries no mixin.
BARE_SCHEMA = f"""<xs:schema xmlns:xs="{XSD}" xmlns:m="{NS}"
    targetNamespace="{NS}" elementFormDefault="qualified">
  <xs:simpleType name="ServiceUri">
    <xs:restriction base="xs:anyURI">
      <xs:pattern value="\\S*"/>
    </xs:restriction>
  </xs:simpleType>
  <xs:complexType name="Doc">
    <xs:sequence>
      <xs:element name="endpoint" type="m:ServiceUri"/>
    </xs:sequence>
  </xs:complexType>
  <xs:element name="endpoint" type="m:ServiceUri"/>
  <xs:element name="doc" type="m:Doc"/>
  <xs:element name="loose" type="xs:anySimpleType"/>
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


# The union finding-fix case: element ``thing`` declared directly with
# a union simple type. The union class is built through ``makeUnionClass``;
# the mixin selected for it must ride the union class's MRO so the hook
# fires on the union-validated instance value.
UNION_SCHEMA = f"""<xs:schema xmlns:xs="{XSD}" xmlns:m="{NS}"
    targetNamespace="{NS}" elementFormDefault="qualified">
  <xs:simpleType name="IntOrStr">
    <xs:union memberTypes="xs:int xs:string"/>
  </xs:simpleType>
  <xs:element name="thing" type="m:IntOrStr"/>
</xs:schema>"""


def _compiled(mixin, **compile_kwargs):
    ext = Extensions()
    ext.for_type("ServiceUri", mixin=mixin)
    return Schema.compile(StringIO(SCHEMA), extensions=ext, **compile_kwargs)


def _parse(mixin, xml, **compile_kwargs):
    return _compiled(mixin, **compile_kwargs).parse(StringIO(xml % NS))


def _parse_bare(mixin, xml, **compile_kwargs):
    ext = Extensions()
    ext.for_type("ServiceUri", mixin=mixin)
    schema = Schema.compile(StringIO(BARE_SCHEMA), extensions=ext, **compile_kwargs)
    return schema.parse(StringIO(xml % NS))


class TestBareSimpleTypeElement:
    """The flagship case: the extended simple type IS the element type.

    A bare simple-typed element's value is constructed through the
    primitive path (``primitive_root_instance`` for a root,
    ``primitiveValueFor`` through ``_addChildInstance`` for a child),
    which never reaches ``makeInstanceFromTag``; the hook fires at those
    construction sites instead, after base validation, once per value.
    """

    def test_bare_simple_typed_root_hook(self):
        mixin = _uri_mixin()
        doc = _parse_bare(mixin, '<m:endpoint xmlns:m="%s">relative/path</m:endpoint>')
        hook_issues = [i for i in doc.report.issues if i.code == "extension-validation"]
        assert len(hook_issues) == 1
        issue = hook_issues[0]
        assert issue.severity is IssueSeverity.ERROR
        assert issue.phase == "instance"
        assert issue.element == "endpoint"
        assert issue.message == "the service URI 'relative/path' has no host"
        # Base anyURI validation ran intact: no base ``value`` issue.
        assert not [i for i in doc.report.issues if i.code == "value"]
        assert doc.root is not None
        # The hook that fired is the mixin's, exactly once.
        assert mixin.calls == 1

    def test_bare_simple_typed_child_hook(self):
        mixin = _uri_mixin()
        doc = _parse_bare(
            mixin, '<m:doc xmlns:m="%s"><m:endpoint>relative/path</m:endpoint></m:doc>'
        )
        hook_issues = [i for i in doc.report.issues if i.code == "extension-validation"]
        assert len(hook_issues) == 1
        assert hook_issues[0].element == "endpoint"
        assert hook_issues[0].phase == "instance"
        assert mixin.calls == 1

    def test_bare_simple_typed_base_failure_skips_hook(self):
        """A bare simple-typed value violating the XSD type itself (the
        ``\\S*`` pattern) reports the base ``value`` error and binds no
        hook finding — under both the drop and the raw fallback."""
        mixin = _uri_mixin()
        for mode_kwargs in ({}, {"mode": ParseModes.LAX}):
            mixin.calls = 0
            doc = _parse_bare(mixin, '<m:endpoint xmlns:m="%s">bad uri</m:endpoint>', **mode_kwargs)
            assert len([i for i in doc.report.issues if i.code == "value"]) == 1
            assert [i for i in doc.report.issues if i.code.startswith("extension")] == []
            assert mixin.calls == 0

    def test_any_simple_type_element_attempts_no_hook(self):
        """An ``xs:anySimpleType``-typed element's builtin class carries
        no mixin: the hook lookup finds nothing, the document binds with
        no extension issue and no error."""
        mixin = _uri_mixin()
        doc = _parse_bare(mixin, '<m:loose xmlns:m="%s">whatever text</m:loose>')
        assert doc.root is not None
        assert doc.root._value_ == ["whatever text"]
        assert not [i for i in doc.report.issues if i.code.startswith("extension")]
        assert not [i for i in doc.report.issues if i.code == "value"]
        assert mixin.calls == 0


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
    def test_bare_string_return_is_one_finding(self):
        """A hook returning a bare ``str`` (not a list of findings) is
        recorded as ONE ``extension-validation`` error — a string is not
        iterated per character."""

        class StringReturnMixin:
            def __pyxsd_validate__(self):
                return "the service URI has no host"

        doc = _parse(StringReturnMixin, '<m:endpoint xmlns:m="%s">relative/path</m:endpoint>')
        hook_issues = [i for i in doc.report.issues if i.code == "extension-validation"]
        assert len(hook_issues) == 1
        assert hook_issues[0].message == "the service URI has no host"

    def test_bare_issue_return_is_one_finding(self):
        """A hook returning a bare ``ValidationIssue`` (not a list) is
        recorded once, with its severity and code kept."""

        class IssueReturnMixin:
            def __pyxsd_validate__(self):
                return ValidationIssue(IssueSeverity.WARNING, "insecure-scheme", "plain http")

        doc = _parse(IssueReturnMixin, '<m:endpoint xmlns:m="%s">http://example.com/x</m:endpoint>')
        kept = [i for i in doc.report.issues if i.code == "insecure-scheme"]
        assert len(kept) == 1
        assert kept[0].severity is IssueSeverity.WARNING

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


class TestReturnShapeNormalization:
    """A hook's return is normalized into findings before recording.

    ``None`` and empty iterables add no finding; a bare ``str`` or
    ``ValidationIssue`` counts as that one finding; any other
    non-iterable return is one ``extension-error`` naming its type —
    never silently swallowed as a falsy value.
    """

    @pytest.mark.parametrize(
        ("returned", "type_name"),
        [(0, "int"), (False, "bool"), (42, "int")],
        ids=["zero", "false", "truthy-int"],
    )
    def test_non_iterable_return_is_one_extension_error(self, returned, type_name):
        class NonIterableReturnMixin:
            def __pyxsd_validate__(self):
                return returned

        doc = _parse(NonIterableReturnMixin, '<m:endpoint xmlns:m="%s">https://host/x</m:endpoint>')
        errors = [i for i in doc.report.issues if i.code == "extension-error"]
        assert len(errors) == 1
        issue = errors[0]
        assert issue.severity is IssueSeverity.ERROR
        assert issue.phase == "instance"
        assert issue.element == "endpoint"
        assert f"unsupported finding of type {type_name}" in issue.message
        # The finding is unsupported, not an exception escaping the hook.
        assert "raised" not in issue.message
        assert "TypeError" not in issue.message
        # Binding continued past the bad return.
        assert doc.root is not None

    @pytest.mark.parametrize(
        "returned",
        [None, [], ()],
        ids=["none", "empty-list", "empty-tuple"],
    )
    def test_no_finding_return_adds_no_finding(self, returned):
        calls = []

        class NoFindingMixin:
            def __pyxsd_validate__(self):
                calls.append(str(self))
                return returned

        doc = _parse(NoFindingMixin, '<m:endpoint xmlns:m="%s">https://host/x</m:endpoint>')
        assert calls == ["https://host/x"]
        assert not doc.report.issues

    def test_generator_return_is_consumed_once(self):
        yielded = []

        class GeneratorMixin:
            def __pyxsd_validate__(self):
                def findings():
                    yielded.append("first")
                    yield "generator finding"
                    yielded.append("second")

                return findings()

        doc = _parse(GeneratorMixin, '<m:endpoint xmlns:m="%s">https://host/x</m:endpoint>')
        hook_issues = [i for i in doc.report.issues if i.code == "extension-validation"]
        assert [i.message for i in hook_issues] == ["generator finding"]
        # The generator was iterated exactly once: both stages ran in order.
        assert yielded == ["first", "second"]

    def test_generator_raising_during_consumption_is_one_extension_error(self):
        class ConsumingMixin:
            def __pyxsd_validate__(self):
                def findings():
                    yield "first finding"
                    raise RuntimeError("generator exploded")

                return findings()

        doc = _parse(ConsumingMixin, '<m:endpoint xmlns:m="%s">https://host/x</m:endpoint>')
        hook_issues = [i for i in doc.report.issues if i.code == "extension-validation"]
        assert [i.message for i in hook_issues] == ["first finding"]
        errors = [i for i in doc.report.issues if i.code == "extension-error"]
        assert len(errors) == 1
        assert errors[0].severity is IssueSeverity.ERROR
        assert errors[0].phase == "instance"
        assert errors[0].element == "endpoint"
        assert "hook raised RuntimeError: generator exploded" in errors[0].message
        assert doc.root is not None


class TestUnionTypeHook:
    """The hook fires on a union simple type's validated instance value.

    The union class wraps the first successfully validated member value
    in ``memberValue``; the mixin rides the union class's MRO, so the
    hook runs on the union wrapper (``str(self)`` delegates) after the
    union accepted the value.
    """

    def test_hook_fires_on_union_value(self):
        calls = []

        class IntOrStrMixin:
            def __pyxsd_validate__(self):
                calls.append(str(self))
                if not str(self).isdigit():
                    return f"union value {str(self)!r} is not all digits"
                return ()

        ext = Extensions()
        ext.for_type("IntOrStr", mixin=IntOrStrMixin)
        schema = Schema.compile(StringIO(UNION_SCHEMA), extensions=ext)
        doc = schema.parse(StringIO('<m:thing xmlns:m="%s">abc</m:thing>'))
        hook_issues = [i for i in doc.report.issues if i.code == "extension-validation"]
        assert len(hook_issues) == 1
        assert hook_issues[0].severity is IssueSeverity.ERROR
        assert hook_issues[0].phase == "instance"
        assert hook_issues[0].element == "thing"
        assert hook_issues[0].message == "union value 'abc' is not all digits"
        # The union itself accepted 'abc' through the string member: no
        # base ``value`` error, and the value bound.
        assert not [i for i in doc.report.issues if i.code == "value"]
        assert str(doc.root) == "abc"
        assert calls == ["abc"]

    def test_clean_union_value_runs_hook_with_no_finding(self):
        calls = []

        class IntOrStrMixin:
            def __pyxsd_validate__(self):
                calls.append(str(self))
                return ()

        ext = Extensions()
        ext.for_type("IntOrStr", mixin=IntOrStrMixin)
        schema = Schema.compile(StringIO(UNION_SCHEMA), extensions=ext)
        doc = schema.parse(StringIO('<m:thing xmlns:m="%s">42</m:thing>'))
        assert not doc.report.issues
        assert calls == ["42"]


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
