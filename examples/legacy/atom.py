class Atom:
    """An atom (position + type) in the CellSizer example library.

    Historical note: part of the original 0.1 crystallography
    transform library; moved to ``examples/legacy/`` in pyxsd 1.0.
    """

    def __init__(self, position, atomType):
        self.position = position
        self.atomType = atomType
