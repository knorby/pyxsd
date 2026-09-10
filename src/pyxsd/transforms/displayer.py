import sys
import tempfile

from pyxsd.transforms.transform import Transform
from pyxsd.writers.xml_tree_writer import XmlTreeWriter

"""
Transform Library: Displayer
============================

:Category: Standard Transform Tools
:Description: Library containing functions to help print data
"""


class Displayer(Transform):
    def openFile(self, fileName=None):  # uses stdout if filename is None
        if fileName is None or fileName == "stdout":
            return sys.stdout
        return open(fileName, "w")

    def writeTree(self, file):
        XmlTreeWriter(self.root, file)

    def makeTempFileOfTree(self):
        newTree = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115 - returned to caller
        self.writeTree(newTree)
        newTree.seek(0)
        return newTree
