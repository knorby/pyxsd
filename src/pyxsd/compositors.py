"""Compositor kinds for XSD content models.

A compositor is the schema construct that groups element particles
inside a complex type: ``xs:sequence``, ``xs:choice``, or ``xs:all``.
The enum inherits from ``str`` so members compare equal to their
lexical XSD spellings.
"""

import enum


class Compositor(enum.StrEnum):
    """The kind of content-model compositor grouping a set of elements."""

    SEQUENCE = "sequence"
    CHOICE = "choice"
    ALL = "all"
