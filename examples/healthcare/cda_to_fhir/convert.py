"""Convert the supported CDA slice and freshly validate its FHIR R4 XML."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from pyxsd import Document, Schema, XmlParserPolicy
from pyxsd.exceptions import PyXSDError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from examples.healthcare.cda_to_fhir.fhir_build import Builder
from examples.healthcare.cda_to_fhir.mapping import (
    ConversionError,
    map_sections,
    patient_resource,
    required,
    timestamp,
)
from examples.healthcare.common.cda_datatypes import CDA, child, children, text
from examples.healthcare.common.conversion_report import ConversionReport
from examples.healthcare.common.schemas import compile_cda, compile_fhir


@dataclass(frozen=True)
class ConversionResult:
    bundle: Document
    report: ConversionReport


def source_issue_key(issue) -> str:
    path = (
        "/".join(f"{step.name}[{step.index}]" for step in issue.location.path)
        if issue.location
        else "<unknown>"
    )
    return f"{issue.code}:/{path}"


def convert_document(
    cda: Document, *, fhir: Schema, allowed_source_issues: Collection[str] = ()
) -> ConversionResult:
    if cda.root is None or cda.root._name_ != f"{{{CDA}}}ClinicalDocument":
        raise ConversionError("Unknown or unbound CDA root")
    for issue in cda.report.errors:
        if (
            issue.code not in ("order", "unexpected-element")
            or source_issue_key(issue) not in allowed_source_issues
        ):
            raise ConversionError(f"Unclassified source finding: {source_issue_key(issue)}")
    report = ConversionReport(
        str(cda.source) if isinstance(cda.source, (str, Path)) else "<memory>",
        {"error": cda.report.error_count, "warning": cda.report.warning_count},
    )
    report.source_findings = cda.report.to_dict()["issues"]
    doc_id = required(cda.root, "id").as_identifier()
    if not doc_id.root or not doc_id.value or doc_id.null_flavor or not doc_id.system:
        raise ConversionError("Missing required document identifier")
    builder = Builder(fhir, doc_id.root, doc_id.extension or "")
    date = timestamp(
        child(cda.root, "effectiveTime"), report, "ClinicalDocument/effectiveTime", instant=True
    )
    targets = children(cda.root, "recordTarget")
    if len(targets) != 1:
        raise ConversionError("Exactly one recordTarget is required")
    patient = patient_resource(builder, required(targets[0], "patientRole"), report)
    resources, sections = map_sections(builder, cda, report)
    title = text(required(cda.root, "title")).strip()
    document_code = required(cda.root, "code").as_coding()
    if not title or not document_code.known_system or not document_code.code:
        raise ConversionError("Missing supported document title/code")
    author = required(required(cda.root, "author"), "assignedAuthor")
    author_id = required(author, "id").as_identifier()
    author_name = text(child(child(author, "assignedPerson"), "name")).strip()
    if not author_id.value or not author_id.system or author_id.null_flavor or not author_name:
        raise ConversionError("Missing usable document author")
    composition = builder.resource(
        "Composition",
        "composition",
        builder.primitive("status", "final", "CompositionStatus"),
        builder.concept("type", document_code.system, document_code.code, document_code.display),
        builder.reference("subject", "recordTarget[1]"),
        builder.primitive("date", date, "dateTime"),
        builder.node(
            "Reference",
            "author",
            builder.identifier(author_id),
            builder.primitive("display", " ".join(author_name.split())),
        ),
        builder.primitive("title", title),
        *sections,
    )
    all_resources = [("composition", composition), ("recordTarget[1]", patient), *resources]
    bundle = builder.node(
        "Bundle",
        "Bundle",
        builder.identifier(doc_id),
        builder.primitive("type", "document", "BundleType"),
        builder.primitive("timestamp", date, "instant"),
        *(builder.entry(key, node) for key, node in all_resources),
    )
    verified = builder.validate(bundle)
    if verified.report.error_count:
        raise ConversionError(f"Generated FHIR failed fresh validation: {verified.report}")
    report.resources = dict(Counter(node._name_.split("}")[-1] for _, node in all_resources))
    report.output_counts = {
        "error": verified.report.error_count,
        "warning": verified.report.warning_count,
    }
    return ConversionResult(verified, report)


def main(argv=None) -> int:
    from examples.healthcare.common.sources import resolve_document

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "documents",
        nargs="*",
        default=["discharge"],
        help="local XML paths, named examples, or repo:relative/path",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-source-issue", action="append", default=[])
    args = parser.parse_args(argv)
    if len({Path(value).stem for value in args.documents}) != len(args.documents):
        parser.error("input filenames must have distinct stems")
    results = []
    try:
        paths = [resolve_document(value) for value in args.documents]
        if len({path.stem for path in paths}) != len(paths):
            parser.error("input filenames must have distinct stems")
        cda, fhir = compile_cda(), compile_fhir()
        for path in paths:
            document = cda.parse(path, xml_policy=XmlParserPolicy.protected())
            results.append(
                (
                    path.stem,
                    convert_document(
                        document, fhir=fhir, allowed_source_issues=args.allow_source_issue
                    ),
                )
            )
    except (ValueError, PyXSDError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for stem, result in results:
        (args.output_dir / f"{stem}.fhir.xml").write_text(
            result.bundle.to_string(), encoding="utf-8"
        )
        (args.output_dir / f"{stem}.report.json").write_text(
            json.dumps(result.report.to_dict(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
