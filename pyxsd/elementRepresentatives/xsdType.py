import sys
from elementRepresentative import ElementRepresentative

#============================================================
#
class XsdType(ElementRepresentative):
    """
    This class is the base class for *SimpleType* and *ComplexType*
    Subclass of *ElementRepresentative*. In this class, the classes
    for all of the types are generated.
    """
    def __init__(self, xsdElement, parent):
        """
        The `__init__` for this class' subclasses.
        Creates a blank list for enumerations.
        Creates a blank dictionary for attributes.
        See *ElementRepresentative* for documentation.
        """
        self.enumerations = []

        self.attributes  = {}
        
        ElementRepresentative.__init__(self, xsdElement, parent)


    #============================================================
    #
    def getContainingTypeName(self):
        """
        Since all types are containing types, this method returns
        its own name.

        No parameters.
        """
        if self.name == None:

            self.name = self.getName()

        return self.name
    
    #============================================================
    #
    def getContainingType(self):
        """
        All types are containing types, so this works for all of the
        XSD types.

        No parameters.
        """
        return self
    
    #============================================================
    #
    def getName(self):
        """
        Mostly normal getName(), except it includes a means to make
        a type name if the type is the child of an element or
        some other tag. That name should look like this:

        `elementName`|`type tag type`

        No parameters
        """
        name = ElementRepresentative.getName(self)
        
        if not name == None:    return name

        # We are implicitly defined in an element
        element  = self.parent
        
        name = '%s|%s' % (element.name, self.tagType) 
        
        element.typeName = name
        
        return name

    #============================================================
    #
    def containsSchemaBase(self, bases):
        """
        Returns true if *SchemaBase* is in the bases list, false
        if it is not. Used by getBaseList()

        Parameters:

        - `bases`- the list of bases for the class that will be created
        
        """
        for base in bases:

            if issubclass(base,SchemaBase):
                return True
        
        return False
    #============================================================
    #
    def getBaseList(self, pyXSD): 
        """
        Creates blank list for the base classes. It goes through
        the list of super classes to be added (all type classes),
        and adds them. Adds SchemaBase, if it is not already added.
        Returns the list as a tuple, since the type factory must
        have the bases stored in a tuple, not a list.

        Parameters:

        - `pyXSD`- The *PyXSD* instance.
        
        """
        baseList = []

        for superClassName in self.superClassNames:
            baseList.append(ElementRepresentative.typeFromName \
                            (superClassName, pyXSD))
            
        if not self.containsSchemaBase(baseList):

            baseList.append(SchemaBase)
                
        return tuple(baseList)

    #============================================================
    #
    def getElements(self):
        """
        Returns a blank list. Subclasses uses this function to
        return elements, but this function is called elsewhere
        on all of the types.

        No parameters.
        """
        return []
    
    #============================================================
    #
    def clsFor(self, pyXSD):
        """
        Produces a class for a schema type. In this class, because this function
        is only makes classes for tag types that are subclasses of *XsdType*.
        Adds functions to the class dictionary to get elements and attributes
        later on. Calls getBaseList() to generate the list of bases. SchemaBase
        is in every base list, which will come into play after the class generation.
        Adds the name and the doc string to the dictionary. Adds the instance of
        *PyXSD* to all attributes, elements, and the class dictionary, so it can
        be accessed later on. In future versions, hopefully this operation will
        be done in a metaclass.

        Parameters:

        - `pyXSD`- The *PyXSD* instance.
        """

        bases = self.getBaseList(pyXSD)

        #arrange base so that it always inherits from SchemaBase (SchemaBase inherits from object)
        clsDict = dict([('pyXSD', pyXSD), ('name', self.name), \
                        ('__doc__', self.__doc__)])

        _elementNames_ = []
        
        for element in self.getElements():
            element.pyXSD = pyXSD
            _elementNames_.append(element.name)
            clsDict[element.name] = element

        clsDict['_elementNames_'] = _elementNames_

        def _getElements(cls):
            elements = []
            for elemName in cls._elementNames_:
                element = cls.__class__.__dict__[elemName]
                elements.append(element)
            return elements

        clsDict['_getElements'] = _getElements

        _attributeNames_ = self.attributes.keys()

        clsDict['_attributeNames_'] = _attributeNames_

        def _getAttributes(cls):
            attrs = []
            for attrName in cls._attributeNames_:
                attr = cls.__dict__[attrName]
                attrs.append(attr)
            return attrs

        clsDict['_getAttributes'] = _getAttributes
        for attr in self.attributes.values():
            attr.pyXSD = pyXSD
        clsDict.update(self.attributes)

        try:
            cls = type(self.name, bases, clsDict)
            
        except Exception, e:
            print e
            self.describe()
            print "baseList %s" % repr(self.superClassNames)
            print "baseList %s" % repr(baseList)
            raise  
            return None

        return cls

#======================================================================
#
from pyxsd.schemaBase      import SchemaBase
