"""The schema-bound instance document: the interaction hub.

A :class:`Document` pairs a compiled :class:`~pyxsd.schema.Schema` with
one bound instance tree and the merged
:class:`~pyxsd.validation.ValidationReport` for the run (schema-phase
issues followed by this document's instance-phase issues). It is what
:meth:`pyxsd.schema.Schema.parse` returns, and the object callers
inspect (``report``/``is_valid``/``require_valid``) and write out
(``write``/``to_string``).
"""

from __future__ import annotations

import json
import logging
import os.path
from collections.abc import Callable, Iterator
from io import StringIO
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from pyxsd.exceptions import PyXSDError, ValidationError
from pyxsd.tree import iter_tree
from pyxsd.validation import ValidationReport
from pyxsd.writers.xml_tree_writer import XmlTreeWriter

if TYPE_CHECKING:
    from pyxsd.schema import Schema

logger = logging.getLogger(__name__)


def write_tree(root: Any, output: str | Path | os.PathLike[str] | IO[str]) -> None:
    """Sends a pythonic instance tree to the tree writer.

    - ``root``: the root instance of a tree. Must be
      formatted in the program's tree structure.

    - ``output``: the file object (or path) to write the tree to.
      Paths are opened and closed here; file objects passed by the
      caller are flushed but left open.
    """
    if isinstance(output, (str, os.PathLike)):
        with open(output, "w") as outputFile:
            XmlTreeWriter(root, outputFile)
    else:
        XmlTreeWriter(root, output)
        output.flush()
    logger.debug("Data sent to the writer...")


class Document:
    """A schema-bound instance document: the interaction hub."""

    #: The schema the document was parsed against.
    schema: Schema
    #: The bound root instance, or ``None`` when binding failed (for
    #: example an unknown document root element).
    root: Any
    #: The source the document was parsed from, as given to ``parse``.
    source: str | Path | os.PathLike[str] | IO[str] | ET.Element | None

    def __init__(
        self,
        schema: Schema,
        root: Any,
        source: str | Path | os.PathLike[str] | IO[str] | ET.Element | None,
        report: ValidationReport,
    ) -> None:
        self.schema = schema
        self.root = root
        self.source = source
        self._report = report

    @property
    def report(self) -> ValidationReport:
        """The merged report: schema-phase issues, then instance-phase."""
        return self._report

    @property
    def is_valid(self) -> bool:
        """Whether the report holds no error-severity issues."""
        return not self.report.has_errors

    def require_valid(self) -> None:
        """Raises :class:`pyxsd.ValidationError` if the report has errors."""
        if self.report.has_errors:
            raise ValidationError(
                f"the document is not valid: {len(self.report.errors)} error(s); "
                "inspect ValidationError.report",
                self.report,
            )

    def write(self, dest: str | Path | os.PathLike[str] | IO[str]) -> None:
        """Writes the bound tree to *dest* (a path or a file object)."""
        if self.root is None:
            raise PyXSDError("the document has no root to write")
        write_tree(self.root, dest)

    def to_string(self) -> str:
        """Returns the bound tree serialized as XML text."""
        if self.root is None:
            raise PyXSDError("the document has no root to write")
        output = StringIO()
        write_tree(self.root, output)
        return output.getvalue()

    def to_dict(self, *, typed: bool = True, always_list: bool = False) -> dict:
        """Returns the bound tree as a plain dict.

        The export convention (child element keys, ``@`` attributes, the
        ``$`` text key, repeated children as lists) is documented in
        ``docs/data-model.md``. Mixed-content tails are not in the bound
        tree and are therefore not exported.
        """
        if self.root is None:
            raise PyXSDError("the document has no root to export")
        # Deferred for the same cold-import reason as ``transform``: the
        # export module pulls in the datatype stack.
        from pyxsd.dict_export import bound_to_dict

        return bound_to_dict(self.root, typed=typed, always_list=always_list)

    def to_json(self, *, indent: int | None = None, **to_dict_kwargs: Any) -> str:
        """Returns :meth:`to_dict` encoded as JSON text.

        Keyword arguments are forwarded to :meth:`to_dict`. Values the
        JSON encoder cannot represent natively (for example a
        ``decimal.Decimal``) are encoded as strings.
        """
        return json.dumps(self.to_dict(**to_dict_kwargs), indent=indent, default=str)

    def transform(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Document | Any:
        """Applies *fn* to the bound tree.

        - ``fn(root, *args, **kwargs)`` may return a new tree root (a
          bound ``SchemaBase`` instance), which becomes the root of a
          new :class:`Document` against the same schema — or anything
          else, which is returned unchanged. Transforms may also mutate
          the tree in place and return ``None``; the document is
          unchanged then.

        A returned document shares this document's report, which
        reflects the tree as it was parsed, not the transformed shape;
        :meth:`revalidate` is the refresh path.
        """
        result = fn(self.root, *args, **kwargs)
        # Imported here (not at module level): a module-level import of
        # schema_base would drag the element_representatives stack in
        # behind this module, and that stack's internal import order is
        # load-bearing — entering it through schema_base from cold
        # breaks it. Deferred, the import stays cold-safe.
        from pyxsd.schema_base import SchemaBase

        if isinstance(result, SchemaBase):
            return Document(
                schema=self.schema,
                root=result,
                source=self.source,
                report=self._report,
            )
        return result

    def revalidate(self) -> Document:
        """Re-parses the serialized tree, returning a fresh :class:`Document`.

        The bound tree is written out and parsed again against the same
        schema, so the returned document carries a fresh instance report
        for the tree's current shape (transforms may have changed it).
        The :attr:`source` is carried over.
        """
        again = self.schema.parse(StringIO(self.to_string()))
        again.source = self.source
        return again

    def walk(self) -> Iterator[Any]:
        """Yields every node of the bound tree, pre-order."""
        return iter_tree(self.root)
