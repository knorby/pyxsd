from typing import Any

from pyxsd.parser import PyXSD
from pyxsd.transforms.displayer import Displayer


class SendTreeToPyXSD(Displayer):
    """
    :Category: Standard Transform Tools
    :Description: Sends the generated XML back into pyXSD
    """

    def __init__(self, root: Any) -> None:
        self.root = root

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
    ) -> Any:
        xmlInput = self.makeTempFileOfTree()
        if not xmlFileOutput:
            xmlFileOutput = "tempFileParsed.xml"
        if transformOutputName is None:
            transformOutputName = "tempFileTransformed.xml"
        if transformFile:
            with open(transformFile) as fd:
                lines: list[str] = [line.strip().strip(">").strip() for line in fd]
            transforms = lines
        PyXSD(
            xmlInput,
            xsdFile,
            xmlFileOutput,
            transformOutputName,
            transforms,
            classFile,
            verbose,
            quiet,
        )
        return self.root
