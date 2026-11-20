"""Executable release evidence for native RELAX NG support.

This module turns the design's release matrix, its required negative/boundary
tests, the pinned Jing/Trang catalog, the adversarial resource measurements,
and the CI configuration into checks that run with the ordinary suite.  It is
not a prose checklist: every matrix row names concrete test node IDs, the
corpus audit reads the audited TOML records, and the CI audit parses the
workflow, coverage, and project configuration that make the claim true.

The pinned catalogs are vendored, not downloaded, so the corpus audit always
runs.  The corpus execution gates themselves remain opt-in with
``PYXSD_RUN_RELAXNG=1`` and are mapped into the Compile row rather than being
duplicated here.
"""

from __future__ import annotations

import configparser
import gc
import hashlib
import re
import subprocess
import sys
import threading
import time
import tomllib
import tracemalloc
import weakref
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests import report_relaxng
from tests.relaxng.baseline import Baseline, BaselineError
from tests.relaxng.baseline import load as load_baseline
from tests.relaxng.baseline import save as save_baseline
from tests.relaxng.dispositions import (
    load_dispositions,
    release_violations,
    validate_dispositions,
)
from tests.relaxng.model import (
    PROFILE_CORE,
    PROFILE_XSD_DATATYPE,
    REQUIRED_PROFILES,
    Outcome,
)

from pyxsd.relaxng import RelaxNGElement

ROOT = Path(__file__).resolve().parents[1]
RELAXNG = ROOT / "src" / "pyxsd" / "relaxng"
CORPUS = ROOT / "tests" / "relaxng" / "corpus"
BASELINE = ROOT / "tests" / "relaxng" / "baseline.toml"
DISPOSITIONS = ROOT / "tests" / "relaxng" / "dispositions.toml"
DESIGN_MATRIX_ROWS: tuple[str, ...] = (
    "Compile, parse, revalidate, batch",
    "Mutable document and writers",
    "Typed Python access",
    "Stubs",
    "Dictionary/data/JSON export",
    "Data/JSON import",
    "JSON Schema generation",
    "Introspection and search",
    "Pydantic",
    "Arrow, Parquet, and datasets",
    "Schema comparison",
    "MCP and CLI",
)

#: Each entry is ``(node id, optional dependency)``.  A ``None`` dependency is
#: part of the dependency-free gate.  Optional rows resolve and run in the
#: installed-adapter/MCP jobs; the dependency-free gate still proves that the
#: files exist and that the row has a core anchor.
MATRIX_TESTS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "Compile, parse, revalidate, batch": (
        (
            "tests/test_relaxng_schema.py::test_compile_returns_the_valid_composed_schema"
            "_with_an_empty_report",
            None,
        ),
        (
            "tests/test_relaxng_parse.py::test_parse_accepts_paths_streams_and_preparsed_elements",
            None,
        ),
        (
            "tests/test_relaxng_parse.py::test_schema_report_errors_are_rejected_before_input"
            "_is_consumed",
            None,
        ),
        (
            "tests/test_relaxng_revalidation.py::test_typed_revalidation_replaces_every_snapshot"
            "_and_keeps_the_plan_class",
            None,
        ),
        (
            "tests/test_relaxng_batch.py::test_report_mode_isolates_each_input_and_retains"
            "_lexical_documents",
            None,
        ),
        (
            "tests/test_relaxng_recovery.py::test_two_independent_sibling_subtree_errors_are"
            "_both_reported",
            None,
        ),
        (
            "tests/test_relaxng_schema_corpus.py::test_pinned_schema_corpus_matches_the"
            "_checked_in_baseline",
            None,
        ),
        (
            "tests/test_relaxng_instance_corpus.py::test_pinned_instance_corpus_matches_the"
            "_checked_in_baseline",
            None,
        ),
        (
            "tests/test_relaxng_typed_corpus.py::test_pinned_instance_corpus_is_typed"
            "_equivalent_to_validation_only",
            None,
        ),
        (
            "tests/test_relaxng_schema.py::test_independent_schemas_compile_concurrently",
            None,
        ),
        (
            "tests/test_relaxng_limits.py::test_concurrent_parse_on_one_schema_is_rejected"
            "_without_hanging",
            None,
        ),
    ),
    "Mutable document and writers": (
        (
            "tests/test_relaxng_document.py::test_writers_preserve_character_values_and_escaped"
            "_markup",
            None,
        ),
        (
            "tests/test_relaxng_document.py::test_xpath_and_elementtree_queries_map_to_the"
            "_original_nodes",
            None,
        ),
        (
            "tests/test_relaxng_document.py::test_root_callable_transform_returns_a_stale"
            "_relaxng_document",
            None,
        ),
        ("tests/test_relaxng_document.py::test_xdm_is_an_unannotated_xml_tree", None),
        (
            "tests/test_relaxng_mutation.py::test_mutation_matrix_invalidates_the_whole_revision",
            None,
        ),
    ),
    "Typed Python access": (
        (
            "tests/test_relaxng_binding.py::test_attribute_descriptors_reuse_typed_values_and"
            "_schema_ambiguity",
            None,
        ),
        (
            "tests/test_relaxng_binding.py::test_child_descriptors_match_cardinality_and_return"
            "_lexical_nodes",
            None,
        ),
        (
            "tests/test_relaxng_binding.py::test_generic_descendant_preserves_stable_parent_and"
            "_sibling_accessors",
            None,
        ),
        (
            "tests/test_relaxng_typed_parse.py::test_unique_atomic_contract_returns_its_ordinary"
            "_python_value",
            None,
        ),
    ),
    "Stubs": (
        ("tests/test_relaxng_stubs.py::test_full_stub_is_structurally_valid_python", None),
        ("tests/test_relaxng_stubs.py::test_emitted_surface_matches_runtime_descriptors", None),
        ("tests/test_relaxng_stubs.py::test_one_class_per_stable_route_not_per_define", None),
        ("tests/test_relaxng_stubs.py::test_generic_route_is_a_comment_not_a_class", None),
    ),
    "Dictionary/data/JSON export": (
        (
            "tests/test_relaxng_conversion_parity.py::test_every_public_entry_accepts_all"
            "_five_formats",
            None,
        ),
        (
            "tests/test_relaxng_conversion_parity.py::test_stable_values_round_trip_for_every"
            "_format",
            None,
        ),
        ("tests/test_relaxng_xml_formats.py::test_all_formats_retain_or_absorb_the_root", None),
        (
            "tests/test_relaxng_xml_formats.py::test_xsi_attributes_are_ordinary_data_and_never"
            "_synthesize_nil",
            None,
        ),
        (
            "tests/test_relaxng_scalar_tags.py::test_exact_tag_has_only_wire_identity_and_copied"
            "_context",
            None,
        ),
    ),
    "Data/JSON import": (
        ("tests/test_relaxng_json_import.py::test_round_trip_reexports_identical_data", None),
        (
            "tests/test_relaxng_json_import.py::test_profile_alternatives_round_trip_with"
            "_complete_candidates",
            None,
        ),
        ("tests/test_relaxng_json_import.py::test_malformed_tags_are_conversion_errors", None),
        (
            "tests/test_relaxng_conversion_parity.py::test_grouped_import_reconstructs_unique"
            "_repeated_child_sequence",
            None,
        ),
    ),
    "JSON Schema generation": (
        ("tests/test_relaxng_json_schema.py::test_profile_envelope_and_disclosures", None),
        (
            "tests/test_relaxng_conversion_parity.py::test_every_actual_export_validates_under"
            "_every_fidelity_and_option",
            None,
        ),
        ("tests/test_relaxng_json_schema.py::test_scalar_type_rejections", None),
        (
            "tests/test_relaxng_json_schema.py::test_recursive_route_emits_a_ref_cycle_and"
            "_validates_depth_two",
            None,
        ),
    ),
    "Introspection and search": (
        (
            "tests/test_relaxng_introspection.py::test_inventory_describes_compiled_patterns"
            "_routes_and_resources",
            None,
        ),
        (
            "tests/test_relaxng_introspection.py::test_find_components_filters_by_literal_text"
            "_kind_and_namespace",
            None,
        ),
        (
            "tests/test_relaxng_introspection.py::test_related_components_is_breadth_first_and"
            "_depth_bounded",
            None,
        ),
        (
            "tests/test_relaxng_introspection.py::test_stable_ids_survive_relocation_and"
            "_equivalent_recompilation",
            None,
        ),
    ),
    "Pydantic": (
        (
            "tests/test_relaxng_pydantic_models.py::test_models_before_instance_xml_and_scalar"
            "_validation",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_standalone.py::test_standalone_record_required_optional"
            "_repeat_and_direct_construction",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_documents.py::test_document_model_round_trip_preserves"
            "_values_and_compiled_order",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_scalars.py::test_ambiguous_scalars_reject_bare"
            "_non_strings_and_booleans",
            "pydantic",
        ),
        (
            "tests/test_relaxng_integration_parity.py::test_matched_flat_record_models_share"
            "_alias_field_and_default_contract",
            "pydantic",
        ),
    ),
    "Arrow, Parquet, and datasets": (
        (
            "tests/test_relaxng_record_projection.py::test_field_source_selectors_are_optional"
            "_exclusive_and_frozen",
            "pyarrow",
        ),
        (
            "tests/test_relaxng_arrow_records.py::test_ambiguous_integer_string_selects_datatype"
            "_and_candidate_id",
            "pyarrow",
        ),
        (
            "tests/test_relaxng_arrow_records.py::test_parquet_round_trip_borrowed_sink_and"
            "_atomic_replacement",
            "pyarrow",
        ),
        (
            "tests/test_relaxng_datasets.py::test_relaxng_dataset_manifest_is_schema_neutral",
            "pyarrow",
        ),
    ),
    "Schema comparison": (
        ("tests/test_relaxng_comparison.py::test_semantically_irrelevant_order_and_prefixes", None),
        ("tests/test_relaxng_comparison.py::test_semantic_differences_survive_normalization", None),
        (
            "tests/test_relaxng_comparison.py::test_relocated_dependencies_compare_resolved"
            "_structure_not_bytes",
            None,
        ),
        (
            "tests/mcp/test_relaxng_comparison.py::test_all_four_classifications_across_languages",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_comparison.py::test_mixed_languages_keep_structural_limit"
            "_and_completed_corpus",
            "mcp",
        ),
    ),
    "MCP and CLI": (
        ("tests/test_relaxng_cli.py::test_full_stub_emission_uses_relaxng_compiler", None),
        ("tests/test_relaxng_cli.py::test_strict_exits_nonzero_on_an_invalid_instance", None),
        (
            "tests/test_relaxng_cli.py::test_native_to_data_exports_relaxng_mixed_content"
            "_lexically",
            None,
        ),
        ("tests/test_relaxng_cli.py::test_relaxng_rejects_xsd_only_options", None),
        (
            "tests/mcp/test_relaxng_validation.py::test_valid_relaxng_validation_reports"
            "_boolean_completeness",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_query.py::test_query_uses_validation_only_parsing_without"
            "_evidence",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_conversion.py::test_export_matches_public_relaxng_results"
            "_for_every_format",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_models.py::test_inspect_before_xml_and_independent"
            "_alternative_json_snapshot",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_records.py::test_selector_python_and_ipc_round_trips_and"
            "_legacy_defaults",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_transforms.py::test_relaxng_checkpoint_revalidates_the"
            "_stale_root_result",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_introspection.py::test_capabilities_add_schema_languages"
            "_without_changing_old_keys",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_cache.py::test_explicit_relaxng_compiles_only_through"
            "_the_public_relaxng_compiler",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_faults.py::test_relaxng_budget_exhaustion_is_not_a_verdict",
            "mcp",
        ),
    ),
}

#: The design's required negative and boundary tests, grouped by contract.
BOUNDARY_TESTS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "Evidence": (
        (
            "tests/test_relaxng_evidence.py::test_late_failure_removes_integer_witness_even"
            "_with_interned_residuals",
            None,
        ),
        (
            "tests/test_relaxng_evidence.py::test_identical_semantic_sites_expose_origin"
            "_union_not_branch_selection",
            None,
        ),
        (
            "tests/test_relaxng_joint_selection.py::test_forbidden_token_pair_cannot_escape"
            "_through_an_atomic_branch",
            None,
        ),
        (
            "tests/test_relaxng_joint_selection.py::test_scalar_fields_are_correlated_across"
            "_late_branch",
            None,
        ),
        (
            "tests/test_relaxng_annotations.py::test_capture_off_and_on_produce_identical"
            "_typed_values_and_candidate_ids",
            None,
        ),
        (
            "tests/test_relaxng_evidence.py::test_mandatory_evidence_budgets_raise_instead_of"
            "_truncating",
            None,
        ),
        (
            "tests/test_relaxng_annotations.py::test_zero_capture_budget_marks_annotations"
            "_incomplete_without_truncating_typed_values",
            None,
        ),
        (
            "tests/test_relaxng_typed_parse.py::test_validation_only_documents_are_evidence"
            "_free_and_reject_typed_access",
            None,
        ),
        (
            "tests/test_relaxng_evidence_limits.py::test_repeated_parses_release_parse_state"
            "_and_leave_schema_caches_alone",
            None,
        ),
    ),
    "Projections": (
        (
            "tests/test_relaxng_projection.py::test_equivalent_alternatives_have_one_public"
            "_contract_and_immutable_keys",
            None,
        ),
        (
            "tests/test_relaxng_projection.py::test_scalar_alternatives_merge_at_attribute"
            "_and_child_positions",
            None,
        ),
        (
            "tests/test_relaxng_projection.py::test_exclusive_choice_keeps_optional_fields"
            "_and_the_choice_constraint",
            None,
        ),
        (
            "tests/test_relaxng_projection.py::test_interleave_preserves_named_access_and"
            "_original_order_contract",
            None,
        ),
        (
            "tests/test_relaxng_projection.py::test_recursive_routes_are_finite_and_independent"
            "_of_document_depth",
            None,
        ),
        (
            "tests/test_relaxng_projection.py::test_repeated_same_name_assignments_merge"
            "_without_position_identity",
            None,
        ),
        (
            "tests/test_relaxng_projection.py::test_named_children_coexist_with_an_ordered"
            "_expanded_name_wildcard",
            None,
        ),
        (
            "tests/test_relaxng_projection.py::test_mixed_content_has_no_concatenated_scalar"
            "_descriptor",
            None,
        ),
        (
            "tests/test_relaxng_projection.py::test_generic_descendant_does_not_poison_stable"
            "_parent_or_sibling",
            None,
        ),
    ),
    "Mutation": (
        (
            "tests/test_relaxng_assignment.py::test_all_encodings_are_prepared_before_any_mutation",
            None,
        ),
        (
            "tests/test_relaxng_assignment.py::test_typed_assignment_marks_annotations_stale"
            "_when_captured",
            None,
        ),
        (
            "tests/test_relaxng_mutation.py::test_mutation_matrix_invalidates_the_whole_revision",
            None,
        ),
        (
            "tests/test_relaxng_mutation.py::test_distant_sibling_edit_dirties_the_shared_revision",
            None,
        ),
        (
            "tests/test_relaxng_revalidation.py::test_typed_revalidation_replaces_every_snapshot"
            "_and_keeps_the_plan_class",
            None,
        ),
        (
            "tests/test_relaxng_revalidation.py::test_revalidation_preserves_capture_and_applies"
            "_the_independent_budget",
            None,
        ),
    ),
    "Scalar interchange": (
        ("tests/test_relaxng_scalar_tags.py::test_typed_native_and_profile_tag_ambiguity", None),
        (
            "tests/test_relaxng_scalar_tags.py::test_empty_lexical_alternatives_round_trip_and"
            "_verify",
            None,
        ),
        ("tests/test_relaxng_scalar_tags.py::test_outer_shape_rejected_at_supplied_pointer", None),
        ("tests/test_relaxng_scalar_tags.py::test_each_body_key_is_required", None),
        ("tests/test_relaxng_scalar_tags.py::test_version_is_exact_integer_one", None),
        (
            "tests/test_relaxng_scalar_tags.py::test_candidate_ids_are_nonempty_sorted_unique"
            "_string_array",
            None,
        ),
        (
            "tests/test_relaxng_scalar_tags.py::test_missing_extra_and_stale_candidate_ids_are"
            "_not_filters",
            None,
        ),
        (
            "tests/test_relaxng_json_import.py::test_conflicting_tag_contexts_on_one_node_are"
            "_rejected",
            None,
        ),
    ),
    "Pydantic": (
        (
            "tests/test_relaxng_pydantic_models.py::test_models_before_instance_xml_and_scalar"
            "_validation",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_standalone.py::test_model_validate_json_and_invalid"
            "_scalar_coercions",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_standalone.py::test_exclusive_choices_constants_and"
            "_supported_facets",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_standalone.py::test_local_route_validates_only_its"
            "_own_content_scope",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_scalars.py::test_scoped_context_is_copied_independent"
            "_and_checked_against_tags",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_documents.py::test_model_snapshot_is_independent_of"
            "_source_mutation",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_documents.py::test_revalidate_controls_stale_evidence"
            "_and_never_reuses_candidates",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_standalone.py::test_namespaced_aliases_unknown_fields"
            "_and_model_metadata_stability",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_standalone.py::test_model_instance_validation_rechecks"
            "_mutated_and_constructed_values",
            "pydantic",
        ),
        (
            "tests/test_relaxng_pydantic_scalars.py::test_json_dump_and_schema_preserve"
            "_wrapper_and_input_distinctions",
            "pydantic",
        ),
    ),
    "Comparison": (
        ("tests/test_relaxng_comparison.py::test_semantically_irrelevant_order_and_prefixes", None),
        (
            "tests/test_relaxng_comparison.py::test_relocated_dependencies_compare_resolved"
            "_structure_not_bytes",
            None,
        ),
        (
            "tests/mcp/test_relaxng_comparison.py::test_all_four_classifications_across_languages",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_comparison.py::test_mixed_languages_keep_structural_limit"
            "_and_completed_corpus",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_comparison.py::test_relaxng_compile_limit_is_a_typed_fault"
            "_not_a_corpus_verdict",
            "mcp",
        ),
        (
            "tests/mcp/test_relaxng_comparison.py::test_worker_crash_and_time_budget_never"
            "_become_corpus_verdicts",
            "mcp",
        ),
    ),
    "Diagnostics": (
        (
            "tests/test_relaxng_recovery.py::test_two_independent_sibling_subtree_errors_are"
            "_both_reported",
            None,
        ),
        (
            "tests/test_relaxng_recovery.py::test_multiple_invalid_attributes_report_each"
            "_occurrence_in_sorted_order",
            None,
        ),
        (
            "tests/test_relaxng_recovery.py::test_missing_attribute_is_reported_before_a"
            "_recoverable_content_error",
            None,
        ),
        (
            "tests/test_relaxng_recovery.py::test_recovery_report_order_and_json_are_stable"
            "_across_repetitions",
            None,
        ),
        (
            "tests/test_relaxng_diagnostics.py::test_unexpected_element_reports_earliest"
            "_occurrence_and_expectation",
            None,
        ),
        (
            "tests/test_relaxng_recovery.py::test_recovery_states_never_produce_a_valid_verdict",
            None,
        ),
        (
            "tests/test_relaxng_annotations.py::test_recovered_findings_never_become_accepting"
            "_annotation_origins",
            None,
        ),
        (
            "tests/test_relaxng_recovery.py::test_invalid_attribute_does_not_duplicate_a"
            "_missing_attribute",
            None,
        ),
        (
            "tests/test_relaxng_recovery.py::test_ignored_subtrees_do_not_generate_descendant"
            "_findings",
            None,
        ),
        (
            "tests/mcp/test_relaxng_validation.py::test_zero_retention_reports_incomplete"
            "_diagnostics",
            "mcp",
        ),
    ),
}

REQUIRED_FILES: tuple[Path, ...] = (
    ROOT / "docs" / "relax-ng.md",
    ROOT / "docs" / "index.md",
    ROOT / "docs" / "api.md",
    ROOT / "docs" / "data-conversion.md",
    ROOT / "docs" / "integrations.md",
    ROOT / "docs" / "mcp.md",
    ROOT / "docs" / "supported.md",
    ROOT / "src" / "pyxsd" / "relaxng" / "__init__.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "schema.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "document.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "syntax.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "simplify.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "checks.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "patterns.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "datatypes.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "validator.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "binding.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "annotations.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "conversion.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "introspection.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "comparison.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "pydantic_adapter.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "pydantic_values.py",
    ROOT / "src" / "pyxsd" / "relaxng" / "records.py",
    CORPUS / "spectest.xml",
    CORPUS / "xsdtest.xml",
    CORPUS / "PROVENANCE.md",
    CORPUS / "SHA256SUMS",
    BASELINE,
    DISPOSITIONS,
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "mcp.yml",
    ROOT / ".github" / "workflows" / "optional-integrations.yml",
    ROOT / "tests" / "integration.coveragerc",
)

_OPTIONAL_MODULES = {
    "pydantic": "pydantic",
    "pyarrow": "pyarrow",
    "mcp": "mcp",
}


def _dependency_available(dependency: str | None) -> bool:
    if dependency is None:
        return True
    import importlib.metadata

    try:
        importlib.metadata.version(_OPTIONAL_MODULES[dependency])
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def _node_ids(files: list[str]) -> set[str]:
    """Collect node IDs, keeping core and MCP suites in separate invocations.

    The repository's combined collection has a pre-existing duplicate-basename
    limitation; CI runs the two suites separately and this audit follows the
    same split rather than changing that behavior.
    """
    groups = {
        "core": [name for name in files if not name.startswith("tests/mcp/")],
        "mcp": [name for name in files if name.startswith("tests/mcp/")],
    }
    node_ids: set[str] = set()
    for label, group in groups.items():
        if not group:
            continue
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *sorted(group)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
        # Exit code 5 means every module in this group skipped at import time
        # because its optional dependency is absent.  The mapping treats that
        # same dependency as unavailable and does not assert those IDs here.
        if result.returncode not in (0, 5):
            pytest.fail(f"{label} pytest collection failed:\n{result.stdout}\n{result.stderr}")
        for line in result.stdout.splitlines():
            line = line.strip()
            if "::" in line:
                node_ids.add(line)
    return node_ids


def _assert_resolves(node_id: str, node_ids: set[str]) -> None:
    if node_id in node_ids:
        return
    prefix = node_id + "["
    if any(collected.startswith(prefix) for collected in node_ids):
        return
    pytest.fail(f"the release mapping names an unresolvable test node: {node_id}")


@pytest.fixture(scope="module")
def collected_node_ids() -> set[str]:
    files: set[str] = set()
    for mapping in (MATRIX_TESTS, BOUNDARY_TESTS):
        for entries in mapping.values():
            for node_id, dependency in entries:
                if _dependency_available(dependency):
                    files.add(node_id.split("::", 1)[0])
    return _node_ids(sorted(files))


def test_every_release_file_exists() -> None:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_FILES if not path.is_file()]
    assert not missing, missing


def test_release_matrix_covers_every_design_row() -> None:
    assert tuple(MATRIX_TESTS) == DESIGN_MATRIX_ROWS
    assert set(BOUNDARY_TESTS) == {
        "Evidence",
        "Projections",
        "Mutation",
        "Scalar interchange",
        "Pydantic",
        "Comparison",
        "Diagnostics",
    }
    optional_only = {"Pydantic", "Arrow, Parquet, and datasets"}
    for row, entries in MATRIX_TESTS.items():
        assert entries, row
        if row not in optional_only:
            assert any(dependency is None for _, dependency in entries), (
                f"{row} needs a dependency-free anchor test"
            )
    for group, entries in BOUNDARY_TESTS.items():
        assert entries, group


def test_release_matrix_node_ids_resolve(collected_node_ids: set[str]) -> None:
    for row, entries in MATRIX_TESTS.items():
        for node_id, dependency in entries:
            if _dependency_available(dependency):
                _assert_resolves(node_id, collected_node_ids)
            else:
                assert (ROOT / node_id.split("::", 1)[0]).is_file(), (row, node_id)
    for group, entries in BOUNDARY_TESTS.items():
        for node_id, dependency in entries:
            if _dependency_available(dependency):
                _assert_resolves(node_id, collected_node_ids)
            else:
                assert (ROOT / node_id.split("::", 1)[0]).is_file(), (group, node_id)


def test_optional_release_rows_run_when_their_extras_are_installed(
    collected_node_ids: set[str],
) -> None:
    missing = {name for name in _OPTIONAL_MODULES if not _dependency_available(name)}
    if missing:
        pytest.skip(f"optional adapters not installed: {sorted(missing)}")
    for mapping in (MATRIX_TESTS, BOUNDARY_TESTS):
        for entries in mapping.values():
            for node_id, dependency in entries:
                if dependency is not None:
                    _assert_resolves(node_id, collected_node_ids)


# ---------------------------------------------------------------------------
# Pinned catalog and disposition audit
# ---------------------------------------------------------------------------


def _catalog_cases():  # exact tuple type comes from report_relaxng
    return report_relaxng.load_cases()


def test_vendored_catalog_checksums_and_provenance_are_pinned() -> None:
    provenance = (CORPUS / "PROVENANCE.md").read_text(encoding="utf-8")
    assert report_relaxng.CATALOG_COMMIT in provenance
    assert report_relaxng.CATALOG_COMMIT == "263a35a5e3ebbfccf8eb9752077e59cde9acc9a1"

    lines = [
        line.split()
        for line in (CORPUS / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {name for _digest, name in lines} == {"spectest.xml", "xsdtest.xml", "LICENSE"}
    for digest, name in lines:
        assert hashlib.sha256((CORPUS / name).read_bytes()).hexdigest() == digest


def test_pinned_catalog_counts_and_profiles_are_exact() -> None:
    cases = _catalog_cases()
    schemas = report_relaxng.schema_cases(cases)
    instances = report_relaxng.instance_cases(cases)

    assert len(schemas) == 623
    assert len(instances) == 1621
    assert len(cases) == 623 + 1621
    assert {case.profile for case in cases} == {PROFILE_CORE, PROFILE_XSD_DATATYPE}
    assert {case.profile for case in cases} <= REQUIRED_PROFILES
    assert not any(case.is_excluded for case in cases)

    assert sum(case.profile == PROFILE_CORE for case in schemas) == 376
    assert sum(case.profile == PROFILE_XSD_DATATYPE for case in schemas) == 247
    assert sum(case.profile == PROFILE_CORE for case in instances) == 538
    assert sum(case.profile == PROFILE_XSD_DATATYPE for case in instances) == 1083
    assert sum(case.kind == "valid-instance" for case in instances) == 593
    assert sum(case.kind == "invalid-instance" for case in instances) == 1028


def test_every_case_has_exactly_one_audited_disposition() -> None:
    cases = _catalog_cases()
    by_id = {case.id: case for case in cases}
    dispositions = load_dispositions(DISPOSITIONS)

    validate_dispositions(dispositions, by_id)
    assignments = report_relaxng.case_dispositions(cases, dispositions)

    assert set(assignments) == set(by_id)
    assert set(assignments.values()) == {PROFILE_CORE, PROFILE_XSD_DATATYPE}
    assert report_relaxng.excluded_dependencies(cases, dispositions) == ()
    assert dispositions == ()


def test_baseline_outcome_vocabulary_has_no_operational_acceptance_value() -> None:
    assert {outcome.value for outcome in Outcome} == {
        "pass",
        "fail",
        "adapter-gap",
        "profile-excluded",
    }


def test_checked_in_baseline_is_a_complete_nonweakened_pass_record() -> None:
    cases = _catalog_cases()
    by_id = {case.id: case for case in cases}
    schemas = report_relaxng.required_schema_cases(cases)
    instances = report_relaxng.required_instance_cases(cases)
    dispositions = load_dispositions(DISPOSITIONS)
    baseline = load_baseline(BASELINE)

    assert baseline.meta == {
        "catalog": report_relaxng.CATALOG_COMMIT,
        "instance_cases": 1621,
        "profile": "core+xsd-datatype",
        "schema_cases": 623,
    }
    assert set(baseline.results) == {case.id for case in schemas}
    assert set(baseline.instances) == {case.id for case in instances}
    assert set(baseline.results.values()) == {"pass"}
    assert set(baseline.instances.values()) == {"pass"}
    assert release_violations(by_id, baseline, dispositions) == ()


@pytest.mark.parametrize("outcome", ["fail", "adapter-gap", "profile-excluded"])
def test_no_nonpass_outcome_is_an_acceptable_rejection(outcome: str) -> None:
    cases = _catalog_cases()
    by_id = {case.id: case for case in cases}
    schemas = report_relaxng.required_schema_cases(cases)
    instances = report_relaxng.required_instance_cases(cases)
    dispositions = load_dispositions(DISPOSITIONS)
    baseline = load_baseline(BASELINE)

    schema_case = schemas[0]
    instance_case = instances[0]
    weakened_schema = Baseline(
        meta=baseline.meta,
        results={**baseline.results, schema_case.id: outcome},
        instances=dict(baseline.instances),
    )
    weakened_instance = Baseline(
        meta=baseline.meta,
        results=dict(baseline.results),
        instances={**baseline.instances, instance_case.id: outcome},
    )

    schema_violations = release_violations(by_id, weakened_schema, dispositions)
    instance_violations = release_violations(by_id, weakened_instance, dispositions)
    assert [violation.case_id for violation in schema_violations] == [schema_case.id]
    assert [violation.case_id for violation in instance_violations] == [instance_case.id]
    assert schema_violations[0].outcome == outcome
    assert instance_violations[0].outcome == outcome


def test_baseline_update_refuses_to_remove_or_downgrade_a_pass(tmp_path: Path) -> None:
    cases = _catalog_cases()
    schemas = report_relaxng.required_schema_cases(cases)
    instances = report_relaxng.required_instance_cases(cases)
    path = tmp_path / "baseline.toml"
    save_baseline(
        path,
        {schemas[0].id: "pass"},
        {"profile": "core+xsd-datatype"},
        instances={instances[0].id: "pass"},
    )

    with pytest.raises(BaselineError, match="refusing to remove"):
        report_relaxng.save_baseline_checked(
            path,
            {schemas[0].id: "fail"},
            {"profile": "core+xsd-datatype"},
            instances={instances[0].id: "pass"},
        )
    with pytest.raises(BaselineError, match="refusing to remove"):
        report_relaxng.save_baseline_checked(
            path,
            {schemas[0].id: "pass"},
            {"profile": "core+xsd-datatype"},
            instances={instances[0].id: "adapter-gap"},
        )
    with pytest.raises(BaselineError, match="refusing to remove"):
        report_relaxng.save_baseline_checked(
            path,
            {},
            {"profile": "core+xsd-datatype"},
            instances={instances[0].id: "pass"},
        )


# ---------------------------------------------------------------------------
# Projection stability and unsupported-result wording
# ---------------------------------------------------------------------------

_STABLE_SCHEMA = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="record">
      <attribute name="id">
        <data type="integer" datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
      </attribute>
      <element name="title"><text/></element>
      <zeroOrMore><element name="tag"><text/></element></zeroOrMore>
    </element>
  </start>
</grammar>"""

_GENERIC_DESCENDANT_SCHEMA = """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start>
    <element name="record">
      <element name="title"><text/></element>
      <element name="payload">
        <choice>
          <data type="integer" datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
          <element name="item"><text/></element>
        </choice>
      </element>
    </element>
  </start>
</grammar>"""


def test_stable_routes_never_use_a_generic_fallback() -> None:
    from pyxsd import relaxng

    schema = relaxng.compile(StringIO(_STABLE_SCHEMA))
    plan = schema.projection
    assert plan.routes
    for route in plan.routes.values():
        assert route.generic_reason is None, route.route_id
        assert schema.projection_classes.class_for(route.route_id) is not RelaxNGElement

    document = schema.parse(StringIO('<record id="7"><title>Rain</title><tag>x</tag></record>'))
    assert type(document.root) is not RelaxNGElement
    assert type(document.root).__module__.startswith("pyxsd.relaxng.generated.")


def test_generic_fallback_is_route_local_and_has_a_semantic_reason() -> None:
    from pyxsd import relaxng

    schema = relaxng.compile(StringIO(_GENERIC_DESCENDANT_SCHEMA))
    plan = schema.projection
    generic = [route for route in plan.routes.values() if route.generic_reason is not None]
    stable = [route for route in plan.routes.values() if route.generic_reason is None]

    assert stable, "the stable parent and sibling routes must remain projected"
    assert generic, "the scalar-versus-child payload route has no stable whole-element contract"
    for route in generic:
        reason = route.generic_reason or ""
        assert reason.strip()
        assert re.search(
            r"scalar|child|mixed|wildcard|ambiguous|order|representation|descriptor",
            reason,
            re.IGNORECASE,
        ), reason
        for forbidden in ("not implemented", "unimplemented", "deferred", "todo"):
            assert forbidden not in reason.lower(), reason
        assert schema.projection_classes.class_for(route.route_id) is RelaxNGElement
    for route in stable:
        assert schema.projection_classes.class_for(route.route_id) is not RelaxNGElement

    document = schema.parse(StringIO("<record><title>Rain</title><payload>12</payload></record>"))
    assert type(document.root).__module__.startswith("pyxsd.relaxng.generated.")
    assert type(document.root.payload) is RelaxNGElement


def test_user_facing_unsupported_results_do_not_say_not_implemented() -> None:
    paths = [
        *sorted(RELAXNG.glob("*.py")),
        *sorted((ROOT / "src" / "pyxsd" / "mcp").rglob("*.py")),
        ROOT / "src" / "pyxsd" / "cli.py",
        ROOT / "src" / "pyxsd" / "transforms" / "to_data.py",
        ROOT / "docs" / "relax-ng.md",
        ROOT / "src" / "pyxsd" / "skills" / "pyxsd-cli" / "SKILL.md",
        ROOT / "src" / "pyxsd" / "skills" / "pyxsd-mcp" / "SKILL.md",
    ]
    offenders = []
    for path in paths:
        lowered = path.read_text(encoding="utf-8").lower()
        for phrase in ("not implemented", "unimplemented"):
            if phrase in lowered:
                offenders.append((str(path.relative_to(ROOT)), phrase))
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# Adversarial resource measurements
# ---------------------------------------------------------------------------

_ADVERSARIAL_FIXTURES = {
    "deterministic": (
        _STABLE_SCHEMA,
        '<record id="7"><title>Rain</title><tag>x</tag><tag>y</tag></record>',
    ),
    "ambiguous": (
        """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start><element name="value"><choice>
    <data type="integer" datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
    <data type="string" datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
  </choice></element></start>
</grammar>""",
        "<value>12</value>",
    ),
    "recursive": (
        """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start><ref name="node"/></start>
  <define name="node">
    <element name="node">
      <optional><attribute name="value"><data type="integer"
        datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/></attribute></optional>
      <zeroOrMore><ref name="node"/></zeroOrMore>
    </element>
  </define>
</grammar>""",
        '<node value="0"><node value="1"><node value="2"><node value="3"/></node></node></node>',
    ),
    "interleaved": (
        """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start><element name="r"><interleave>
    <element name="x"><data type="integer"
      datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/></element>
    <element name="y"><data type="string"
      datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/></element>
  </interleave></element></start>
</grammar>""",
        "<r><y>word</y><x>12</x></r>",
    ),
    "hostile": (
        """<grammar xmlns="http://relaxng.org/ns/structure/1.0">
  <start><element name="scores"><list><oneOrMore><choice>
    <data type="integer" datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
    <data type="string" datatypeLibrary="http://www.w3.org/2001/XMLSchema-datatypes"/>
  </choice></oneOrMore></list></element></start>
</grammar>""",
        "<scores>" + " ".join(str(index) for index in range(16)) + "</scores>",
    ),
}

_MEASUREMENTS: list[dict[str, Any]] = []


def _evidence_metrics(document: Any) -> dict[str, int]:
    evidence = document.binding_evidence
    assert evidence is not None
    return {
        "states": len(evidence.retained_nodes),
        "edges": len(evidence.retained_edges),
        "witnesses": sum(len(edge.witnesses) for edge in evidence.retained_edges),
        "summaries": len(evidence._schema_patterns),
        "occurrences": len(evidence.occurrences),
    }


def test_adversarial_fixtures_measure_capture_off_and_on_without_an_sla() -> None:
    from pyxsd import relaxng

    compiled = {
        name: relaxng.compile(StringIO(schema))
        for name, (schema, _xml) in _ADVERSARIAL_FIXTURES.items()
    }
    by_fixture: dict[str, dict[str, dict[str, int]]] = {}

    for name, (_schema, xml) in _ADVERSARIAL_FIXTURES.items():
        by_fixture[name] = {}
        for capture in ("none", "patterns"):
            schema = compiled[name]
            gc.collect()
            start = time.perf_counter()
            tracemalloc.start()
            document = schema.parse(StringIO(xml), typed=True, capture=capture)
            _current, peak = tracemalloc.get_traced_memory()
            elapsed = time.perf_counter() - start
            tracemalloc.stop()

            assert document.is_valid, (name, [issue.code for issue in document.report.issues])
            assert document.typed is True
            metrics = _evidence_metrics(document)
            assert all(value >= 0 for value in metrics.values())
            assert metrics["states"] >= 1
            assert elapsed >= 0.0
            assert peak >= 0
            by_fixture[name][capture] = metrics
            _MEASUREMENTS.append(
                {
                    "fixture": name,
                    "capture": capture,
                    **metrics,
                    "seconds": round(elapsed, 6),
                    "peak_bytes": peak,
                }
            )

        assert by_fixture[name]["none"] == by_fixture[name]["patterns"]


def test_adversarial_measurements_are_recorded() -> None:
    assert {entry["fixture"] for entry in _MEASUREMENTS} == set(_ADVERSARIAL_FIXTURES)
    assert {entry["capture"] for entry in _MEASUREMENTS} == {"none", "patterns"}
    assert len(_MEASUREMENTS) == 2 * len(_ADVERSARIAL_FIXTURES)

    rows = [
        "| fixture | capture | states | edges | witnesses | summaries | occurrences "
        "| seconds | peak bytes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in _MEASUREMENTS:
        rows.append(
            "| {fixture} | {capture} | {states} | {edges} | {witnesses} | {summaries} "
            "| {occurrences} | {seconds} | {peak_bytes} |".format(**entry)
        )
    assert len(rows) == 2 + len(_MEASUREMENTS)
    print("\n".join(rows))


def test_concurrent_parse_uses_a_barrier_and_aborts_on_worker_failure() -> None:
    from pyxsd import relaxng

    schema_one = relaxng.compile(StringIO(_STABLE_SCHEMA))
    schema_two = relaxng.compile(StringIO(_STABLE_SCHEMA))
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}
    failures: list[BaseException] = []

    def worker(name: str, schema: Any) -> None:
        try:
            barrier.wait(timeout=10)
            results[name] = schema.parse(
                StringIO('<record id="1"><title>Rain</title></record>'), typed=False
            )
        except BaseException as exc:  # abort the sibling instead of hanging it
            failures.append(exc)
            barrier.abort()
            raise

    threads = [
        threading.Thread(target=worker, args=("one", schema_one)),
        threading.Thread(target=worker, args=("two", schema_two)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert set(results) == {"one", "two"}
    assert all(document.is_valid for document in results.values())


# ---------------------------------------------------------------------------
# Schema-cache isolation
# ---------------------------------------------------------------------------


def test_repeated_parses_do_not_accumulate_instance_evidence_in_schema_caches() -> None:
    from pyxsd import relaxng

    schema = relaxng.compile(StringIO(_STABLE_SCHEMA))
    xml = '<record id="7"><title>Rain</title><tag>x</tag></record>'
    warm = schema.parse(StringIO(xml), typed=True, capture="patterns")
    assert warm.annotations.complete is True
    del warm
    gc.collect()

    keys = frozenset(vars(schema))
    identities = {name: id(value) for name, value in vars(schema).items()}

    references = []
    for _ in range(6):
        document = schema.parse(StringIO(xml), typed=True, capture="patterns")
        assert document.binding_evidence is not None
        references.append(
            (
                weakref.ref(document),
                weakref.ref(document.binding_evidence),
                weakref.ref(document.root),
                weakref.ref(document.annotations),
            )
        )
        del document

    gc.collect()

    assert all(reference() is None for group in references for reference in group)
    assert frozenset(vars(schema)) == keys
    assert {name: id(value) for name, value in vars(schema).items()} == identities


# ---------------------------------------------------------------------------
# CI and coverage configuration
# ---------------------------------------------------------------------------


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    for step in job["steps"]:
        if step.get("name") == name:
            return step
    pytest.fail(f"workflow job has no step named {name!r}")


def _run_text(job: dict[str, Any], name: str) -> str:
    return _step(job, name)["run"]


def test_installed_adapter_job_runs_every_relaxng_optional_suite() -> None:
    job = _workflow("optional-integrations.yml")["jobs"]["adapters"]
    install = _run_text(job, "Install both extras at minimum or current versions")
    assert "jsonschema" in install
    run = _run_text(job, "Test installed adapters and separate branch coverage")
    for name in (
        "tests/test_relaxng_pydantic_models.py",
        "tests/test_relaxng_pydantic_scalars.py",
        "tests/test_relaxng_pydantic_standalone.py",
        "tests/test_relaxng_pydantic_documents.py",
        "tests/test_relaxng_arrow_records.py",
        "tests/test_relaxng_record_projection.py",
        "tests/test_relaxng_datasets.py",
        "tests/test_relaxng_integration_parity.py",
        "tests/test_relaxng_docs.py",
    ):
        assert name in run, name
    for name in (
        "tests/test_pydantic_integration.py",
        "tests/test_arrow_integration.py",
        "tests/test_integration_projection.py",
    ):
        assert name in run, name
    assert "--cov=pyxsd.integrations" in run
    assert "--cov=pyxsd.relaxng.pydantic_adapter" in run
    assert "--cov=pyxsd.relaxng.pydantic_values" in run
    assert "--cov=pyxsd.relaxng.records" in run
    assert "--cov-config=tests/integration.coveragerc" in run
    assert "--cov-branch" in run
    assert "--cov-report=xml" in run

    independent = _workflow("optional-integrations.yml")["jobs"]["independent-installs"]
    install = _run_text(independent, "Install wheel into a clean independent environment")
    assert "jsonschema" in install
    run = _run_text(independent, "Verify base and independently installed extras")
    for name in (
        "tests/test_relaxng_pydantic_models.py",
        "tests/test_relaxng_pydantic_scalars.py",
        "tests/test_relaxng_pydantic_standalone.py",
        "tests/test_relaxng_pydantic_documents.py",
        "tests/test_relaxng_arrow_records.py",
        "tests/test_relaxng_record_projection.py",
        "tests/test_relaxng_datasets.py",
        "tests/test_relaxng_integration_parity.py",
        "tests/test_relaxng_docs.py",
    ):
        assert name in run, name


def test_integration_coverage_config_measures_relaxng_optional_modules() -> None:
    parser = configparser.ConfigParser()
    parser.read(ROOT / "tests" / "integration.coveragerc", encoding="utf-8")
    sources = {line.strip() for line in parser["run"]["source"].splitlines() if line.strip()}
    assert sources == {
        "pyxsd.integrations",
        "pyxsd.relaxng.pydantic_adapter",
        "pyxsd.relaxng.pydantic_values",
        "pyxsd.relaxng.records",
    }
    assert parser["run"].getboolean("branch") is True
    assert parser["report"].getfloat("fail_under") == 92.0


def test_base_coverage_omits_only_the_optional_relaxng_modules() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    run = project["tool"]["coverage"]["run"]
    omitted = set(run["omit"])
    assert omitted == {
        "*/pyxsd/integrations/pydantic.py",
        "*/pyxsd/integrations/arrow.py",
        "*/pyxsd/integrations/_dataset.py",
        "*/pyxsd/relaxng/pydantic_adapter.py",
        "*/pyxsd/relaxng/pydantic_values.py",
        "*/pyxsd/relaxng/records.py",
        "*/pyxsd/mcp/*",
    }
    assert project["tool"]["coverage"]["report"]["fail_under"] == 99.0
    assert run["branch"] is True


def test_ci_matrices_execute_every_supported_python_version_and_split_suites() -> None:
    supported = ["3.11", "3.12", "3.13", "3.14", "3.15"]

    ci = _workflow("ci.yml")["jobs"]
    test_job = ci["test"]
    assert test_job["strategy"]["matrix"]["python-version"] == supported
    coverage_step = _step(test_job, "Run tests with coverage")
    run = coverage_step["run"]
    assert "--cov" in run and "--cov-branch" in run
    assert "--ignore=tests/mcp" in run
    assert coverage_step.get("env", {}).get("PYXSD_RUN_RELAXNG") == "1"
    assert "Run the XSD Test Suite" in {step.get("name") for step in test_job["steps"]}

    mcp_job = _workflow("mcp.yml")["jobs"]["mcp"]
    assert mcp_job["strategy"]["matrix"]["python"] == supported
    assert mcp_job["strategy"]["matrix"]["dependencies"] == ["minimum", "current"]
    mcp_run = _run_text(mcp_job, "Test installed MCP runtime (MCP-only, no Arrow)")
    assert "tests/mcp" in mcp_run
    assert "tests/test_mcp_packaging.py" in mcp_run

    adapters = _workflow("optional-integrations.yml")["jobs"]["adapters"]
    assert adapters["strategy"]["matrix"]["python"] == supported
    assert adapters["strategy"]["matrix"]["dependencies"] == ["minimum", "current"]

    docs_run = _run_text(_workflow("docs.yml")["jobs"]["build"], "Build documentation")
    assert "sphinx-build -W" in docs_run


def test_platform_evidence_records_linux_and_macos_without_an_unverified_windows_job() -> None:
    runners: set[str] = set()
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in workflow.get("jobs", {}).values():
            value = job.get("runs-on")
            if isinstance(value, str):
                runners.add(value)

    # Current verified evidence is Ubuntu for the full matrices and macOS for
    # the installed stdio job.  No Windows runner is claimed in this branch;
    # adding one requires separately verified CI evidence.
    assert "ubuntu-latest" in runners
    assert "macos-latest" in runners
    assert not any("windows" in runner.lower() for runner in runners)
