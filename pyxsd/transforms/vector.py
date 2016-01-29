import math


class Vector (tuple):

    """

    ====================================
    Transform Library: CellSizer: Vector
    ====================================

    :Author: Kali Norby <kali.norby@gmail.com>
    :Date: Fri, 1 Sept 2006
    :Category: Computational Materials Science
    :Description: A class for vectors with some vector functions
    :Copyright: pyXSD License

    Included as part of the CellSizer Library

    """

    def __init__(self, val):

        tuple.__init__(val)

    def __mul__(self, scalar):
        if not isinstance(scalar, int) and not isinstance(scalar, float):
            raise TypeError, "a scalar must be a int or float"
        multiply = lambda x: x * scalar
        return Vector(map(multiply, self))

    def __add__(self, vector):
        if not isinstance(vector, Vector):
            raise TypeError, "a vector can only be added to another vector"
        if not len(vector) == len(self):
            raise TypeError, "vectors must have the same number of dimensions to be added"
        listOfNewValues = []
        for a, b in zip(self, vector):
            listOfNewValues.append(a + b)
        return Vector(listOfNewValues)

    def __sub__(self, vector):
        if not isinstance(vector, Vector):
            raise TypeError, "vector subtraction must be between two vectors"
        if not len(vector) == len(self):
            raise TypeError, "vectors must have the same number of dimensions to find the difference of two vectors"
        listOfNewValues = []
        for a, b in zip(self, vector):
            listOfNewValues.append(a - b)
        return Vector(listOfNewValues)

    def findDotProduct(self, vector):
        product = 0
        if not isinstance(vector, Vector):
            raise TypeError, "the dot product is between two vectors"
        if not len(vector) == len(self):
            raise TypeError, "the dot product is between two vectors in the same vector space"
        for a, b in zip(self, vector):
            product += (a * b)
        return product

    def distance(self, vector):
        if not isinstance(vector, Vector):
            raise TypeError, "the distance formula for vectors is between two vectors"
        if not len(vector) == len(self):
            raise TypeError, "the distance formula for vectors is between two vectors in the same vector space"
        subtractedVector = self - vector
        return subtractedVector.findLength()

    def findLength(self):
        return math.sqrt(self.findDotProduct(self))
