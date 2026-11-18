"""Standalone RELAX NG scalar models: values, tags, snapshots, and context."""

from __future__ import annotations

import io
import json
import math
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

pydantic = pytest.importorskip("pydantic")
from jsonschema import Draft202012Validator  # noqa: E402

from integration_helpers import RECORD_SCHEMA, compile_schema  # noqa: E402
from pydantic import RootModel, TypeAdapter, ValidationError  # noqa: E402
from pyxsd.integrations import IntegrationError  # noqa: E402
from pyxsd.integrations.pydantic import ModelSet, models  # noqa: E402
from pyxsd.namespaces import XML_NS  # noqa: E402
from pyxsd.relaxng.contracts import ScalarContract, candidate_id  # noqa: E402
from pyxsd.relaxng.datatypes import (  # noqa: E402
    Datatype,
    DatatypeContext,
    DatatypeFactory,
    DatatypeLibraryRegistration,
    UnsupportedDatatypeOperation,
)
from pyxsd.relaxng.json_values import scalar_tag  # noqa: E402
from pyxsd.relaxng.patterns import Text  # noqa: E402
from pyxsd.relaxng.pydantic_adapter import RelaxNGModelSet  # noqa: E402
from pyxsd.relaxng.pydantic_values import (  # noqa: E402
    _UNDECODABLE,
    STANDALONE_REVISION,
    ScalarCodec,
    _decode_list,
    _decode_token,
    context_from_validation,
    relaxng_scalar_annotation,
)
from pyxsd.relaxng.schema import RelaxNGSchema  # noqa: E402
from pyxsd.relaxng.values import AlternativeValue, Candidate  # noqa: E402

NS = "http://relaxng.org/ns/structure/1.0"
XSD = "http://www.w3.org/2001/XMLSchema-datatypes"


def compile_element(
    body: str,
    *,
    name: str = "doc",
    datatype_libraries: dict[str, DatatypeLibraryRegistration] | None = None,
) -> RelaxNGSchema:
    schema = RelaxNGSchema.compile(
        io.StringIO(
            f'<element xmlns="{NS}" name="{name}" datatypeLibrary="{XSD}">{body}</element>'
        ),
        datatype_libraries=datatype_libraries,
    )
    schema.require_valid()
    return schema


def model_for(
    body: str,
    *,
    datatype_libraries: dict[str, DatatypeLibraryRegistration] | None = None,
) -> tuple[RelaxNGSchema, type[RootModel]]:
    schema = compile_element(body, datatype_libraries=datatype_libraries)
    return schema, RelaxNGModelSet(schema).model_for(element="doc")


def root_contracts(schema: RelaxNGSchema) -> tuple[ScalarContract, ...]:
    return schema.projection.routes[schema.projection.roots[0]].content.scalar_contracts


def message(error: ValidationError) -> str:
    return error.errors()[0]["msg"]


def assert_export_matches(model_class: type[RootModel], instance: RootModel) -> None:
    schema = model_class.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(json.loads(instance.model_dump_json()))


class NonDecodingDatatype(Datatype):
    """A named custom datatype that validates but declares no decoder."""

    library_uri = "urn:test"
    type_name = "opaque"
    parameters = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        del context
        return lexical == "ok"

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        del left_context, right_context
        return left == right


class NonDecodingFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return NonDecodingDatatype()


class UnsupportedDecodingDatatype(NonDecodingDatatype):
    """A declared decoder that reports the operation is unsupported."""

    type_name = "unsupported"
    supports_decode = True

    def decode(self, lexical: str, context: DatatypeContext) -> object:
        del lexical, context
        raise UnsupportedDatatypeOperation("no decoder")


class UnsupportedDecodingFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return UnsupportedDecodingDatatype()


class ResultDatatype(Datatype):
    """A custom decodable type to exercise the bounded opaque schema."""

    library_uri = "urn:result"
    type_name = "result"
    parameters = ()
    supports_decode = True

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        del context
        return True

    def equal(
        self,
        left: str,
        left_context: DatatypeContext,
        right: str,
        right_context: DatatypeContext,
    ) -> bool:
        del left_context, right_context
        return left == right

    def decode(self, lexical: str, context: DatatypeContext) -> str:
        del context
        return f"result:{lexical}"


class ResultFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return ResultDatatype()


def registration(factory: DatatypeFactory, fingerprint: str) -> DatatypeLibraryRegistration:
    return DatatypeLibraryRegistration(factory, fingerprint)


def test_unique_scalars_use_ordinary_python_values():
    _, text = model_for("<text/>")
    assert text.model_validate("value").root == "value"
    with pytest.raises(ValidationError):
        text.model_validate(42)

    _, integer = model_for('<data type="integer"/>')
    assert integer.model_validate(7).root == 7
    assert integer.model_validate("7").root == 7
    assert integer.model_validate_json("7").root == 7
    with pytest.raises(ValidationError):
        integer.model_validate(True)
    with pytest.raises(ValidationError):
        integer.model_validate(1.5)
    with pytest.raises(ValidationError):
        integer.model_validate("not-an-integer")

    _, boolean = model_for('<data type="boolean"/>')
    assert boolean.model_validate(True).root is True
    assert boolean.model_validate("true").root is True

    _, decimal = model_for('<data type="decimal"/>')
    assert decimal.model_validate(Decimal("1.20")).root == Decimal("1.20")
    assert decimal.model_validate("1.20").root == Decimal("1.20")
    assert decimal.model_validate_json(decimal.model_validate("1.20").model_dump_json()).root == (
        Decimal("1.20")
    )
    with pytest.raises(ValidationError):
        decimal.model_validate(1.2)

    _, floating = model_for('<data type="float"/>')
    assert floating.model_validate(1.5).root == 1.5
    assert math.isnan(floating.model_validate("NaN").root)

    _, qname = model_for('<data type="QName"/>')
    context = {"relaxng": {"namespaces": {"p": "urn:x"}, "base_uri": "urn:base"}}
    resolved = qname.model_validate("p:x", context=context).root
    assert resolved.to_expanded_name() == "{urn:x}x"
    assert qname.model_validate("Q{urn:x}x", context=context).root == resolved
    with pytest.raises(ValidationError):
        qname("p:x")
    with pytest.raises(ValidationError) as error:
        qname.model_validate("p:x")
    assert "base_uri=None" in message(error.value)
    with pytest.raises(ValidationError) as error:
        qname.model_validate("Q{urn:missing}x")
    assert "requires an in-scope prefix" in message(error.value)
    assert qname.model_validate("Q{}x").root.to_expanded_name() == "x"
    assert (
        qname.model_validate(
            "Q{urn:x}x", context={"relaxng": {"namespaces": {"": "urn:x"}}}
        ).root.to_expanded_name()
        == "{urn:x}x"
    )
    assert (
        qname.model_validate(
            "Q{urn:x}x", context={"relaxng": {"namespaces": {"p": "urn:y", "q": "urn:x"}}}
        ).root.to_expanded_name()
        == "{urn:x}x"
    )
    with pytest.raises(ValidationError):
        qname.model_validate("Q{bad local}")
    with pytest.raises(ValidationError):
        qname.model_validate("Q{}x", context={"relaxng": {"namespaces": {"": "urn:d"}}})

    _, restricted = model_for('<data type="integer"><param name="minInclusive">2</param></data>')
    assert restricted.model_validate(2).root == 2
    with pytest.raises(ValidationError):
        restricted.model_validate(1)

    _, valued = model_for('<value type="integer">5</value>')
    assert valued.model_validate("5").root == 5
    with pytest.raises(ValidationError):
        valued.model_validate(6)

    _, excepted = model_for(
        '<data type="integer"><except><value type="integer">5</value></except></data>'
    )
    assert excepted.model_validate(6).root == 6
    with pytest.raises(ValidationError):
        excepted.model_validate(5)


def test_unique_lists_use_declared_tuple_and_list_shapes():
    _, fixed = model_for(
        '<list><data type="integer"/><data type="token" datatypeLibrary=""/></list>'
    )
    assert fixed.model_validate((1, "a")).root == (1, "a")
    assert fixed.model_validate("1 a").root == (1, "a")
    assert fixed.model_validate_json('[1, "a"]').root == (1, "a")
    with pytest.raises(ValidationError):
        fixed.model_validate([1, "a"])

    _, homogeneous = model_for('<list><zeroOrMore><data type="integer"/></zeroOrMore></list>')
    assert homogeneous.model_validate([1, 2]).root == [1, 2]
    assert homogeneous.model_validate_json("[]").root == []
    assert homogeneous.model_validate("").root == []
    with pytest.raises(ValidationError):
        homogeneous.model_validate((1, 2))


def test_unique_nondecodable_contracts_remain_wrappers():
    opaque = {
        "urn:test": registration(NonDecodingFactory(), "test/1"),
    }
    _, custom = model_for(
        '<data type="opaque" datatypeLibrary="urn:test"/>',
        datatype_libraries=opaque,
    )
    value = custom.model_validate("ok").root
    assert isinstance(value, AlternativeValue)
    assert len(value.candidates) == 1
    assert not value.candidates[0].decodable
    assert value.revision == STANDALONE_REVISION
    with pytest.raises(ValidationError):
        custom.model_validate("no")

    _, unsupported = model_for(
        '<data type="unsupported" datatypeLibrary="urn:test"/>',
        datatype_libraries={
            "urn:test": registration(UnsupportedDecodingFactory(), "test/2"),
        },
    )
    assert not unsupported.model_validate("ok").root.candidates[0].decodable

    _, interpretation = model_for(
        '<list><choice><data type="integer"/><data type="string"/></choice></list>'
    )
    value = interpretation.model_validate("12").root
    assert isinstance(value, AlternativeValue)
    assert value.lexical == "12"
    assert value.candidates[0].decodable is False
    with pytest.raises(ValidationError):
        interpretation.model_validate([12])

    _, opaque_list = model_for(
        '<list><data type="opaque" datatypeLibrary="urn:test"/></list>',
        datatype_libraries={"urn:test": registration(NonDecodingFactory(), "test/1")},
    )
    assert not opaque_list.model_validate("ok").root.candidates[0].decodable

    _, unsupported_list = model_for(
        '<list><data type="unsupported" datatypeLibrary="urn:test"/></list>',
        datatype_libraries={
            "urn:test": registration(UnsupportedDecodingFactory(), "test/2"),
        },
    )
    assert not unsupported_list.model_validate("ok").root.candidates[0].decodable


def test_ambiguous_scalars_reject_bare_non_strings_and_booleans():
    _, ambiguous = model_for('<choice><data type="integer"/><data type="string"/></choice>')
    value = ambiguous.model_validate("12").root
    assert isinstance(value, AlternativeValue)
    assert len(value.candidates) == 2
    integer = next(
        candidate for candidate in value.candidates if candidate.contract.type_name == "integer"
    )
    string = next(
        candidate for candidate in value.candidates if candidate.contract.type_name == "string"
    )
    assert value.as_candidate(integer.candidate_id) == 12
    assert value.as_candidate(string.candidate_id) == "12"
    for bare in (12, True, 1.5, None):
        with pytest.raises(ValidationError):
            ambiguous.model_validate(bare)
    only_string = ambiguous.model_validate("text").root
    assert isinstance(only_string, AlternativeValue)
    assert [candidate.contract.type_name for candidate in only_string.candidates] == ["string"]

    _, mixed = model_for('<choice><data type="integer"/><data type="boolean"/></choice>')
    one = mixed.model_validate("1").root
    assert [candidate.contract.type_name for candidate in one.candidates] == [
        "integer",
        "boolean",
    ]
    true = mixed.model_validate("true").root
    assert [candidate.contract.type_name for candidate in true.candidates] == ["boolean"]
    with pytest.raises(ValidationError) as error:
        mixed.model_validate("neither")
    assert "not accepted by any" in message(error.value)

    _, text_choice = model_for('<choice><text/><data type="integer"/></choice>')
    text_value = text_choice.model_validate("word").root
    assert isinstance(text_value, AlternativeValue)
    assert len(text_value.candidates) == 1
    assert not text_value.candidates[0].decodable
    assert str(text_value) == "word"


def test_tags_are_rechecked_against_the_complete_candidate_set():
    _, ambiguous = model_for('<choice><data type="integer"/><data type="string"/></choice>')
    value = ambiguous.model_validate("12").root
    tag = scalar_tag(value)
    accepted = ambiguous.model_validate(tag).root
    assert isinstance(accepted, AlternativeValue)
    assert [candidate.candidate_id for candidate in accepted.candidates] == [
        candidate.candidate_id for candidate in value.candidates
    ]
    assert accepted.revision == STANDALONE_REVISION
    assert ambiguous.model_validate_json(json.dumps(tag)).root.lexical == "12"

    subset = deepcopy(tag)
    subset["#value"]["candidates"] = subset["#value"]["candidates"][:1]
    with pytest.raises(ValidationError):
        ambiguous.model_validate(subset)

    extra = deepcopy(tag)
    extra["#value"]["candidates"].append("not-a-candidate")
    with pytest.raises(ValidationError):
        ambiguous.model_validate(extra)

    other_grammar = deepcopy(tag)
    other_grammar["#value"]["grammar"] = "other"
    with pytest.raises(ValidationError):
        ambiguous.model_validate(other_grammar)

    with_extra_member = deepcopy(tag)
    with_extra_member["extra"] = True
    with pytest.raises(ValidationError):
        ambiguous.model_validate(with_extra_member)

    bad_version = deepcopy(tag)
    bad_version["#value"]["version"] = 2
    with pytest.raises(ValidationError):
        ambiguous.model_validate(bad_version)

    duplicate = deepcopy(tag)
    duplicate["#value"]["candidates"].insert(1, duplicate["#value"]["candidates"][0])
    with pytest.raises(ValidationError):
        ambiguous.model_validate(duplicate)


def test_snapshots_are_rechecked_and_marked_standalone():
    _, ambiguous = model_for('<choice><data type="integer"/><data type="string"/></choice>')
    value = ambiguous.model_validate("12").root
    accepted = ambiguous.model_validate(value).root
    assert accepted == value
    assert accepted.revision == STANDALONE_REVISION

    later = replace(value, revision=12345)
    assert ambiguous.model_validate(later).root.revision == STANDALONE_REVISION

    stale = replace(value, grammar_fingerprint="another-grammar")
    with pytest.raises(ValidationError):
        ambiguous.model_validate(stale)

    subset = replace(value, candidates=value.candidates[:1])
    with pytest.raises(ValidationError):
        ambiguous.model_validate(subset)

    other_schema, _ = model_for('<data type="boolean"/>')
    other_contract = root_contracts(other_schema)[0]
    forged = replace(
        value,
        candidates=(
            replace(value.candidates[0], contract=other_contract),
            value.candidates[1],
        ),
    )
    with pytest.raises(ValidationError) as error:
        ambiguous.model_validate(forged)
    assert "recomputed contract" in message(error.value)


def test_scoped_context_is_copied_independent_and_checked_against_tags():
    context = {
        "relaxng": {
            "namespaces": {"p": "urn:x"},
            "base_uri": "urn:base",
        },
        "other": {"ignored": True},
    }
    original = deepcopy(context)
    schema, qname = model_for('<data type="QName"/>')
    assert context_from_validation(SimpleNamespace(context=context)) == DatatypeContext(
        "urn:base", {"p": "urn:x"}
    )
    assert context == original
    value = qname.model_validate("p:x", context=context).root
    assert value.to_expanded_name() == "{urn:x}x"

    contract = root_contracts(schema)[0]
    snapshot = AlternativeValue(
        "p:x",
        (
            Candidate(
                candidate_id(schema._grammar_fingerprint, contract),
                contract,
                value,
                True,
            ),
        ),
        DatatypeContext("urn:base", {"p": "urn:x"}),
        schema._grammar_fingerprint,
        STANDALONE_REVISION,
    )
    assert qname.model_validate(snapshot).root.to_expanded_name() == "{urn:x}x"
    tag = scalar_tag(snapshot)
    assert qname.model_validate(tag).root.to_expanded_name() == "{urn:x}x"

    conflicting = {"relaxng": {"namespaces": {"p": "urn:y"}}}
    with pytest.raises(ValidationError) as error:
        qname.model_validate(snapshot, context=conflicting)
    assert "urn:y" in message(error.value)
    with pytest.raises(ValidationError):
        qname.model_validate(tag, context=conflicting)

    other_scope = {"relaxng": {"namespaces": {"p": "urn:y"}}}
    assert qname.model_validate("p:x", context=other_scope).root.to_expanded_name() == "{urn:y}x"
    assert qname.model_validate("p:x", context=context).root.to_expanded_name() == "{urn:x}x"


def test_context_validation_rejects_malformed_scopes():
    def info(context):
        return SimpleNamespace(context=context)

    assert context_from_validation(None) == DatatypeContext(None, {})
    assert context_from_validation(info({})) == DatatypeContext(None, {})
    for context in (
        "not-a-mapping",
        {"relaxng": "not-a-mapping"},
        {"relaxng": {"namespaces": {}, "extra": True}},
        {"relaxng": {"base_uri": 1}},
        {"relaxng": {"namespaces": "not-a-mapping"}},
        {"relaxng": {"namespaces": {"p": 1}}},
        {"relaxng": {"namespaces": {"xml": XML_NS}}},
    ):
        with pytest.raises(ValueError):
            context_from_validation(info(context))


def test_list_ambiguity_never_collapses_to_a_string():
    _, ambiguous = model_for(
        '<choice><list><zeroOrMore><data type="integer"/></zeroOrMore></list>'
        '<data type="integer"/></choice>'
    )
    value = ambiguous.model_validate("12").root
    assert isinstance(value, AlternativeValue)
    assert value.lexical == "12"
    by_kind = {candidate.contract.kind: candidate for candidate in value.candidates}
    assert by_kind["list"].decoded == [12]
    assert by_kind["atomic"].decoded == 12
    assert ambiguous.model_validate("1 2").root.candidates[0].decoded == [1, 2]

    fixed_schema, _ = model_for(
        '<list><data type="integer"/><data type="token" datatypeLibrary=""/></list>'
    )
    pattern = fixed_schema.projection.encoding_patterns[
        root_contracts(fixed_schema)[0].pattern_identity
    ]
    assert _decode_list(pattern, "1", DatatypeContext(None, {})) is _UNDECODABLE
    assert _decode_token(Text(), "x", DatatypeContext(None, {})) is _UNDECODABLE

    interpretation_schema, _ = model_for(
        '<list><choice><data type="integer"/><data type="string"/></choice></list>'
    )
    interpretation_pattern = interpretation_schema.projection.encoding_patterns[
        root_contracts(interpretation_schema)[0].pattern_identity
    ]
    assert _decode_list(interpretation_pattern, "12", DatatypeContext(None, {})) is _UNDECODABLE


def test_json_dump_and_schema_preserve_wrapper_and_input_distinctions():
    _, ambiguous = model_for('<choice><data type="integer"/><data type="string"/></choice>')
    model = ambiguous.model_validate("12")
    assert isinstance(model.root, AlternativeValue)
    dumped = model.model_dump(mode="json")
    assert dumped["#value"]["version"] == 1
    assert model.model_dump_json() == json.dumps(dumped, separators=(",", ":"))
    assert_export_matches(ambiguous, model)

    schema = ambiguous.model_json_schema()
    branches = schema["anyOf"]
    assert {"type": "string"} in branches
    tag_schema = next(branch for branch in branches if branch.get("type") == "object")
    body = tag_schema["properties"]["#value"]
    assert body["additionalProperties"] is False
    assert set(body["required"]) == {"version", "grammar", "lexical", "context", "candidates"}
    assert body["properties"]["version"] == {"type": "integer", "const": 1}
    assert body["properties"]["candidates"]["minItems"] == 1
    assert body["properties"]["candidates"]["uniqueItems"] is True
    assert schema["x-pyxsd-cardinality"] == {"minimum": 1, "maximum": 1}

    _, integer = model_for('<data type="integer"/>')
    integer_schema = integer.model_json_schema()
    assert {"type": "integer"} in integer_schema["anyOf"]
    assert {} not in integer_schema["anyOf"]
    assert_export_matches(integer, integer.model_validate(7))

    _, fixed = model_for(
        '<list><data type="integer"/><data type="token" datatypeLibrary=""/></list>'
    )
    assert_export_matches(fixed, fixed.model_validate((1, "a")))
    list_schema = next(
        branch for branch in fixed.model_json_schema()["anyOf"] if branch.get("type") == "array"
    )
    assert list_schema["minItems"] == list_schema["maxItems"] == 2
    assert list_schema["prefixItems"] == [
        {"type": "integer"},
        {"type": "string", "x-pyxsd-type": "token"},
    ]
    assert list_schema["x-pyxsd-list-shape"] == "tuple"

    _, optional = model_for(
        '<optional><data type="integer"/></optional><attribute name="lang"><text/></attribute>'
    )
    optional_schema = optional.model_json_schema()
    assert "$" not in optional_schema.get("required", [])
    assert {"type": "null"} in optional_schema["properties"]["$"]["anyOf"]


def test_custom_decodable_scalar_schema_is_bounded_and_has_no_dangling_ref():
    _, custom = model_for(
        '<data type="result" datatypeLibrary="urn:result"/>',
        datatype_libraries={"urn:result": registration(ResultFactory(), "result/1")},
    )
    schema = custom.model_json_schema()
    assert {"type": "string"} in schema["anyOf"]
    assert {"type": "number"} in schema["anyOf"]
    assert {"type": "boolean"} in schema["anyOf"]
    assert not any("$ref" in branch for branch in schema["anyOf"])
    assert custom.model_validate("value").root == "result:value"
    assert custom.model_validate("value").model_dump_json() == '"result:value"'


def test_direct_construction_and_json_validation_share_contracts():
    _, integer = model_for('<data type="integer"/>')
    assert integer(7).root == 7
    assert integer.model_validate_json('"7"').root == 7

    _, record = model_for(
        '<attribute name="count"><data type="integer"/></attribute>'
        '<optional><data type="boolean"/></optional>'
    )
    value = record.model_validate({"@count": 2, "$": True})
    assert value.attr_count == 2
    assert value.xml_value is True
    assert record.model_validate_json('{"@count": "2", "$": true}').attr_count == 2
    assert record.model_validate_json('{"@count": 2}').xml_value is None
    assert record.model_validate({"@count": 2, "$": None}).xml_value is None


def test_empty_content_attribute_requires_the_empty_string():
    schema = compile_element('<attribute name="a"><empty/></attribute><empty/>')
    model = RelaxNGModelSet(schema).model_for(element="doc")
    assert model.model_validate({"@a": ""}).attr_a == ""
    with pytest.raises(ValidationError):
        model.model_validate({"@a": "x"})
    with pytest.raises(ValidationError):
        model.model_validate({"@a": None})


def test_codec_less_text_annotations_and_datatype_failures():
    schema = compile_element("<text/>")
    contract = root_contracts(schema)[0]
    adapter = TypeAdapter(relaxng_scalar_annotation((contract,)))
    assert adapter.validate_python("value") == "value"
    with pytest.raises(ValidationError):
        adapter.validate_python(1)
    with pytest.raises(ValidationError):
        adapter.validate_python(None)
    assert adapter.json_schema() == {"type": "string"}

    optional = TypeAdapter(relaxng_scalar_annotation((contract,), may_be_empty=True))
    assert optional.validate_python(None) is None
    assert {"type": "null"} in optional.json_schema()["anyOf"]

    restricted = TypeAdapter(relaxng_scalar_annotation((contract,)))
    assert restricted.validate_json('"value"') == "value"

    integer_schema = compile_element('<data type="integer"/>')
    integer_contract = root_contracts(integer_schema)[0]
    with pytest.raises(IntegrationError):
        relaxng_scalar_annotation((integer_contract,))
    with pytest.raises(TypeError):
        relaxng_scalar_annotation((contract,), codec="not-a-codec")

    empty = TypeAdapter(relaxng_scalar_annotation(()))
    assert empty.validate_python("") == ""
    with pytest.raises(ValidationError):
        empty.validate_python("x")
    empty_optional = TypeAdapter(relaxng_scalar_annotation((), may_be_empty=True))
    assert empty_optional.validate_python(None) is None


def test_codec_configuration_errors_are_explicit():
    schema, integer = model_for('<data type="integer"/>')
    plan = schema.projection
    fingerprint = schema._grammar_fingerprint
    with pytest.raises(TypeError):
        ScalarCodec("not-a-schema", plan, fingerprint)
    with pytest.raises(TypeError):
        ScalarCodec(schema, "not-a-plan", fingerprint)
    with pytest.raises(ValueError):
        ScalarCodec(schema, plan, "")

    codec = ScalarCodec(schema, plan, fingerprint)
    contract = root_contracts(schema)[0]
    with pytest.raises(TypeError):
        codec.pattern_for("not-a-contract")
    with pytest.raises(IntegrationError):
        codec.pattern_for(replace(contract, pattern_identity="missing"))
    with pytest.raises(IntegrationError):
        codec.pattern_for(replace(contract, type_name="forged"))

    with pytest.raises(IntegrationError):
        relaxng_scalar_annotation((object(),), codec=codec)
    assert relaxng_scalar_annotation([contract], codec=codec)
    with pytest.raises(IntegrationError):
        relaxng_scalar_annotation(
            root_contracts(model_for('<data type="string"/>')[0]), codec=codec
        )

    assert integer.model_validate(1).root == 1


def test_xsd_models_keep_existing_behavior():
    registry = models(compile_schema(RECORD_SCHEMA))
    assert isinstance(registry, ModelSet)
    Root = registry.model_for(element="root")
    model = Root.model_validate({"count": 1})
    assert model.count == 1
    assert model.attr_version == 9
    assert model.item == []
