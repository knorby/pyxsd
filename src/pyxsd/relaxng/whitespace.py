"""XML 1.0 whitespace predicates shared by RELAX NG reading and simplification.

RELAX NG 1.0 section 4.2 removes only the four XML whitespace characters
(#x20, #x9, #xD, #xA) from name, type, and combine values.  Unicode
whitespace such as U+00A0 is content, not padding, so these helpers never use
``str.isspace`` or the default Unicode ``strip``.
"""

from __future__ import annotations

__all__ = ["XML_WHITESPACE", "is_xml_whitespace", "trim_xml_whitespace"]

#: The whitespace characters named by XML 1.0 and RELAX NG 1.0 section 4.2.
XML_WHITESPACE = " \t\r\n"


def is_xml_whitespace(text: str) -> bool:
    """Whether *text* consists solely of XML whitespace characters."""
    return all(character in XML_WHITESPACE for character in text)


def trim_xml_whitespace(text: str) -> str:
    """Remove leading and trailing XML whitespace characters from *text*."""
    return text.strip(XML_WHITESPACE)
