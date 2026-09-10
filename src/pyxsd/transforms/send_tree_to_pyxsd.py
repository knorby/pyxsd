from pyxsd.parser import PyXSD
from pyxsd.transforms.displayer import Displayer


class SendTreeToPyXSD(Displayer):
    """
    :Category: Standard Transform Tools
    :Description: Sends the generated XML back into pyXSD
    """

    def __init__(self, root):
        self.root = root

    def __call__(
        self,
        xsdFile=None,
        xmlFileOutput="_No_Output_",
        transformOutputName=None,
        transforms=None,
        transformFile=None,
        classFile=None,
        verbose=False,
        quiet=False,
    ):
        xmlInput = self.makeTempFileOfTree()
        if not xmlFileOutput:
            xmlFileOutput = "tempFileParsed.xml"
        if transformOutputName is None:
            transformOutputName = "tempFileTransformed.xml"
        if transformFile:
            with open(transformFile) as fd:
                transforms = [line.strip().strip(">").strip() for line in fd]
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
