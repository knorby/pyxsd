"""Opt-in XML parser policy values and shared declaration enforcement.

The default values keep core parsing on :mod:`xml.etree.ElementTree` with
today's DTD and entity behavior. ``protected()`` and ``strict()`` opt in to
defusedxml for callers that accept its parser overhead; no profile retrieves
external DTDs or entities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyxsd.resource_policy import ResourcePolicyError


@dataclass(frozen=True)
class XmlLimits:
    """Optional structural budgets for one XML parse.

    Every bound is inclusive and counted on the input as supplied:
    ``max_bytes`` on stream/file bytes (characters for text-mode
    streams), ``max_depth`` on element nesting (the root is depth 1),
    and ``max_elements`` on start tags. ``None`` leaves that budget
    unset.
    """

    max_bytes: int | None = None
    max_depth: int | None = None
    max_elements: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("max_bytes", self.max_bytes),
            ("max_depth", self.max_depth),
            ("max_elements", self.max_elements),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or None")


@dataclass(frozen=True)
class XmlParserPolicy:
    """An immutable, opt-in XML parser posture.

    The forbid flags are meaningful only with ``use_defusedxml=True``;
    constructing a policy that asks for declaration denial without the
    defusedxml parser would silently ignore the request, so it is rejected.
    """

    use_defusedxml: bool = False
    forbid_dtd: bool = False
    forbid_entities: bool = False
    forbid_external: bool = False

    def __post_init__(self) -> None:
        if not self.use_defusedxml and (
            self.forbid_dtd or self.forbid_entities or self.forbid_external
        ):
            raise ValueError(
                "forbid_dtd, forbid_entities, and forbid_external require use_defusedxml=True"
            )

    @classmethod
    def default(cls) -> XmlParserPolicy:
        """Stdlib ElementTree with today's DTD/entity behavior and no overhead."""
        return cls()

    @classmethod
    def protected(cls) -> XmlParserPolicy:
        """Defusedxml; forbid entity declarations and external references.

        Plain DTD syntax stays allowed, so harmless internal ``ATTLIST``
        defaults still apply.
        """
        return cls(use_defusedxml=True, forbid_entities=True, forbid_external=True)

    @classmethod
    def strict(cls) -> XmlParserPolicy:
        """Defusedxml; forbid DTDs, entity declarations, and external references."""
        return cls(
            use_defusedxml=True,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )


def configure_expat_security(parser: Any, policy: XmlParserPolicy) -> None:
    """Install ``policy``'s declaration denials on an Expat parser.

    Accepts a raw :mod:`xml.parsers.expat` parser or a defusedxml
    ``DefusedXMLParser`` (whose underlying Expat parser is ``.parser``).
    The installed handlers raise :class:`ResourcePolicyError` before any
    declaration is acted on; they never retrieve an external resource.
    """
    expat_parser = getattr(parser, "parser", parser)
    if policy.forbid_dtd:
        expat_parser.StartDoctypeDeclHandler = _reject_doctype
    if policy.forbid_entities:
        expat_parser.EntityDeclHandler = _reject_entity
        expat_parser.UnparsedEntityDeclHandler = _reject_entity
    if policy.forbid_external:
        expat_parser.ExternalEntityRefHandler = _reject_external


def _reject_doctype(*args: Any) -> None:
    raise ResourcePolicyError("XML DOCTYPE declarations are forbidden by the parser policy")


def _reject_entity(*args: Any) -> None:
    raise ResourcePolicyError("XML entity declarations are forbidden by the parser policy")


def _reject_external(*args: Any) -> None:
    raise ResourcePolicyError("XML external references are forbidden by the parser policy")


__all__ = [
    "XmlLimits",
    "XmlParserPolicy",
    "configure_expat_security",
]
