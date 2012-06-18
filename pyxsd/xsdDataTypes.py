import types
import base64

"""
This module contains classes of primitive data types found in xml xsd files.
These check of make sure that the value assigned to an element is of the proper data type.
Many correspond closely to data types in python, and others are more complex.

Every `__init__` method class, which is in every class but XsdDataType, has `val` as its only
parameter. `val` is the value assigned that is being checked. For example, in the assignment 'Integer(5)',
'Integer' is the class and '5' the value being checked.

This module contains the following classes:

- *XsdDataType*
- *Integer*
- *PositiveInteger*
- *NonNegativeInteger*
- *NegativeInteger*
- *NonPositiveInteger*
- *Double*
- *TypeList*
- *Boolean*
- *String*
- *ID*
- *IDREF*
- *Base64Binary*

"""
#============================================================
#
class XsdDataType (object):
    """
    An empty class. Acts as a common base class for all of the other primitive data classes, so
    it is easier to pick out the classes from the module. Has *object* as a base class.
    """
    pass

#============================================================
#
class Integer(int, XsdDataType):
    """
    Identical to the integer type in python. Has *int* and *XsdDataType* as base classes.
    """

    name = 'Integer'

    def __init__(self, val):
        
        int.__init__(self, val)

#============================================================
#
class PositiveInteger(Integer):
    """
    Integers with values greater than zero. Has *Integer* as a base class.
    """
    name = 'PositiveInteger'
    
    def __init__(self, val):
    
        if val <= 0:

            raise TypeError, "Not a positive Integer"

        Integer.__init__(self, val)

#============================================================
#
class NonNegativeInteger(Integer):
    """
    Integers with values greater than or equal to zero. Has *Integer* as a base class.
    """
    name = 'NonNegativeInteger'
    
    def __init__(self, val):
    
        if val < 0:

            raise TypeError, "Not a non-negative Integer"

        Integer.__init__(self, val)

#============================================================
#
class NegativeInteger(Integer):
    """
    Integers with values less than zero. Has *Integer* as a base class.
    """
    name = 'NegativeInteger'
    
    def __init__(self, val):
    
        if val >= 0:

            raise TypeError, "Not a negative Integer"

        Integer.__init__(self, val)


#============================================================
#
class NonPositiveInteger(Integer):
    """
    Integers with values less than or equal to zero. Has *Integer* as a base class.
    """
    name = 'NonPostiveInteger'
    
    def __init__(self, val):
    
        if val > 0:

            raise TypeError, "Not a non-postive Integer"

        Integer.__init__(self, val)

#============================================================
#
class Double(float, XsdDataType):
    """
    Identical to the float type in python. Has *float* and *XsdDataType* as base classes.
    """
    name = 'Float' #Should this be 'Double' instead?

    def __init__(self, val):
        
        float.__init__(self,val)

#============================================================
#
class TypeList(list, XsdDataType):
    """
    Identical to the list type in python. Has *list* and *XsdDataType* as base classes.
    """
    name = "List"

    def __init__(self, val):

        list.__init__(self, val)
#============================================================
#
class Boolean(Integer):
    """
    Class for Boolean. The boolean type in python is not quite like other types in the language.
    The Boolean type class in python cannot be used as a base class or have its `__init__` function
    used as it is with the other classes in the module. In python, the boolean type is really an integer
    that can either be 0 or 1. Since booleans are normally expressed as 'True' or 'False' in python, the
    programmer must be sure that True is entered as 1 and False as zero when using this class. The class
    has defined `__str__` and `__repr__` methods, so that the data is presented in a way that makes more
    sense in python or xml, depending on the use. `__str__` is for python and `__repr__` for python. Make
    sure to use the appropiate function when using booleans. Class has *Integer* as a base class.
    """
    name = 'Boolean'

    def __init__(self, val):

        self.val = val
        
        if val < 0 or val > 1:

            raise TypeError, "Invalid Boolean Value %i" % val 

        Integer.__init__(self, val)
        
    def __str__(self):
        """
        Returns 'true' or 'false', depending on the value of 'val', when the str() function is used. Use
        for xml and xsd files.
        """
        if self.val == 1:
            return "true"
        if self.val == 0:
            return "false"

    def __repr__(self):
        """
        Returns True or False, depending on the value of 'val', when the repr() function is used. Use
        for python.
        """
        if val == 1:
            return True
        if val == 0:
            return False 
    
#============================================================
#
class String(str, XsdDataType):
    """
    Identical to the string type in python. Has *str* and *XsdDataType* as base classes.
    """
    
    name = 'String'

    def __init__(self, val):
        
        str.__init__(self, val)


#============================================================
#
class ID(String):
    """
    Used for ID attributes in xml and xsd files. Uses *String* as a base class, and makes no changes to it.
    """
    name = 'ID'

    def __init__(self, val):
        
        String.__init__(self, val)
    
#============================================================
#
class IDREF(String):
    """
    Used for IDREF attributes in xml and xsd files. Uses *String* as a base class, and makes no changes to it.
    """
    name = 'IDREF'

    def __init__(self, val):
        
        String.__init__(self, val)
    
#============================================================
#
class Base64Binary(String):
    """
    Used with data encoded into base64. Treats the data as a string, and has *String* as a base class.
    Tries to see if the the binary is valid by decoding and then reencoding it with the base64 library
    included with python. This process may not detect errors every time. It should only be able to see
    if the base64 binary is well-formed when working correctly.
    """
    name = 'Base64Binary'
    
    def __init__(self, val):
        try:
            valTestDe = base64.decodestring(val)
            valTestEn = base64.encodestring(valTestDe)

        except:
            raise TypeError, "Not a valid Base64 Binary"


        String.__init__(self, val)
