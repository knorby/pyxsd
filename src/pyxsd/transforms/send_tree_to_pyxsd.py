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
        xsdFile: str | None = None,
        xmlFileOutput: str | bool = "_No_Output_",
        transformOutputName: str | None = None,
        transforms: list[str] | None = None,
        transformFile: str | None = None,
        classFile: str | None = None,
        verbose: bool = False,
        quiet: bool = False,
        mode: Any = None,
    ) -> Any:
        """Reparses the tree and stores the run's report on ``self``.

        - ``xsdFile``: the schema to revalidate against. When omitted,
          the enclosing parser's schema is used (driver-attached runs
          only; a standalone call without any schema raises the
          parser's usual no-schema error).

        - ``mode``: the binding policy for the revalidation. When
          omitted, the enclosing parser's mode is inherited.

        - ``self.parser``: the constructed PyXSD object (the tree it
          parsed is its own bound copy; the transform still returns the
          caller's original tree).

        - ``self.report``: the revalidation report, also merged into
          the enclosing run's report when driven by ``PyXSD.transform``.
        """
        outer = getattr(self, "outerParser", None)
        if xsdFile is None and outer is not None:
            xsdFile = str(outer.xsdFile)
        self._xmlInput = self.makeTempFileOfTree()
        try:
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
            if mode is None and outer is not None:
                mode = outer.mode
            if mode is not None:
                kwargs["mode"] = mode
            parser = PyXSD(self._xmlInput, xsdFile, **kwargs)
        finally:
            self._xmlInput.close()
        self.parser = parser
        self.report = parser.report
        return self.root
