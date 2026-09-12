import contextlib
import os.path
from pathlib import Path
from typing import IO, Any

from pyxsd.parser import PyXSD
from pyxsd.transforms.displayer import Displayer
from pyxsd.validation import ValidationReport


class SendTreeToPyXSD(Displayer):
    """
    :Category: Standard Transform Tools
    :Description: Sends the generated XML back into pyXSD
    """

    # Set so the transform driver hands this transform the enclosing
    # parser: its schema and binding mode become the revalidation
    # defaults, and its report absorbs the findings of the reparse.
    inheritsParserContext = True

    def __init__(self, root: Any) -> None:
        super().__init__(root)
        #: The PyXSD object of the last revalidation, if one ran.
        self.parser: PyXSD | None = None
        #: The validation report of the last revalidation.
        self.report: ValidationReport | None = None
        self._xmlInput: IO[str] | None = None

    def __call__(
        self,
        xsdFile: str | Path | os.PathLike[str] | IO[str] | None = None,
        xmlFileOutput: str | bool = "_No_Output_",
        transformOutputName: str | None = None,
        transforms: list[str] | None = None,
        transformFile: str | None = None,
        classFile: str | None = None,
        verbose: bool = False,
        quiet: bool = False,
        mode: Any = None,
        namespace_schemas: dict[str, str] | None = None,
    ) -> Any:
        """Reparses the tree and stores the run's report on ``self``.

        - ``xsdFile``: the schema to revalidate against. When omitted,
          the enclosing parser's schema is used as it was given: a path
          stays a path, and an in-memory schema stream is rewound and
          reused rather than converted to a filename. A standalone call
          without any schema raises the parser's usual no-schema error.

        - ``mode``: the binding policy for the revalidation. When
          omitted, the enclosing parser's mode is inherited.

        - ``namespace_schemas``: ``{namespace: location}`` mappings for
          imports without a ``schemaLocation``. When omitted, the
          enclosing parser's mapping is inherited.

        - ``self.parser``: the constructed PyXSD object (the tree it
          parsed is its own bound copy; the transform still returns the
          caller's original tree).

        - ``self.report``: the revalidation report, also merged into
          the enclosing run's report when driven by ``PyXSD.transform``.
        """
        outer = getattr(self, "outerParser", None)
        if xsdFile is None and outer is not None:
            xsdFile = outer.xsdFile
            if hasattr(xsdFile, "seek"):
                # Non-seekable streams are reused as-is.
                with contextlib.suppress(OSError, ValueError):
                    xsdFile.seek(0)
        if namespace_schemas is None and outer is not None:
            namespace_schemas = dict(outer.namespaceSchemas)
        try:
            self._xmlInput = self.makeTempFileOfTree()
            if not xmlFileOutput:
                xmlFileOutput = "tempFileParsed.xml"
            if transformOutputName is None:
                transformOutputName = "tempFileTransformed.xml"
            if transformFile:
                with open(transformFile) as fd:
                    lines: list[str] = [line.strip().strip(">").strip() for line in fd]
                transforms = lines
            kwargs: dict[str, Any] = {
                "xmlFileOutput": xmlFileOutput,
                "transformOutputName": transformOutputName,
                "transforms": transforms,
                "classFile": classFile,
                "verbose": verbose,
                "quiet": quiet,
            }
            if namespace_schemas:
                kwargs["namespace_schemas"] = namespace_schemas
            if mode is None and outer is not None:
                mode = outer.mode
            if mode is not None:
                kwargs["mode"] = mode
            parser = PyXSD(self._xmlInput, xsdFile, **kwargs)
        finally:
            if self._xmlInput is not None:
                self._xmlInput.close()
        self.parser = parser
        self.report = parser.report
        return self.root
