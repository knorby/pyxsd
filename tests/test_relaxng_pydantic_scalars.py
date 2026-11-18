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
    _atomic_json_value,
    _decode_list,
    _decode_token,
    _generic_json_value,
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

    def encode(self, value: object, context: DatatypeContext) -> str:
        del context
        if isinstance(value, str):
            return value
        raise TypeError("expected a string")


class EncodeOnlyDatatype(NonDecodingDatatype):
    """A datatype with an encoder but no decoder declaration."""

    type_name = "encode-only"

    def decode(self, lexical: str, context: DatatypeContext) -> object:
        del lexical, context
        raise AssertionError("a nondecodable datatype must not be decoded")

    def encode(self, value: object, context: DatatypeContext) -> str:
        del context
        if isinstance(value, str):
            return value
        raise TypeError("expected a string")


class UnsupportedDecodingFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return UnsupportedDecodingDatatype()


class EncodeOnlyFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return EncodeOnlyDatatype()


class ResultDatatype(Datatype):
    """A custom decoder-only type used for one-way round-trip tests."""

    library_uri = "urn:result"
    type_name = "result"
    parameters = ()
    supports_decode = True

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def allows(self, lexical: str, context: DatatypeContext) -> bool:
        del context
        return lexical == "value"

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


class ResultEncodingDatatype(ResultDatatype):
    """A decoder+encoder type whose decoded strings must not be re-decoded."""

    def encode(self, value: object, context: DatatypeContext) -> str:
        del context
        if isinstance(value, str) and value.startswith("result:"):
            return value[len("result:") :]
        raise TypeError("expected a result: decoded value")


class LossyEncodingDatatype(ResultEncodingDatatype):
    """A decoder+encoder whose encoder produces a lexically invalid form."""

    type_name = "lossy"

    def encode(self, value: object, context: DatatypeContext) -> str:
        del value, context
        return "bad"


class LossyEncodingFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return LossyEncodingDatatype()


class ResultFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return ResultDatatype()


class ResultEncodingFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return ResultEncodingDatatype()


class CustomQNameDatatype(Datatype):
    """A custom-library type merely named ``QName``, not the XSD datatype."""

    library_uri = "urn:custom-qname"
    type_name = "QName"
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
        return lexical

    def encode(self, value: object, context: DatatypeContext) -> str:
        del context
        if isinstance(value, str):
            return value
        raise TypeError("expected a string")


class CustomQNameFactory(DatatypeFactory):
    def create(self, type_name, parameters, context):
        del type_name, parameters, context
        return CustomQNameDatatype()


def registration(factory: DatatypeFactory, fingerprint: str) -> DatatypeLibraryRegistration:
    return DatatypeLibraryRegistration(factory, fingerprint)


def snapshot_for(
    schema: RelaxNGSchema,
    contract: ScalarContract,
    lexical: str,
    decoded: object,
    *,
    context: DatatypeContext | None = None,
    decodable: bool = True,
) -> AlternativeValue:
    return AlternativeValue(
        lexical,
        (
            Candidate(
                candidate_id(schema._grammar_fingerprint, contract),
                contract,
                decoded,
                decodable,
            ),
        ),
        context if context is not None else DatatypeContext(None, {}),
        schema._grammar_fingerprint,
        STANDALONE_REVISION,
    )


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

    _, encode_only = model_for(
        '<data type="encode-only" datatypeLibrary="urn:test"/>',
        datatype_libraries={"urn:test": registration(EncodeOnlyFactory(), "test/3")},
    )
    assert not encode_only.model_validate("ok").root.candidates[0].decodable

    _, lossy = model_for(
        '<data type="lossy" datatypeLibrary="urn:test"/>',
        datatype_libraries={"urn:test": registration(LossyEncodingFactory(), "test/4")},
    )
    lossy_value = lossy.model_validate("value").root
    assert isinstance(lossy_value, AlternativeValue)
    assert lossy_value.candidates[0].decoded == "result:value"

    opaque_schema, _ = model_for(
        '<data type="opaque" datatypeLibrary="urn:test"/>',
        datatype_libraries={"urn:test": registration(NonDecodingFactory(), "test/1")},
    )
    opaque_pattern = opaque_schema.projection.encoding_patterns[
        root_contracts(opaque_schema)[0].pattern_identity
    ]
    assert _decode_token(opaque_pattern, "ok", DatatypeContext(None, {})) is _UNDECODABLE


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
    for typed in ([1, 2], (1, 2)):
        with pytest.raises(ValidationError) as error:
            ambiguous.model_validate(typed)
        assert "ambiguous" in message(error.value)

    _, heterogeneous = model_for(
        '<choice><list><data type="integer"/><data type="token" datatypeLibrary=""/></list>'
        '<data type="integer"/></choice>'
    )
    with pytest.raises(ValidationError):
        heterogeneous.model_validate((1, "a"))
    with pytest.raises(ValidationError):
        heterogeneous.model_validate([1, "a"])

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
        datatype_libraries={"urn:result": registration(ResultEncodingFactory(), "result/2")},
    )
    schema = custom.model_json_schema()
    assert {"type": "string"} in schema["anyOf"]
    assert {"type": "number"} in schema["anyOf"]
    assert {"type": "boolean"} in schema["anyOf"]
    assert not any("$ref" in branch for branch in schema["anyOf"])
    assert custom.model_validate("value").root == "result:value"
    assert custom.model_validate("result:value").root == "result:value"
    assert custom.model_validate("value").model_dump_json() == '"result:value"'
    assert (
        custom.model_validate_json(custom.model_validate("value").model_dump_json()).root
        == "result:value"
    )
    validation = next(
        branch["x-pyxsd-validation"] for branch in schema["anyOf"] if "x-pyxsd-validation" in branch
    )
    assert set(validation) == {"grammar-fingerprint", "candidate-completeness"}


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
    with pytest.raises(TypeError):
        ScalarCodec(schema, plan, 1)
    with pytest.raises(IntegrationError):
        ScalarCodec(schema, plan, "")
    with pytest.raises(IntegrationError):
        ScalarCodec(schema, plan, plan.grammar_fingerprint)
    assert ScalarCodec(schema, plan).grammar_fingerprint == fingerprint
    assert ScalarCodec(schema, plan, fingerprint).grammar_fingerprint == fingerprint
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


def test_opaque_empty_lists_remain_nondecodable_wrappers():
    opaque = {"urn:test": registration(NonDecodingFactory(), "test/1")}
    schema, empty_list = model_for(
        '<list><zeroOrMore><data type="opaque" datatypeLibrary="urn:test"/></zeroOrMore></list>',
        datatype_libraries=opaque,
    )
    for lexical in ("", " \t\n"):
        value = empty_list.model_validate(lexical).root
        assert isinstance(value, AlternativeValue)
        assert value.lexical == lexical
        assert len(value.candidates) == 1
        assert value.candidates[0].decodable is False
        with pytest.raises(UnsupportedDatatypeOperation):
            value.as_candidate(value.candidates[0].candidate_id)
        tag = scalar_tag(value)
        assert empty_list.model_validate(tag).root == value
        assert empty_list.model_validate_json(json.dumps(tag)).root == value
        assert empty_list.model_validate(lexical).model_dump() is value
        assert json.loads(empty_list.model_validate(lexical).model_dump_json()) == tag

    schema = empty_list.model_json_schema()
    assert not any(branch.get("type") == "array" for branch in schema["anyOf"])
    assert {"type": "string"} in schema["anyOf"]
    assert any(branch.get("type") == "object" for branch in schema["anyOf"])

    _, ambiguous = model_for(
        '<choice><list><zeroOrMore><data type="opaque" datatypeLibrary="urn:test"/>'
        '</zeroOrMore></list><data type="integer"/></choice>',
        datatype_libraries=opaque,
    )
    for lexical in ("", " \t\n"):
        value = ambiguous.model_validate(lexical).root
        assert isinstance(value, AlternativeValue)
        assert value.lexical == lexical
        assert [candidate.decodable for candidate in value.candidates] == [False]
        tag = scalar_tag(value)
        assert ambiguous.model_validate(tag).root == value


def test_python_dumps_preserve_wrapper_identity_and_revalidate():
    _, ambiguous = model_for('<choice><data type="integer"/><data type="string"/></choice>')
    model = ambiguous.model_validate("12")
    python_dump = model.model_dump()
    assert python_dump is model.root
    assert model.model_dump(mode="python") is model.root
    assert ambiguous.model_validate(python_dump).root == model.root
    assert model.model_dump(mode="json")["#value"]["version"] == 1

    _, record = model_for(
        '<attribute name="a"><choice><data type="integer"/><data type="string"/></choice>'
        "</attribute><empty/>"
    )
    record_model = record.model_validate({"@a": "12"})
    record_dump = record_model.model_dump()
    assert record_dump["@a"] is record_model.attr_a
    assert record_model.model_dump(by_alias=False)["attr_a"] is record_model.attr_a
    assert record_model.model_dump(exclude={"attr_a"}) == {}
    assert record.model_validate(record_dump).attr_a == record_model.attr_a
    assert record_model.model_dump(mode="json")["@a"]["#value"]["version"] == 1

    decoder_only = {"urn:result": registration(ResultFactory(), "result/1")}
    _, custom = model_for(
        '<data type="result" datatypeLibrary="urn:result"/>',
        datatype_libraries=decoder_only,
    )
    custom_model = custom.model_validate("value")
    assert isinstance(custom_model.root, AlternativeValue)
    assert custom_model.model_dump() is custom_model.root
    assert custom.model_validate(custom_model.model_dump()).root == custom_model.root
    assert json.loads(custom_model.model_dump_json())["#value"]["lexical"] == "value"

    _, custom_list = model_for(
        '<list><zeroOrMore><data type="result" datatypeLibrary="urn:result"/></zeroOrMore></list>',
        datatype_libraries=decoder_only,
    )
    list_model = custom_list.model_validate("value value")
    assert isinstance(list_model.root, AlternativeValue)
    assert list_model.model_dump() is list_model.root
    assert custom_list.model_validate(list_model.model_dump()).root == list_model.root

    _, parent = model_for(
        '<element name="child"><choice><data type="integer"/><data type="string"/>'
        "</choice></element>"
    )
    parent_model = parent.model_validate({"child": "12"})
    parent_dump = parent_model.model_dump()
    assert parent_dump["child"] is parent_model.child.root
    assert parent.model_validate(parent_dump).child.root == parent_model.child.root

    _, repeated = model_for(
        '<zeroOrMore><element name="child"><choice><data type="integer"/>'
        '<data type="string"/></choice></element></zeroOrMore>'
    )
    repeated_model = repeated.model_validate({"child": ["12", "text"]})
    repeated_dump = repeated_model.model_dump()
    assert repeated_dump["child"][0] is repeated_model.child[0].root
    assert repeated_dump["child"][1] is repeated_model.child[1].root
    assert repeated.model_validate(repeated_dump).child[0].root == repeated_model.child[0].root


def test_json_lists_round_trip_declared_token_shapes():
    context = {"relaxng": {"namespaces": {"p": "urn:x"}}}
    _, mixed = model_for('<list><data type="decimal"/><data type="QName"/></list>')
    mixed_model = mixed.model_validate_json('["1.20", "Q{urn:x}x"]', context=context)
    assert mixed_model.root[0] == Decimal("1.20")
    assert mixed_model.root[1].to_expanded_name() == "{urn:x}x"
    mixed_dump = json.loads(mixed_model.model_dump_json())
    assert mixed_dump == ["1.20", "Q{urn:x}x"]
    assert mixed.model_validate_json(mixed_model.model_dump_json(), context=context).root == (
        mixed_model.root
    )
    assert_export_matches(mixed, mixed_model)
    mixed_schema = next(
        branch for branch in mixed.model_json_schema()["anyOf"] if branch.get("type") == "array"
    )
    assert mixed_schema["prefixItems"][0]["type"] == "string"
    assert mixed_schema["prefixItems"][0]["x-pyxsd-type"] == "decimal"
    assert mixed_schema["prefixItems"][1]["x-pyxsd-qname"] is True

    _, floats = model_for('<list><zeroOrMore><data type="float"/></zeroOrMore></list>')
    float_model = floats.model_validate_json('["NaN", 1.5]')
    assert math.isnan(float_model.root[0])
    assert json.loads(float_model.model_dump_json()) == ["NaN", 1.5]
    assert math.isnan(floats.model_validate_json(float_model.model_dump_json()).root[0])

    _, integers = model_for('<list><zeroOrMore><data type="integer"/></zeroOrMore></list>')
    empty = integers.model_validate_json("[]")
    assert empty.root == []
    assert json.loads(empty.model_dump_json()) == []
    integers_schema = integers.model_json_schema()
    array_schema = next(
        branch for branch in integers_schema["anyOf"] if branch.get("type") == "array"
    )
    assert array_schema["items"] == {"type": "integer"}

    _, xsd_list = model_for('<data type="IDREFS"/>')
    xsd_model = xsd_list.model_validate_json('["a", "b"]')
    assert str(xsd_model.root) == "a b"
    assert json.loads(xsd_model.model_dump_json()) == ["a", "b"]
    assert xsd_list.model_validate_json(xsd_model.model_dump_json()).root == xsd_model.root
    xsd_schema = next(
        branch for branch in xsd_list.model_json_schema()["anyOf"] if branch.get("type") == "array"
    )
    assert xsd_schema["minItems"] == 1
    assert xsd_schema["items"] == {"type": "string"}

    _, attribute_list = model_for('<attribute name="ids"><data type="IDREFS"/></attribute><empty/>')
    attribute_model = attribute_list.model_validate_json('{"@ids": ["a", "b"]}')
    assert str(attribute_model.attr_ids) == "a b"
    assert json.loads(attribute_model.model_dump_json()) == {"@ids": ["a", "b"]}
    assert (
        attribute_list.model_validate_json(attribute_model.model_dump_json()).attr_ids
        == attribute_model.attr_ids
    )


def test_binary_scalars_and_lists_round_trip():
    for name, raw, encoded in (
        ("base64Binary", b"ab", "YWI="),
        ("hexBinary", b"\x01\xff", "01FF"),
    ):
        _, model = model_for(f'<data type="{name}"/>')
        value = model.model_validate(raw)
        assert value.root == raw
        assert json.loads(value.model_dump_json()) == encoded
        assert model.model_validate_json(value.model_dump_json()).root == raw
        schema = next(
            branch
            for branch in model.model_json_schema()["anyOf"]
            if branch.get("x-pyxsd-encoding") is not None
        )
        assert schema["type"] == "string"
        assert schema["x-pyxsd-type"] == name
    with pytest.raises(ValidationError):
        model_for('<data type="base64Binary"/>')[1].model_validate_json('"!!!!"')

    _, binary_list = model_for('<list><data type="base64Binary"/><data type="hexBinary"/></list>')
    listed = binary_list.model_validate((b"ab", b"\x01\xff"))
    assert listed.root == (b"ab", b"\x01\xff")
    assert json.loads(listed.model_dump_json()) == ["YWI=", "01FF"]
    assert binary_list.model_validate_json(listed.model_dump_json()).root == listed.root
    list_schema = next(
        branch
        for branch in binary_list.model_json_schema()["anyOf"]
        if branch.get("type") == "array"
    )
    assert list_schema["prefixItems"] == [
        {"type": "string", "x-pyxsd-type": "base64Binary", "x-pyxsd-encoding": "base64"},
        {"type": "string", "x-pyxsd-type": "hexBinary", "x-pyxsd-encoding": "hex"},
    ]


def test_decoder_only_and_round_trip_encodable_contracts():
    decoder_only = {"urn:result": registration(ResultFactory(), "result/1")}
    _, model = model_for(
        '<data type="result" datatypeLibrary="urn:result"/>',
        datatype_libraries=decoder_only,
    )
    value = model.model_validate("value").root
    assert isinstance(value, AlternativeValue)
    assert value.candidates[0].decoded == "result:value"
    assert value.candidates[0].decodable is True
    assert json.loads(model.model_validate("value").model_dump_json())["#value"]["lexical"] == (
        "value"
    )
    with pytest.raises(ValidationError) as error:
        model.model_validate("result:value")
    assert "decoder-only" in message(error.value)
    assert any(branch.get("type") == "object" for branch in model.model_json_schema()["anyOf"])

    _, decoded_list = model_for(
        '<list><zeroOrMore><data type="result" datatypeLibrary="urn:result"/></zeroOrMore></list>',
        datatype_libraries=decoder_only,
    )
    list_value = decoded_list.model_validate("value value").root
    assert isinstance(list_value, AlternativeValue)
    assert list_value.candidates[0].decoded == ["result:value", "result:value"]
    assert decoded_list.model_validate("").root == []

    encoding = {"urn:result": registration(ResultEncodingFactory(), "result/2")}
    _, encoded = model_for(
        '<data type="result" datatypeLibrary="urn:result"/>',
        datatype_libraries=encoding,
    )
    lexical = encoded.model_validate("value").root
    decoded_form = encoded.model_validate("result:value").root
    assert lexical == decoded_form == "result:value"
    assert "result:result:" not in lexical
    assert json.loads(encoded.model_validate("value").model_dump_json()) == "result:value"
    assert encoded.model_validate_json('"result:value"').root == "result:value"


def test_codec_fingerprint_matches_public_schema_identity():
    schema, model = model_for('<choice><data type="integer"/><data type="string"/></choice>')
    plan = schema.projection
    assert plan.grammar_fingerprint != schema._grammar_fingerprint
    with pytest.raises(IntegrationError):
        ScalarCodec(schema, plan, plan.grammar_fingerprint)
    codec = ScalarCodec(schema, plan, schema._grammar_fingerprint)
    assert codec.grammar_fingerprint == schema._grammar_fingerprint

    model_value = model.model_validate("12").root
    document = schema.parse(io.StringIO("<doc>12</doc>"))
    document.require_valid()
    document_value = document.root.scalar_value()
    model_tag = scalar_tag(model_value)
    document_tag = scalar_tag(document_value)
    assert model_tag["#value"]["grammar"] == schema._grammar_fingerprint
    assert model_tag["#value"]["candidates"] == document_tag["#value"]["candidates"]
    assert model.model_validate(document_value).root == model_value


def test_record_json_dump_uses_aliases_and_validates_against_schema():
    _, record = model_for(
        '<attribute name="count"><data type="integer"/></attribute>'
        '<optional><data type="boolean"/></optional>'
    )
    model = record.model_validate({"@count": "2", "$": True})
    dumped = json.loads(model.model_dump_json())
    assert dumped == {"@count": 2, "$": True}
    schema = record.model_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(dumped)
    assert record.model_validate_json(model.model_dump_json()).attr_count == 2
    assert record.model_validate_json(model.model_dump_json()).xml_value is True


def test_unique_tags_and_snapshots_are_rechecked():
    schema, model = model_for('<data type="integer"/>')
    contract = root_contracts(schema)[0]
    snapshot = snapshot_for(schema, contract, "7", 7)
    assert model.model_validate(snapshot).root == 7
    tag = scalar_tag(snapshot)
    assert model.model_validate(tag).root == 7
    with pytest.raises(ValidationError):
        model.model_validate(replace(snapshot, grammar_fingerprint="other"))
    with pytest.raises(ValidationError):
        model.model_validate(
            replace(
                snapshot,
                candidates=(replace(snapshot.candidates[0], candidate_id="other"),),
            )
        )
    bad_tag = deepcopy(tag)
    bad_tag["#value"]["candidates"] = ["other"]
    with pytest.raises(ValidationError):
        model.model_validate(bad_tag)
    assert model.model_validate(replace(snapshot, revision=123)).root == 7


def test_list_interpretation_is_a_scoped_assertion_without_token_graph():
    schema, model = model_for(
        '<list><choice><data type="integer"/><data type="string"/></choice></list>'
    )
    document = schema.parse(io.StringIO("<doc>12</doc>"))
    document.require_valid()
    value = document.root.scalar_value()
    interpretation = value.interpretations[0]
    result = model.model_validate(interpretation).root
    assert isinstance(result, AlternativeValue)
    assert result.lexical == "12"
    assert result.interpretations == ()
    assert model.model_validate(scalar_tag(result)).root == result

    with pytest.raises(ValidationError):
        model.model_validate(replace(interpretation, grammar_fingerprint="other"))
    with pytest.raises(ValidationError):
        model.model_validate(
            interpretation,
            context={"relaxng": {"namespaces": {"p": "urn:conflict"}}},
        )
    with pytest.raises(ValidationError):
        model.model_validate(replace(interpretation, candidate_id="other"))

    other_schema, _ = model_for(
        '<list><zeroOrMore><data type="token" datatypeLibrary=""/></zeroOrMore></list>'
    )
    other_contract = root_contracts(other_schema)[0]
    not_static = replace(
        interpretation,
        contract=other_contract,
        candidate_id=candidate_id(schema._grammar_fingerprint, other_contract),
    )
    with pytest.raises(ValidationError) as error:
        model.model_validate(not_static)
    assert "not statically possible" in message(error.value)

    schema2, model2 = model_for(
        '<choice><list><oneOrMore><data type="integer"/></oneOrMore></list>'
        '<list><oneOrMore><data type="token" datatypeLibrary=""/></oneOrMore></list></choice>'
    )
    document2 = schema2.parse(io.StringIO("<doc>abc</doc>"))
    document2.require_valid()
    token_value = document2.root.scalar_value()
    token_interpretation = token_value.interpretations[0]
    contracts2 = root_contracts(schema2)
    integer_contract = next(
        contract for contract in contracts2 if contract != token_interpretation.contract
    )
    missing_candidate = replace(
        token_interpretation,
        contract=integer_contract,
        candidate_id=candidate_id(schema2._grammar_fingerprint, integer_contract),
    )
    with pytest.raises(ValidationError) as error:
        model2.model_validate(missing_candidate)
    assert "recomputed candidates" in message(error.value)

    _, integer = model_for('<data type="integer"/>')
    with pytest.raises(ValidationError) as error:
        integer.model_validate(interpretation)
    assert "requires a list scalar position" in message(error.value)


def test_custom_library_qname_type_is_not_xsd_qname():
    custom = {
        "urn:custom-qname": registration(CustomQNameFactory(), "custom-qname/1"),
    }
    _, model = model_for(
        '<data type="QName" datatypeLibrary="urn:custom-qname"/>',
        datatype_libraries=custom,
    )
    assert model.model_validate("Q{urn:x}x").root == "Q{urn:x}x"
    assert model.model_validate_json('"Q{urn:x}x"').root == "Q{urn:x}x"


def test_json_list_edge_contracts_and_direct_conversion_helpers():
    _, integer = model_for('<data type="integer"/>')
    with pytest.raises(ValidationError):
        integer.model_validate_json("[1]")

    _, interpretation = model_for(
        '<list><choice><data type="integer"/><data type="string"/></choice></list>'
    )
    with pytest.raises(ValidationError) as error:
        interpretation.model_validate_json("[12]")
    assert "statically representable" in message(error.value)

    _, fixed = model_for(
        '<list><data type="integer"/><data type="token" datatypeLibrary=""/></list>'
    )
    with pytest.raises(ValidationError) as error:
        fixed.model_validate_json("[1]")
    assert "requires 2 tokens" in message(error.value)

    _, xsd_list = model_for('<data type="IDREFS"/>')
    with pytest.raises(ValidationError) as error:
        xsd_list.model_validate_json("[1]")
    assert "string tokens" in message(error.value)

    _, list_of_xsd_list = model_for('<list><data type="IDREFS"/></list>')
    nested = list_of_xsd_list.model_validate_json('[["a"]]')
    assert json.loads(nested.model_dump_json()) == [["a"]]
    assert list_of_xsd_list.model_validate_json(nested.model_dump_json()).root == nested.root
    with pytest.raises(ValidationError) as error:
        list_of_xsd_list.model_validate_json('[["a", "b"]]')
    assert "cannot contain XML whitespace" in message(error.value)

    _, floats = model_for('<list><zeroOrMore><data type="float"/></zeroOrMore></list>')
    infinities = floats.model_validate_json('["INF", "-INF"]')
    assert json.loads(infinities.model_dump_json()) == ["INF", "-INF"]


class _LexicalValue:
    def lexical(self) -> str:
        return "2020-01-01"


def test_contract_json_helpers_cover_declared_branches():
    assert _generic_json_value(None) is None
    assert _generic_json_value(True) is True
    assert _generic_json_value(2) == 2
    assert _generic_json_value(1.5) == 1.5
    assert _generic_json_value("x") == "x"
    assert _generic_json_value(b"ab") == "YWI="
    assert _generic_json_value(Decimal("1.20")) == "1.20"
    assert _generic_json_value([Decimal("1.20")]) == ["1.20"]
    with pytest.raises(TypeError):
        _generic_json_value(object())

    binary_schema = compile_element('<data type="base64Binary"/>')
    binary_contract = root_contracts(binary_schema)[0]
    assert _atomic_json_value(binary_contract, "YWI=") == "YWI="

    xsd_list_schema = compile_element('<data type="IDREFS"/>')
    xsd_list_contract = root_contracts(xsd_list_schema)[0]
    assert _atomic_json_value(xsd_list_contract, ["a", "b"]) == ["a", "b"]
    assert _atomic_json_value(xsd_list_contract, "a b") == ["a", "b"]
    with pytest.raises(TypeError):
        _atomic_json_value(xsd_list_contract, object())

    qname_schema = compile_element('<data type="QName"/>')
    qname_contract = root_contracts(qname_schema)[0]
    assert _atomic_json_value(qname_contract, "p:x") == "p:x"

    date_schema = compile_element('<data type="date"/>')
    date_contract = root_contracts(date_schema)[0]
    assert _atomic_json_value(date_contract, _LexicalValue()) == "2020-01-01"
    assert _atomic_json_value(date_contract, "2020-01-01") == "2020-01-01"
    with pytest.raises(TypeError):
        _atomic_json_value(date_contract, object())

    float_schema = compile_element('<data type="float"/>')
    float_contract = root_contracts(float_schema)[0]
    assert _atomic_json_value(float_contract, float("inf")) == "INF"
    assert _atomic_json_value(float_contract, float("-inf")) == "-INF"
    assert _atomic_json_value(float_contract, 1.5) == 1.5


def test_unique_text_and_codec_less_json_dumps():
    _, text = model_for("<text/>")
    dumped = text.model_validate("value")
    assert json.loads(dumped.model_dump_json()) == "value"
    assert dumped.model_dump() == "value"

    schema = compile_element("<text/>")
    contract = root_contracts(schema)[0]
    adapter = TypeAdapter(relaxng_scalar_annotation((contract,)))
    assert adapter.dump_python("value") == "value"
    assert adapter.dump_json("value") == b'"value"'


def test_xsd_models_keep_existing_behavior():
    registry = models(compile_schema(RECORD_SCHEMA))
    assert isinstance(registry, ModelSet)
    Root = registry.model_for(element="root")
    model = Root.model_validate({"count": 1})
    assert model.count == 1
    assert model.attr_version == 9
    assert model.item == []
