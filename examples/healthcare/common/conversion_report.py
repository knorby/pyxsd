"""Mapping limitations are separate from XSD validation findings."""

from dataclasses import asdict, dataclass, field


@dataclass
class ConversionReport:
    source: str
    source_counts: dict[str, int]
    resources: dict[str, int] = field(default_factory=dict)
    mapped_sections: list[str] = field(default_factory=list)
    mapped_entries: list[str] = field(default_factory=list)
    unsupported_sections: list[dict[str, str | None]] = field(default_factory=list)
    unsupported_entries: list[dict[str, str]] = field(default_factory=list)
    notes: list[dict[str, str]] = field(default_factory=list)
    source_findings: list[dict] = field(default_factory=list)
    output_counts: dict[str, int] = field(default_factory=dict)

    @property
    def unsupported_section_count(self) -> int:
        return len(self.unsupported_sections)

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "unsupported_section_count": self.unsupported_section_count}
