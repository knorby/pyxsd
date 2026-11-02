"""A deliberately small clinical mapping. Unsupported values are reported."""

from examples.healthcare.common.cda_datatypes import attr, child, children, text


class ConversionError(ValueError):
    """Input cannot safely produce the requested example output."""


def required(node, name):
    found = child(node, name)
    if found is None:
        raise ConversionError(f"Missing required CDA element: {name}")
    return found


def timestamp(node, report, path, *, instant=False):
    if node is None:
        raise ConversionError(f"Missing required timestamp: {path}")
    value = node.as_datetime()
    if value.lexical is None or (
        instant and (value.timezone is None or value.precision in ("year", "month", "day", "hour"))
    ):
        raise ConversionError(f"Insufficient timestamp for {path}")
    if value.precision in ("minute", "hour"):
        report.notes.append(
            {
                "path": path,
                "reason": f"source precision: {value.precision}; lexical zero padding only",
            }
        )
    return value.lexical


def patient_resource(builder, role, report):
    patient = required(role, "patient")
    fields = []
    for node in children(role, "id"):
        value = node.as_identifier()
        if value.value and value.system and not value.null_flavor:
            fields.append(builder.identifier(value))
        else:
            report.notes.append(
                {
                    "path": "patientRole/id",
                    "reason": f"unsupported identifier nullFlavor={value.null_flavor}",
                }
            )
    if not fields:
        raise ConversionError("Patient requires at least one usable identifier")
    for name in children(patient, "name"):
        parts = []
        family = text(child(name, "family")).strip()
        if family:
            parts.append(builder.primitive("family", family))
        parts.extend(
            builder.primitive("given", text(given).strip())
            for given in children(name, "given")
            if text(given).strip()
        )
        if parts:
            fields.append(builder.node("HumanName", "name", *parts))
    gender = attr(child(patient, "administrativeGenderCode"), "code")
    if gender in ("F", "M", "UN"):
        fields.append(
            builder.primitive(
                "gender",
                {"F": "female", "M": "male", "UN": "other"}[gender],
                "AdministrativeGender",
            )
        )
    elif gender:
        report.notes.append({"path": "patient/gender", "reason": f"unsupported gender {gender}"})
    birth = child(patient, "birthTime")
    if birth is not None:
        value = birth.as_datetime()
        if value.lexical and value.precision in ("year", "month", "day"):
            fields.append(builder.primitive("birthDate", value.lexical, "date"))
        else:
            report.notes.append(
                {
                    "path": "patient/birthTime",
                    "reason": f"unsupported birthTime nullFlavor={value.null_flavor}",
                }
            )
    return builder.resource("Patient", "recordTarget[1]", *fields)


def supported_coding(node, report, path):
    if node is None or not hasattr(node, "as_coding"):
        report.unsupported_entries.append(
            {"path": path, "reason": "missing/unsupported coding datatype"}
        )
        return None
    value = node.as_coding()
    if not value.known_system or not value.code or value.null_flavor:
        report.unsupported_entries.append(
            {
                "path": path,
                "reason": f"unsupported code system={value.system} nullFlavor={value.null_flavor}",
            }
        )
        return None
    if value.translations:
        report.notes.append({"path": path, "reason": "coding translations not mapped"})
    return value


def map_sections(builder, document, report):
    resources, sections = [], []
    body = child(child(document.root, "component"), "structuredBody")
    for index, component in enumerate(children(body, "component"), 1):
        section = child(component, "section")
        code_node = child(section, "code")
        code = attr(code_node, "code")
        path = f"section[{index}]"
        if (
            code not in ("11450-4", "8716-3")
            or attr(code_node, "codeSystem") != "2.16.840.1.113883.6.1"
        ):
            report.unsupported_sections.append(
                {"path": path, "code": code, "title": text(child(section, "title")).strip()}
            )
            continue
        references = []
        for number, entry in enumerate(children(section, "entry"), 1):
            occurrence = f"{path}/entry[{number}]"
            mapped = (
                map_problem(builder, entry, occurrence, report)
                if code == "11450-4"
                else map_vitals(builder, entry, occurrence, report)
            )
            resources.extend(mapped)
            if mapped:
                references.append(builder.reference("entry", mapped[0][0]))
                report.mapped_entries.append(occurrence)
        if references:
            sections.append(
                builder.node(
                    "Composition.Section",
                    "section",
                    builder.primitive("title", text(child(section, "title")).strip() or code),
                    builder.concept("code", "http://loinc.org", code),
                    *references,
                )
            )
            report.mapped_sections.append(path)
    return resources, sections


def map_problem(builder, entry, path, report):
    act = child(entry, "act")
    relations = children(act, "entryRelationship")
    observation = child(relations[0], "observation") if len(relations) == 1 else None
    if (
        observation is None
        or attr(act, "negationInd") in ("true", "1")
        or attr(act, "moodCode") != "EVN"
        or attr(relations[0], "typeCode") != "SUBJ"
        or attr(relations[0], "inversionInd") in ("true", "1")
        or attr(observation, "negationInd") in ("true", "1")
        or attr(child(act, "statusCode"), "code") != "active"
        or attr(observation, "moodCode") != "EVN"
    ):
        report.unsupported_entries.append(
            {"path": path, "reason": "unsupported problem structure/status/negation/mood"}
        )
        return []
    coding = supported_coding(child(observation, "value"), report, path)
    if coding is None:
        return []
    resource = builder.resource(
        "Condition",
        path,
        builder.concept(
            "clinicalStatus", "http://terminology.hl7.org/CodeSystem/condition-clinical", "active"
        ),
        builder.concept("code", coding.system, coding.code, coding.display),
        builder.reference("subject", "recordTarget[1]"),
    )
    return [(path, resource)]


def map_vitals(builder, entry, path, report):
    organizer = child(entry, "organizer")
    if attr(child(organizer, "statusCode"), "code") != "completed":
        report.unsupported_entries.append({"path": path, "reason": "unsupported panel status"})
        return []
    panel_code = supported_coding(child(organizer, "code"), report, path)
    if panel_code is None:
        return []
    resources = []
    for index, component in enumerate(children(organizer, "component"), 1):
        occurrence = f"{path}/component[{index}]"
        observation = child(component, "observation")
        coding = supported_coding(child(observation, "code"), report, occurrence)
        value = child(observation, "value")
        if coding is None:
            continue
        if value is None or not hasattr(value, "as_quantity"):
            report.unsupported_entries.append(
                {"path": occurrence, "reason": "unsupported quantity datatype"}
            )
            continue
        quantity = value.as_quantity()
        if (
            quantity.value is None
            or not quantity.value.is_finite()
            or not quantity.unit
            or attr(child(observation, "statusCode"), "code") != "completed"
            or attr(observation, "negationInd") in ("true", "1")
            or attr(observation, "moodCode") != "EVN"
        ):
            report.unsupported_entries.append(
                {
                    "path": occurrence,
                    "reason": f"unsupported measurement/status/mood nullFlavor={quantity.null_flavor}",
                }
            )
            continue
        fields = observation_fields(builder, coding, observation, report, occurrence)
        fields.append(
            builder.node(
                "Quantity",
                "valueQuantity",
                builder.primitive("value", str(quantity.value), "decimal"),
                builder.primitive("unit", quantity.unit),
                builder.primitive("system", "http://unitsofmeasure.org", "uri"),
                builder.primitive("code", quantity.unit, "code"),
            )
        )
        resources.append((occurrence, builder.resource("Observation", occurrence, *fields)))
    if not resources:
        return []
    panel = builder.resource(
        "Observation",
        path,
        *observation_fields(builder, panel_code, organizer, report, path),
        *(builder.reference("hasMember", key) for key, _ in resources),
    )
    return [(path, panel), *resources]


def observation_fields(builder, coding, source, report, path):
    return [
        builder.primitive("status", "final", "ObservationStatus"),
        builder.concept(
            "category", "http://terminology.hl7.org/CodeSystem/observation-category", "vital-signs"
        ),
        builder.concept("code", coding.system, coding.code, coding.display),
        builder.reference("subject", "recordTarget[1]"),
        builder.primitive(
            "effectiveDateTime",
            timestamp(child(source, "effectiveTime"), report, path, instant=True),
            "dateTime",
        ),
    ]
