"""Element representatives for XSD identity constraints.

XSD 1.0 lets an element declaration carry identity constraints:
``xs:key`` (fields present and unique), ``xs:unique`` (fields unique
when present) and ``xs:keyref`` (fields must match a named key or
unique). Each constraint contains ``xs:selector`` and ``xs:field``
child tags whose ``xpath`` attributes say which descendant nodes the
constraint covers and where the compared values come from.

These representatives only record the constraint on their parent
element declaration; the actual checking happens after the instance
document is bound, in :mod:`pyxsd.identity`.
"""

import logging

from pyxsd.element_representatives.element_representative import ElementRepresentative
from pyxsd.namespaces import XSD_NS, local_name, namespace_of

logger = logging.getLogger(__name__)


class IdentityConstraint(ElementRepresentative):
    """Base class for the key, unique and keyref tags."""

    #: Local names of tags that may appear inside an identity
    #: constraint. ``element`` and other declarations are not allowed.
    _ALLOWED_CHILDREN = ("annotation", "selector", "field")

    def __init__(self, xsdElement, parent):
        """Collects the selector and field paths from the child tags.

        ``xs:selector`` and ``xs:field`` children supply the XPath
        subset paths (the ``xpath`` attribute on each tag). Constraints
        are only legal on element declarations; a constraint found
        anywhere else is dropped with a warning.

        See ElementRepresentative for documentation.
        """
        # Must exist before ``super().__init__`` runs ``processChildren``.
        self.unexpectedChildTags: list[str] = []
        super().__init__(xsdElement, parent)
        selectors = [
            child.xpath
            for child in self.processedChildren
            if child is not None and child.__class__.__name__ == "Selector"
        ]
        self.selector = selectors[0] if selectors else ""
        self.fieldPaths = [
            child.xpath
            for child in self.processedChildren
            if child is not None and child.__class__.__name__ == "Field"
        ]
        self.constraintName = self.xsdElement.get("name") or self.name
        container = self.parent
        if container is not None and container.__class__.__name__ == "Element":
            container.identities.append(self)
        else:
            logger.warning(
                "identity constraint '%s' must be declared inside an element "
                "declaration; it was found inside a %s and will be ignored",
                self.constraintName,
                container.__class__.__name__ if container is not None else "unknown parent",
            )

    def processChildren(self):
        """Builds ERs only for the legal annotation/selector/field
        children.

        Anything else (for example an ``element`` declaration inside a
        ``keyref``) is not a component of the constraint; its tag is
        recorded on ``unexpectedChildTags`` so the parser can report
        the schema error after the ER run.
        """
        for child in self.xsdElement:
            tagName = local_name(child.tag)
            if namespace_of(child.tag) != XSD_NS or tagName not in self._ALLOWED_CHILDREN:
                self.unexpectedChildTags.append(tagName)
                self.processedChildren.append(None)
                continue
            self.processedChildren.append(ElementRepresentative.factory(child, self))
        return None

    def getName(self):
        """Makes a unique bookkeeping name for the constraint."""
        contName = self.getContainingTypeName()
        return f"{contName}|{self.__class__.__name__}|{self.xsdElement.get('name') or '?'}"


class Selector(ElementRepresentative):
    """The class for the selector tag inside an identity constraint."""

    def getName(self):
        """Returns a bookkeeping name for the selector."""
        return f"{self.getContainingTypeName()}|selector"


class Field(ElementRepresentative):
    """The class for the field tag inside an identity constraint."""

    def __init__(self, xsdElement, parent):
        """Records the field's ``xpath`` attribute.

        See ElementRepresentative for documentation.
        """
        super().__init__(xsdElement, parent)
        self.xpath = self.tagAttributes.get("xpath", "")

    def getName(self):
        """Returns a bookkeeping name for the field."""
        return f"{self.getContainingTypeName()}|field"


class Key(IdentityConstraint):
    """The class for the key tag."""


class Unique(IdentityConstraint):
    """The class for the unique tag."""


class Keyref(IdentityConstraint):
    """The class for the keyref tag.

    A keyref additionally carries a ``refer`` attribute naming the
    key or unique that its values must match.
    """

    def __init__(self, xsdElement, parent):
        """Stores the referenced constraint name.

        See IdentityConstraint and ElementRepresentative for
        documentation.
        """
        super().__init__(xsdElement, parent)
        self.refer = self.tagAttributes.get("refer", "")
