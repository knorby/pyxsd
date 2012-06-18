from elementRepresentative import ElementRepresentative
from pyxsd.xsdDataTypes    import *


#============================================================
#
class Element(ElementRepresentative):
    """
    The class for the element tag. Subclass of *ElementRepresentative*.
    The element tag and the attribute tag are the most important in the
    xml and in the program, so this class contains some machinery that
    many of the other classes do not have. The element and attribute
    classes contain descriptor methods. By specifing the `__get__`,
    `__set__`, and `__delete__` (with `__get__` and `__set__` being the
    most important). These methods specify how a variable is set and
    how it retrieved. Any modification of these methods should be made
    under extreme caution! Both Element and Attribute, primarily
    Attribute, use these descriptors to add a level oof checking to the
    program. If some variable is set to some value that does no match
    the specifications in the schema, an error will be raised. These
    methods add a powerful layer of functionality, with a small amount
    of code; however, these functions are almost invisible unless they
    raise an error, so developers should bear in mind these methods
    when modifying the program.
    """    
    def __init__(self, xsdElement, parent):
        """
        Adds itself to the element list in its parent.
        See *ElementRepresentative* for documentation.
        """

        ElementRepresentative.__init__(self, xsdElement, parent)

        parent.elements.append(self)

    #============================================================
    #
    def getType(self):
        """
        Returns its type from the class dictionary in *PyXSD*. The
        instance of *PyXSD* is attached to every element and attribute
        while the classes for the schema types are being built. Clearly,
        this function is used after the main ER run.
        """
        if not 'type' in self.__dict__:

            raise "Element.getType() Error: type is not in %s's dictionary."
            return None

        if self.type in self.pyXSD.classes.keys():
            return self.pyXSD.classes[self.type]
        
        return ElementRepresentative.typeFromName(self.type, self.pyXSD)

    #============================================================
    #
    def processChildren(self):
        """
        There is a special processChildren() here to handle special types,
        which can be declared as a child of an element. If an element
        child can exist that is not a type, then this function will screw
        it up; however, as far as the developers knew at the time of writing
        this program, they cannot.

        No parameters.
        """
        children         = self.xsdElement.getchildren()

        if len(children) == 0: return None
        
        for child in children:

            processedChild = ElementRepresentative.factory(child, self)

            self.processedChildren.append(processedChild)

            self.type = processedChild.name

            self.tagAttributes['type'] = self.type

            processedChild.processChildren()
            
    #============================================================
    #
    def __str__(self):
        """
        Prints its name in a form that allows for quick identification
        of an element, without needing a bulky name that does not
        match the name used.
        """
        return "%s|%s|%s" % (ElementRepresentative.getContainingTypeName(self), \
                             self.__class__.__name__, self.name)

    #============================================================ Descriptor Protocol
    #============================================================ 
    #
    def __get__(self, obj, mystery=None):
        """
        Gets an element value from the obj's dictionary. Returns
        it value if it has one, returns the default value if it
        does not.

        The second argument is called `mystery`. It does not do
        anything, so it should not really be thought of as a
        parameter to this method. This variable is needed because of,
        you guessed it, a mystery. Sometimes, but not every time,
        the obj is passed in as an instance and it corresponding
        class. Perhaps it is slightly different, but it does this
        very odd thing with only some of the attributes/elements.
        As far as I can tell, the 'mystery' does not cause any
        problems, but it is a bug, and should be fixed if
        possible.

        See python documentation for full documentation on
        descriptors. Some links should be included on the
        `pyXSD website <http://pyxsd.org>`_.
        """

        if self.name in obj.__dict__:
            return obj.__dict__[self.name]

        default = getattr(self, 'default', None)
        return default

    #============================================================ 
    #
    def __set__(self, obj, value):
        """
        Sets an element's name to the element in the obj's dictionary.
        If multiple elements exist, sets it to a list. If it is not
        an element, raises an error. Has code for case when it is dictionary,
        but no case in which a dictionary would be used.

        See python documentation for full documentation on
        descriptors. Some links should be included on the
        `pyXSD website <http://pyxsd.org>`_.
        """
        if not isinstance(value, self.getType()):
            raise Exception()

        value = obj.__dict__.get(self.name,None)

        if self.isList():
            if value == None:
                obj.__dict__[self.name] = []
            obj.__dict__[self.name].append(value)
            return

        if self.isDict():
            if value == None:
                obj.__dict__[self.name] = {}
                obj.__dict__[self.name][obj.id] = value
                return
            
        obj.__dict__[self.name] = value

    #============================================================ 
    #
    def __delete__(self, obj):
        """
        Deletes an entry from the dictionary.

        See python documentation for full documentation on
        descriptors. Some links should be included on the
        `pyXSD website <http://pyxsd.org>`_.
        """

        del obj.__dict__[self.name]

    #============================================================ 
    #
    def isDict (self):
        """
        Returns false. Placeholder function for possible future
        addition of case where the an element could best be
        expressed as a dictionary.

        No parameters
        """
        return False

    #============================================================ 
    #
    def isList (self):
        """
        Returns true if maxOccurs is greater than one. If it is
        true, treats all of the elements that are from the schema
        definition as a list. Otherwise returns false.

        No parameters.
        """
        minOccurs = self.getMinOccurs()
        
	maxOccurs = self.getMaxOccurs()

	if maxOccurs > 1:

	   return True
       
        return False

    #============================================================ 
    #
    def getMinOccurs(self):
        """
        Returns an integer value for the `minOccurs`. If no `minOccurs`
        has been set, uses default of 1.

        No parameters
        """
        return int(getattr(self, 'minOccurs', 1)) 

    #============================================================ 
    # 
    def getMaxOccurs(self):
        """
        Returns an integer value for the `maxOccurs`. If no `maxOccurs`
        has been set, uses default of 1. If `maxOccurs` is set to
        'unbounded', returns 99999, since this should cover about
        every case in which someone would use 'unbounded'

        No parameters
        """
        maxOccurs = getattr(self, 'maxOccurs', 1)
        if maxOccurs == 'unbounded':
            return 99999
        return int(maxOccurs)
        
