import types
from pyxsd.xsdDataTypes import *

"""
The ElementRepresentative System
================================

The ElementReprsentative system is in charge of converting the schema file
into a collection of classes that represent the types in the schema. The
system takes in the ElementTree representation of the schema file. The pyXSD
class calls ElementTree to parse the schema file in order to keep all calls
to ElementTree in one place. The classmethod `factory` in the ElementRepresentative
class starts the system up, and it is the function that pyXSD calls.
`factory` first reads in an ElementTree element, and then finds a class that
has the same name as the name of element's tag type. `factory` makes an instance of
the class that it finds. In these tag type classes, the `__init__` function is the
first to be called. Each of these classes are subclasses of ElementRepresentative.
These tag specific classes' `__init__` call the `__init__` in the ElementRepresentative
class. The `__init__` function in ElementRepresentative collects general information
about the name and make calls to specifiic functions to help with this operation.
Some of these functions are located in ElementReprsentative exclusively, but many
can also be found in the specific classes. For example, the getName() function
normally calls a version of the function in the ER class, but on some tags,
the ER version of getName() would not generate a unique name in every case, or
it would lack all the needed information on all or some cases. In the classes for
such tags, there is another version of getName() that overrides the ElementRepresentative
version. In general, the most common methods are found in ElementRepresentative,
while methods that are very specific to a tag are found in the class. All the methods needed
to parse the tree are found in the *ER* class. These classes that override must all have the same
arguements in order for the system to work. The system was designed so that it is
each ElementRepresentatives job to:
    
- Collect all information from the element and put it in the appropriate place
    
- Construct ElementRepresentatives for all of the element children

The `__init__` function
-----------------------

In the ER `__init__` function, it makes a call to the processChildren method,
which calls the `factory` on all of the children of an element. Since the ER
`__init__` calls this method, any variable assignment that is needed by the
children must be made before the call to the ER __init__ function. The
children are completely processed before the parent is fully finished. Any
developer should be mindful of this fact when creating or changing an `__init__`
method in a tag type class.

Each `__init__` function has two variables as arguments:

    - `xsdElement`- the ElementTree element that is being converted to an ER
    - `parent`- the parent ER for the tag being processed.

Remember: the tag type classes are called from `factory` so each `__init__`
must follow this same pattern in order to work.

Class Constuction
-----------------

Currently, the classes are constucted in the *xsdType* class. This class is the
base class for *complexType* and *simpleType*. Class constuction is currently
done using a class type factory, which uses the *type* class to build new classes
by supplying a dictionary, tuple of bases, and a name. This class is called from
*pyXSD* and the classes are stored in a dictionary in that class. This method
works well, but it would be better to use a custom metaclass to build these
classes. A metaclass would make it easier to print out the generated classes to a
file so that the programmer could use them for whatever purpose they have, or modify
a class to extend a type in the schema without actually changing the schema. Some
work has been done to make this change, but it would be in later versions if at all.

"""

class ElementRepresentative(object):

    """
    ElementRepresentative is the base class for all of the tag type classes in the
    ElementRepresentative system, and it contains the methods that control movement
    around the tree. This class contains the most general ways to gather information
    from the schema.
    """
    def __init__(self, xsdElement,  parent):
        """
        See the documentation for the ElementRepresentative system at the top of the
        ElementRepresentative module.
        """

        self.xsdElement        = xsdElement

        self.parent            = parent

        self.tagParts          = self.xsdElement.tag.split("}")

        self.tagType           = self.tagParts[1]
        
        self.name              = self.getName()

        self.register(self.name, self)
        
        self.superClassNames   = []

        self.subClassNames     = []

        self.references        = []

        self.referToMe         = []

        self.tagAttributes     = {}

        self.processedChildren = []

        self.layerNum          = self.findLayerNum()

        self.clsName           = self.name 
        self.clsName           = self.clsName[0].upper() + self.clsName[1:]
        
        for name in xsdElement.keys():
            if name == 'name': continue
            
            setattr(self, name, xsdElement.get(name))

            self.tagAttributes[name] = xsdElement.get(name)

            continue

        self.processChildren()
        
    #============================================================
    #
    def __str__(self):
        """
        sets the str() function to print the ER information for a tag in the form: ClassName[TagName]
        """

        return "%s[%s]" % (self.__class__.__name__, self.__dict__.get('name','???'))
    
    #============================================================
    #
    def processChildren(self):
        """
        Calls the `factory` on all of the children of an element.

        No parameters.
        """

        children         = self.xsdElement.getchildren()

        if len(children) == 0: return None
        
        for child in children:

            processedChild = ElementRepresentative.factory(child, self)

            self.processedChildren.append(processedChild)


    #============================================================
    #
    def factory(cls, xsdElement, parent):

        """
        A classmethod. Initializes the tag-specific class for a particular ElementTree schema element.
        See the ER system documentation. 

        usage: ElementRepresentative.factory(xsdElement, parent)

        Parameters:

        - `cls`- The class to operate under (this method is a classmethod). Normally just `ElementRepresentative`.
        - `xsdElement`- the ElementTree element that is being converted to an ER
        - `parent`- the parent ER for the tag being processed. 'None' when being called on the root `schema` element.
        
        """

        clsName = cls.classNameFor(xsdElement, parent)

        if clsName in theVars.keys():

            cls = theVars[clsName]

            return cls(xsdElement, parent)

        # Complain
        print "ElementRepresentative %s is not defined!" % clsName

        return None

    factory = classmethod(factory)

        
    #============================================================
    #
    def describe(self):
        """
        A debugging function that prints out the contents of the dictionary.

        No parameters
        """

        for attrName, value in vars(self).iteritems():

            print " %s -> %s " % (attrName,value)
            
    #============================================================
    #
    def findLayerNum(self):
        """
        Called by the ER `__init__`. Returns an integer tha specifies how deep in the tree
        a particular element is. The `schema` element, which is the root element, is '1'.
        """
        if self.parent == None: return 1

        parentLayerNum  = self.parent.findLayerNum()

        currentLayerNum = parentLayerNum + 1

        return currentLayerNum
       
    #============================================================
    #
    def checkTopLevelType(self):

        """
        Checks to see if an element is at the top-level. Returns True if it is, False if
        it is not. The top level elements are all the children of the root `Schema` tag.

        No parameters
        """
        
        if isinstance(self.parent, Schema): return True

        return False
            

    #============================================================
    #
    def typeFromName(cls, xsdTypeName, pyXSD):
        """
        A classmethod. Used with the clsFor() function in `xsdDataType`. Returns a schema type given
        the type's name. Returns data type classes from *xsdDataTypes* for primitive data types.
        Calls clsFor on ERs.

        Parameters:

        - `cls`- The class that it is working with (it is a classmethod). Usually *ElementRepresentative*.
        - `xsdTypeName`- the name of the type to be returned
        - `pyXSD`- the instance of the *PyXSD* class. Included so it can be used as an argument with the clsFor() method. See clsFor() in *XsdType* for more information.

        """
        
        if not xsdTypeName[:3] == 'xs:':
            getFromNameReturned = cls.getFromName(xsdTypeName)
            if getFromNameReturned:
                return getFromNameReturned.clsFor(pyXSD)
            print "typeFromName() error: getFromName() is returning None for", xsdTypeName
            return None
            
        xsdTypeNameSplit = xsdTypeName.split(':')
        xsdTypeName      = xsdTypeNameSplit[1] 

        if xsdTypeName     == 'string':
            return String

        if xsdTypeName     == 'double':
            return Double

        if xsdTypeName     == 'int' or xsdTypeName == 'integer':

            return Integer
        
        if xsdTypeName     == 'boolean':

            return Boolean

        if xsdTypeName     == 'positiveInteger':

            return PositiveInteger

        if xsdTypeName     == 'ID':

            return ID
        
        if xsdTypeName     == 'IDREF':

            return IDREF
        
        if xsdTypeName     == 'base64Binary':

            return Base64Binary

        print "XsdTypeName Error: %s does not correspond to a class" % xsdTypeName
        return None

    typeFromName = classmethod(typeFromName)
        
    #============================================================
    #
    def addSuperClassName(self, name):
        """
        Adds a base class name to containing type for a particular element.
        Calls come from *Restiction* and *Extension*.

        Parameters:

        - `name`- the name of the base class to add to the base class list
        
        """
        if name == None: return
       
        for superClassName in self.getContainingType().superClassNames: #Prevent Duplicates
            if superClassName == name: return

        self.getContainingType().superClassNames.append(name)
        
    #============================================================
    #
    def getContainingType(self):
        """
        Returns the parent's getContainingType() function. If this function is being called from schema,
        an error is returned. This method is one of a few getContaingType() functions. When a function
        is a containing type, the function should return that ER.

        No parameters.
        """
        if not self.parent == None:
            return self.parent.getContainingType()

        
        print "ElementRepresentative Error: the program encountered an unknown error in getContainingType()"
        print "The class dictionary is as follows:"
        self.describe()

        return None
    
    #============================================================
    #
    def getSchema(self):
        """
        This method returns the parent's getSchema() function. getSchema() should return
        the containing instance when it is a schema tag from the getSchema() function in
        the `schema` class.

        No parameters
        """
        return self.parent.getSchema()
    
    #============================================================
    #
    def getContainingTypeName(self):
        """
        Returns the name of the containingType.

        No parameters.
        """
        theType = self.getContainingType()

        if theType == None: return None

        return theType.name
    
    #============================================================
    #
    def getName(self):
        """
        Returns the name field in the ElementTree element. One of many getName()
        function.

        No parameters.
        """
        name = self.xsdElement.get("name")

        if not name == None: return name

        return None
        
    #============================================================
    #
    def register(cls, name, obj):
        """
        The registry stores all ER objs in a dictionary with their name as a key.
        This is why all names must be unique. Helps find objs. A classmethod, but
        could be changed to staticmethod.

        Parameters:

        - `cls`- The class that is being used (classmethod). Usually Element Representative
        - `name`- The name of the ER obj.
        - `obj`- The ER obj.
        
        """
        if not name in registry:

            registry[name] = []

            registry[name].append(obj)

    register = classmethod(register)
    
    #============================================================
    #
    def getFromName(cls, name):
        """
        Retrieve an entry in the registry by its name. A classmethod, but could be staticmethod.

        Parameters:
        
        - `cls`- The class that is being used (classmethod). Usually Element Representative
        - `name`- The name of the ER obj.
        
        """
        l = registry.get(name,[])

        if l == []:

            try:

                l = registry[name]

            except:
                print "getFromName Error: %s is not a key in the registry" % name
                return None

        if len(l) == 1:

            m =l[0] 

            return m

        #Complain
        print "ElementRepresentative Error: %s" % repr(l)
        
        return None

    getFromName = classmethod(getFromName)


    #============================================================
    #
    def tryConvert(variable):
        """
        Tries to convert a variable from a string in the xsd to a python value.
        Returns the entry if it cannot be converted.

        Parameters:

        - `variable`- the value of a variable that the method is trying to convert.

        """
        try:
            return int(variable)
        except:
            pass
        try:
            return float(variable)
        except:
            pass
        if variable == 'false': return False
        if variable == 'true': return True
        return variable

    tryConvert = staticmethod(tryConvert)

    #============================================================
    #
    def classNameFor(cls, xsdElement, parent):
        """
        returns the name of the class that the factory should find. A classmethod.

        usage: ElementRepresentative.classNameFor(xsdElement, parent)

        Parameters:

        - `cls`- The class to operate under (this method is a classmethod). Comes from `factory`
        - `xsdElement`- the ElementTree element that is being converted to an ER
        - `parent`- the parent ER for the tag being processed. 'None' when being called on the root `schema` element.
        
        """
        
        clsName = xsdElement.tag

        tagParts = xsdElement.tag.split("}")
        
        if len(tagParts) == 2:

            clsName = tagParts[1]

            clsName = clsName[0].upper() + clsName[1:]

        return clsName
    
    classNameFor = classmethod(classNameFor)

    #============================================================
    #

#Imports all of the tag-specific classes
tags = ['element', 'attribute', 'schema', 'xsdType', 'extension', 'simpleType',
        'complexType', 'annotation', 'attributeGroup', 'documentation', 'restriction', 'sequence',
        'choice', 'list', 'simpleContent', 'complexContent', 'enumeration', 'pattern', 'length',
        'minInclusive', 'maxInclusive', 'minExclusive', 'maxExclusive']

for tag in tags:
    tagUpper = tag[:1].upper() + tag[1:]
    exec('from %s import %s' % (tag, tagUpper))
    

theVars        = vars()
registry       = {}


