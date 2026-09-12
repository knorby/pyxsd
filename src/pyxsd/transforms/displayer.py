import sys
import tempfile
from typing import IO, Any

from pyxsd.transforms.transform import Transform
from pyxsd.writers.xml_tree_writer import XmlTreeWriter

"""
Transform Library: Displayer
============================

:Category: Standard Transform Tools
:Description: Library containing functions to help print data
"""


class Displayer(Transform):
    # The tree root, stored by concrete subclass ``__init__``.
    root: Any

    def openFile(self, fileName: str | None = None) -> IO[str]:  # uses stdout if filename is None
        if fileName is None or fileName == "stdout":
            return sys.stdout
        return open(fileName, "w")

    def writeTree(self, file: IO[str]) -> None:
        XmlTreeWriter(self.root, file)
        file.flush()

    def makeTempFileOfTree(self) -> IO[str]:
        newTree = tempfile.TemporaryFile(mode="w+")  # noqa: SIM115 - returned to caller
        try:
            self.writeTree(newTree)
        except BaseException:
            # The caller never receives the stream, so close it here;
            # otherwise a serialization failure leaks the file handle.
            newTree.close()
            raise
        newTree.seek(0)
        return newTree
