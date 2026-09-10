class BravaisLattice:
    """A Bravais lattice (vectors + atom basis) for the CellSizer example library.

    Historical note: part of the original 0.1 crystallography
    transform library; moved to ``examples/transforms/`` in pyxsd 1.0.
    """

    def __init__(self, vectors, basis):
        self.vectors = vectors
        self.basis = basis
