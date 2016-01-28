from displayer import Displayer

"""
====================
Transform: PrintData
====================

:Author: Karl Norby <knorby@uchicago.edu>
:Date: Fri, 1 Sept 2006
:Category: Standard Transform Tools
:Description: sends tree to the writer
:Copyright: pyXSD License

"""


class PrintData(Displayer):
    #============================================================
    #

    def __init__(self, root):
        self.root = root

    #============================================================
    #
    def __call__(self, fileName=None):
        self.writeTree(self.openFile(fileName))
        return self.root
