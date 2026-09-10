import math


class Vector(tuple):
    """
    Transform Library: CellSizer: Vector
    ====================================

    :Category: Computational Materials Science
    :Description: A class for vectors with some vector functions

    Included as part of the CellSizer Library
    """

    def __init__(self, val):
        tuple.__init__(val)

    def __mul__(self, scalar):
        if not isinstance(scalar, int) and not isinstance(scalar, float):
            raise TypeError("a scalar must be a int or float")
        return Vector([x * scalar for x in self])

    def __add__(self, vector):
        if not isinstance(vector, Vector):
            raise TypeError("a vector can only be added to another vector")
        if len(vector) != len(self):
            raise TypeError("vectors must have the same number of dimensions to be added")
        return Vector([a + b for a, b in zip(self, vector, strict=False)])

    def __sub__(self, vector):
        if not isinstance(vector, Vector):
            raise TypeError("vector subtraction must be between two vectors")
        if len(vector) != len(self):
            raise TypeError(
                "vectors must have the same number of dimensions "
                "to find the difference of two vectors"
            )
        return Vector([a - b for a, b in zip(self, vector, strict=False)])

    def findDotProduct(self, vector):
        product = 0
        if not isinstance(vector, Vector):
            raise TypeError("the dot product is between two vectors")
        if len(vector) != len(self):
            raise TypeError("the dot product is between two vectors in the same vector space")
        for a, b in zip(self, vector, strict=False):
            product += a * b
        return product

    def distance(self, vector):
        if not isinstance(vector, Vector):
            raise TypeError("the distance formula for vectors is between two vectors")
        if len(vector) != len(self):
            raise TypeError(
                "the distance formula for vectors is between two vectors in the same vector space"
            )
        subtractedVector = self - vector
        return subtractedVector.findLength()

    def findLength(self):
        return math.sqrt(self.findDotProduct(self))
