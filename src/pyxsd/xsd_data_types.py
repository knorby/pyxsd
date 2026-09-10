"""Primitive and derived data types from XML Schema.

This module contains classes for the primitive data types found in XML
schema (XSD) files.  These check that the value assigned to an element
is of the proper data type.  Many correspond closely to data types in
Python, and others are more complex.

The subclasses of Python's immutable builtins (``int``, ``float``,
``str``, ``tuple``) do their construction and validation in ``__new__``
rather than ``__init__``, per the standard pattern for subclassing
immutable types.
"""

import base64

__all__ = [
    "ID",
    "IDREF",
    "Base64Binary",
    "Boolean",
    "Double",
    "Integer",
    "NegativeInteger",
    "NonNegativeInteger",
    "NonPositiveInteger",
    "PositiveInteger",
    "String",
    "TypeList",
    "XsdDataType",
]


class XsdDataType:
    """An empty class. Acts as a common base class for all of the other
    primitive data classes, so it is easier to pick out the classes
    from the module.
    """


class Integer(int, XsdDataType):
    """Identical to the integer type in Python."""

    name = "Integer"


class PositiveInteger(Integer):
    """Integers with values greater than zero."""

    name = "PositiveInteger"

    def __new__(cls, val):
        if int(val) <= 0:
            raise TypeError("Not a positive Integer")
        return super().__new__(cls, val)


class NonNegativeInteger(Integer):
    """Integers with values greater than or equal to zero."""

    name = "NonNegativeInteger"

    def __new__(cls, val):
        if int(val) < 0:
            raise TypeError("Not a non-negative Integer")
        return super().__new__(cls, val)


class NegativeInteger(Integer):
    """Integers with values less than zero."""

    name = "NegativeInteger"

    def __new__(cls, val):
        if int(val) >= 0:
            raise TypeError("Not a negative Integer")
        return super().__new__(cls, val)


class NonPositiveInteger(Integer):
    """Integers with values less than or equal to zero."""

    name = "NonPositiveInteger"

    def __new__(cls, val):
        if int(val) > 0:
            raise TypeError("Not a non-positive Integer")
        return super().__new__(cls, val)


class Double(float, XsdDataType):
    """Identical to the float type in Python."""

    name = "Double"


class TypeList(list, XsdDataType):
    """Identical to the list type in Python."""

    name = "List"


class Boolean(Integer):
    """Class for Boolean values.

    In Python, the boolean type is really an integer that is either 0
    or 1, and it cannot be used as a base class, so this class
    subclasses ``int`` instead.  Since booleans are normally expressed
    as ``True`` or ``False`` in Python, the programmer must be sure
    that True is entered as 1 and False as zero when using this class.
    The class defines ``__str__`` and ``__repr__`` methods so the data
    is presented appropriately: ``__str__`` produces the XML lexical
    form ("true" / "false") and ``__repr__`` the Python form.
    """

    name = "Boolean"

    def __new__(cls, val):
        val = int(val)
        if val < 0 or val > 1:
            raise TypeError(f"Invalid Boolean Value {val}")
        obj = super().__new__(cls, val)
        obj.val = val
        return obj

    def __str__(self):
        """Returns 'true' or 'false', depending on the value of 'val'.
        Use for xml and xsd files.
        """
        if self.val == 1:
            return "true"
        return "false"

    def __repr__(self):
        """Returns 'True' or 'False', depending on the value of 'val'.
        Use for Python.
        """
        if self.val == 1:
            return "True"
        return "False"


class String(str, XsdDataType):
    """Identical to the string type in Python."""

    name = "String"


class ID(String):
    """Used for ID attributes in xml and xsd files."""

    name = "ID"


class IDREF(String):
    """Used for IDREF attributes in xml and xsd files."""

    name = "IDREF"


class Base64Binary(String):
    """Used with data encoded into base64.

    Treats the data as a string and has :class:`String` as a base
    class.  Tries to see if the binary is valid by decoding it with
    the base64 library included with Python.  This process may not
    detect errors every time; it should be able to see if the base64
    binary is well-formed when working correctly.
    """

    name = "Base64Binary"

    def __new__(cls, val):
        try:
            base64.b64decode(val, validate=True)
        except (ValueError, TypeError):
            raise TypeError("Not a valid Base64 Binary") from None
        return super().__new__(cls, val)
