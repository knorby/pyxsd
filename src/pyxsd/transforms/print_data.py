import sys

from pyxsd.transforms.displayer import Displayer

"""
Transform: PrintData
====================

:Category: Standard Transform Tools
:Description: sends tree to the writer
"""


class PrintData(Displayer):
    def __init__(self, root):
        self.root = root

    def __call__(self, fileName=None):
        output = self.openFile(fileName)
        try:
            self.writeTree(output)
        finally:
            # stdout is shared; files opened for this write are ours to close.
            if output is not sys.stdout:
                output.close()
        return self.root
