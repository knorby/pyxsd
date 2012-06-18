from displayer import Displayer
from pyxsd.pyXSD import PyXSD

class SendTreeToPyXSD(Displayer):
    """

    :Author: Kali Norby <kali.norby@gmail.com>
    :Date: Fri, 8 Sept 2006
    :Category: Standard Transform Tools
    :Description: Sends the generated XML back into pyXSD
    :Copyright: pyXSD License
    
    """
    #============================================================
    #
    def __init__(self, root):
        self.root = root
    
    #============================================================
    #
    def __call__(self, xsdFile=None, xmlFileOutput='_No_Output_', transformOutputName=None, transforms=[], transformFile=None, classFile=None, verbose = False, quiet=False):
        xmlInput = self.makeTempFileOfTree()
        if not xmlFileOutput:
            xmlFileOutput = "tempFileParsed.xml"
        if transformOutputName==None:
            xmlFileOutput = "tempFileTransformed.xml"
        linestrip = lambda x: x.strip('>').strip('\n').strip()
        if transformFile:
            transformFile = open(transformFile, 'r')
            transforms = transformFile.readlines()
            transformFile.close()
            transforms = map(linestrip, transforms)
        PyXSD(xmlInput, xsdFile, xmlFileOutput, transformOutputName, transforms, classFile, verbose, quiet)
        return self.root
