from elementRepresentative import ElementRepresentative
from pyxsd.xsdDataTypes    import *

#============================================================
#
class Attribute(ElementRepresentative):
    """
    The class for the attribute tag. Subclass of *ElementRepresentative*.
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
        Adds itself to the attribute dictionary in its containing
        type. See *ElementRepresentative* for documentation.
        """
        ElementRepresentative.__init__(self, xsdElement, parent)

        self.getContainingType().attributes[self.name] = self

    #============================================================
    #
    def __str__(self):
        """
        Prints its name in a form that allows for quick identification
        of an attribute, without needing a bulky name that does not
        match the name used.
        """
        return "%s|%s|%s" % (self.getContainingTypeName(),
                             self.__class__.__name__,
                             ElementRepresentative.getName(self))


    #============================================================
    #
    def processChildren(self):
        """
        There is a special processChildren() here to handle special types,
        which can be declared as a child of an attribute. If an attribute
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
    def getType(self):
        """
        Returns its type from the class dictionary in *PyXSD*. The
        instance of *PyXSD* is attached to every element and attribute
        while the classes for the schema types are being built. Clearly,
        this function is used after the main ER run.
        """
        if not 'type' in self.__dict__:

            raise "Attribute.getType() Error: type is not in %s's dictionary."
            return None

        if self.type in self.pyXSD.classes.keys():
            return self.pyXSD.classes[self.type]
        
        return ElementRepresentative.typeFromName(self.type, self.pyXSD)



    #============================================================ Descriptor Protocol
    #============================================================ 
    #
    def __get__(self, obj, mystery=None):
        """
        Gets an attribute value from the obj's dictionary. Returns
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
        Sets values to attributes. Converts text Boolean values to
        binary values (integers of 0 and 1).

        See python documentation for full documentation on
        descriptors. Some links should be included on the
        `pyXSD website <http://pyxsd.org>`_.
        """
        if issubclass(self.getType(), XsdDataType):
            if self.getType() == Boolean:
                if isinstance(value, basestring):

                    if value == 'true' or value == 'True':

                        value = 1

                    elif value =='False' or value =='false':

                        value = 0
            try:
                value = self.getType()(value)
            except Exception, e:
                print
                print "Parser Error: One of your attributes is invalid."
                print "The program's error message is as follows:"
                print "   %s" % e
                print "The program will attempt to continue, but may experience errors."
                print
        elif not isinstance(obj, self.getType()):
           
            raise Exception()
        
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
    def getUse(self):
        """
        Returns the 'use' value, which says if the attribute is required
        or the default optional. 
        """
        if not 'use' in self.__dict__.keys():
            self.use = 'optional'
        return self.use
    
