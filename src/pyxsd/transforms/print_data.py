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
        self.writeTree(self.openFile(fileName))
        return self.root
