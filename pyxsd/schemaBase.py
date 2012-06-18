import sets

class SchemaBase(object):
    """
    Serves as the base class for all schema type classes created. The pythonic instance tree is built from this class.
    This class also contains the means to do non-fatal parser error checking. A little bit of the work this class does
    is also done in pyXSD. The schema and xml file do not line up perfectly. The top level element in the schema and the
    schema tag both contain information relevent to the top-level tag in the XML. For this reason, the tree
    building/checking must be started in the same location the method `makeInstanceFromTag` is called in this class.
    """

    #=======================================================
    #
    def __init__(self):
        """
        Creates the instances that are in the tree. These objects are initialized from within SchemaBase. 

        No parameters
        """
        self._children_  = []

        self._value_     = None

    #=======================================================
    #
    def makeInstanceFromTag(cls, elementTag):
        """
        A classmethod. It takes in a schema type class and its corresponding xml element.
        It then instanciates the class. It adds a name from the name in the xml element,
        and then it hands the instance and the element to other methods to add attributes,
        elements, and values to this instance. It adds these according to the schema classes,
        and not the element. A non-fatal (when possible) error is raised when the xml element
        does not correspond to the schema class.

        Parameters:

        - `cls`- The schema type class to use. NOTE: since this function is a classmethod, `cls` is the first argument, not the instance of the class.
        - `elementTag`- The xml element that correspond to `cls` 
        
        """

        instance           = cls()
        
        instance._name_    = elementTag.tag.split('}')[-1]
        
        cls.addAttributesTo(instance, elementTag)
        cls.addElementsTo  (instance, elementTag)
        cls.addValueTo     (instance, elementTag)

        return instance

    makeInstanceFromTag = classmethod(makeInstanceFromTag)
        
    #=======================================================
    #
    def addAttributesTo(cls, instance, elementTag):

        """
        A classmethod. Called by `makeInstanceFromTag()`. Adds attributes according to the schema by calling
        `getAttributesFromTag()`. The attributes are then checked.

        parameters:

        - `cls`- The schema type class to use. NOTE: since this function is a classmethod, `cls` is the first argument, not the instance of the class.
        - `instance` - The instance of `cls` that is having attributes added to it.
        - `elementTag`- The xml element that correspond to `cls` 

        """

        tagsUsed = instance.getAttributesFromTag(elementTag)

        instance.checkAttributes(tagsUsed, elementTag)

    addAttributesTo = classmethod(addAttributesTo)


    #=======================================================
    #
    def getAttributesFromTag(self, elementTag):

        """
        Adds attributes to the *_attribs_* dictionary in the instance. Only attributes in
        the type classes are added. All of the attribute values are validated against
        descriptors in the Attribute class in ElementRepresentatives. The only exception
        to this proceedure is for namespace and schemaLocation tags, as the program
        currently does not have any mechanism to actually check these.

        Parameters:

        - `elementTag`: the xml element that the instance represents
        
        """

        self._attribs_ = {}

        usedAttributes = []

                
        
        for attr in elementTag.attrib.keys():
            if 'xmlns' in attr or 'xsi:' in attr:

                setattr(self,attr,elementTag.attrib[attr])

                usedAttributes.append(attr)

                self._attribs_[attr] = elementTag.attrib[attr]

        for name in self.descAttributeNames():

            if name in elementTag.attrib.keys():

                setattr(self,name,elementTag.attrib[name])

                usedAttributes.append(name)

                self._attribs_[name] = elementTag.attrib[name]

        return usedAttributes

    #=======================================================
    #
    def addElementsTo(cls, instance, elementTag):

        """
        A classmethod. Checks order on the child elements, with different functions for `sequences` and `choices`.
        Iterates through all the elements specified in the class of the schema, and matches these elements with the
        elements from the xml. Redirects elements that are primitive types (integer, double, string, and so on) to another function.
        Calls makeInstanceFromTag() on all the children.

        Parameters:

        - `cls`- The schema type class to use. NOTE: since this function is a classmethod, `cls` is the first argument, not the instance of the class.
        - `instance` - The instance of `cls` that is having elements added to it.
        - `elementTag`- The xml element that correspond to `cls`
        
        """

        subElements = elementTag.getchildren()

        getSubElementName = lambda x: x.tag.split('}')[-1]

        elemDescriptors = instance._getElements()

        if len(subElements) == 0: # This element has no children
            return

        if elemDescriptors[0].sOrC == "sequence":

            cls.checkElementOrderInSequence(elemDescriptors, subElements)

        if elemDescriptors[0].sOrC == "choice":

            cls.checkElementOrderInChoice(elemDescriptors[0], subElements)
            
        #-----------------------------
        
        for descriptor in elemDescriptors:

            descriptorName = descriptor.name

            for subElement in subElements:

                subElementName = getSubElementName(subElement)
                
                if descriptorName == subElementName:

                    subElCls    = descriptor.getType()
                    
                    if subElCls == None: #An Error Message
                        print "Parser Error: There is no type in the schema that corresponds to the type stated in the %s element" \
                              % descriptorName 
                        continue
                    
                    #-----------------------------
                    #for elements with primitive types

                    if not issubclass(subElCls, SchemaBase):
                        subInstance        = cls.primitiveValueFor(subElCls, subElement)
                        subInstance._name_ = subElementName
                        instance._children_.append(subInstance)
                        setattr(instance, subElementName, subInstance)
                        continue

                    #-----------------------------
                    
                    subInstance        = subElCls.makeInstanceFromTag(subElement)
                    subInstance._name_ = subElementName
                    instance._children_.append(subInstance)
                    continue
                
        return instance
    
    addElementsTo = classmethod(addElementsTo)
            
    #=======================================================
    #
    def addValueTo(cls, instance, elementTag):

        """
        Checks to see if the tag has a value, and assigns it to the element instance if it does.
        Uses the ElementTree function `.text` to retrieve this information from the tag.

        Parameters:

        - `cls`- The schema type class to use. NOTE: since this function is a classmethod, `cls` is the first argument, not the instance of the class.
        - `instance` - The instance of `cls` that is having values added to it.
        - `elementTag`- The xml element that correspond to `cls`

        """

        lineStrip = lambda x: x.strip().strip('\n').strip('\t') 

        if elementTag.text:

            dataEntry = None

            instance._value_ = []

            if '\n' in sets.Set(elementTag.text.rstrip('\n')):

                dataEntry = elementTag.text.split('\n')

                for line in dataEntry:
                    
                    line = lineStrip(line)

                    if len(line) == 0:

                            continue
                        
                    instance._value_.append(line)

            if len(instance._value_) == 0:
                instance._value_ = None

    addValueTo = classmethod(addValueTo)

    #=======================================================
    #
    def checkElementOrderInChoice(cls, elemDescriptor, subElements):
        """
        A classmethod. Checks to see that elements in a choice field, which is specified in the schema, follow
        the rules of such a field. Gets minOccurs and maxOccurs from the choice element in the schema, and
        checks the number of elements from there.

        Parameters:

        - `cls`- The schema type class in use. NOTE: since this function is a classmethod, `cls` is the first argument, not the instance of the class.
        - `subElements`- All of the children of an element that is being processed in addElementsTo().

        """
        
        minOccurs = elemDescriptor.getMinOccurs()
        if minOccurs < 0:
            print "Parser Error: the value of 'minOccurs' in %s must be greater than or equal to zero." % cls.name
            print "The program will assign minOccurs the default vaule of 1 and attempt to proceed."
            print
            minOccurs = 1
            

        maxOccurs = elemDescriptor.getMaxOccurs()
        if minOccurs < 0:
            print "Parser Error: the value of 'maxOccurs' in %s must be greater than or equal to zero." % cls.name
            print "The program will assign minOccurs the default vaule of 1 and attempt to proceed."
            print
            maxOccurs = 1
        

        if len(subElements) < minOccurs:

            print "Parser Error: the program cannot find any elements in the xml that are specified in the choice field for", cls.name
            print

        elif len(subElements) > maxOccurs:

            print "Parser Error: the parser found too many elements for a choice element in %s." % cls.name
            print "This choice element can only have one element in it."
            print
        

        return
            
    checkElementOrderInChoice = classmethod(checkElementOrderInChoice)

    #=======================================================
    #
    def checkElementOrderInSequence(cls, descriptors, subElements):

        """
        A classmethodChecks the element order in sequence fields to make sure that the order specified in the schema is preserved
        in the xml. Raises non-fatal errors when a problem is found. Checks minOccurs and maxOccurs on each element as well.

        Parameters:
        
        - `cls`- The schema type class in use. NOTE: since this function is a classmethod, `cls` is the first argument, not the instance of the class.
        - `descriptors`- a list of schema-specified elements that define parameters for an element. Called `descriptors` because the program takes advantage of descriptors in python to help check the data. These descriptors are in the Element class in elementRepresentatives. 
        - `subElements`- All of the children of an element that is being processed in addElementsTo(). Correspond to elements in `descriptors`
        
        """

        descriptorNames = map(lambda x: x.name, descriptors)
        subElementNames = map(lambda x: x.tag.split('}')[1], subElements)
        if not len(descriptorNames) == len(descriptors):
            print "Name descriptor mismatch"
            
        for index in range(0, len(descriptors)):
            descriptor = descriptors[index]
            dname      = descriptorNames[index]
            
            
            count, subElementNames = cls.consume(dname, subElementNames)

            if count == 0:
                if descriptor.getMinOccurs() == 0:
                    if  not dname in subElementNames:
                        continue
                print "Parser Error: Order Error - Expected element name '%s' in different position." % dname  
                print
                continue
            
            if count < descriptor.getMinOccurs():
                #complain
                print "Parser Error: The Element '%s' in '%s' occurs less" % (dname, cls.name)
                print "than the specified number of minOccurs (%i) in the schema." % descriptor.getMinOccurs()
                print "Note: it is possible that there is a problem with the order of"
                print "elements and not the minOccurs value."
                print
                continue

            if count > descriptor.getMaxOccurs():
                #complain
                print "Parser Error: The element '%s' in '%s' occurs more" % (dname, cls.name)
                print " than the specified number of maxOccurs in the schema."
                if descriptor.getMaxOccurs()==1:
                    print "Your maxOccurs value is 1, which is the default value."
                    print "Perhaps you meant to assign this vairablea different value?"
                print
                continue
            
    checkElementOrderInSequence = classmethod(checkElementOrderInSequence)

    #=======================================================
    #
    def consume(cls, dname, subElements):
        """
        A classmethod. Used to check the number of times an element type in the schema is used with the xml elements.  Used by checkElementOrderInSequence().

        Parameters:

        - `cls`- The schema type class in use. NOTE: since this function is a classmethod, `cls` is the first argument, not the instance of the class.
        - `dname`- The name of the descriptor that is currently being checked.
        - `subElements`- the list of subElements being checked.
        
        """
        count = 0
        while len(subElements) > 0 and subElements[0] == dname:
            subElements = subElements[1:]
            count+=1

        return count, subElements

    consume = classmethod(consume)

    #=======================================================
    #
    def primitiveValueFor(cls, subElCls, subElement):
        """
        A classmethod. Used to check and assign primitive values to an instance. called by addElementsTo().
        NOTE: this class may not work correctly for all elements with primitive data types.
        If you find an error in this method or any other error in the program, please submit
        this error and the appropiate correction on the `pyXSD website <http://pyxsd.org>`_. 

        Parameters:
        
        - `cls`- The schema type class in use. NOTE: since this function is a classmethod, `cls` is the first argument, not the instance of the class.
        - `subElCls`- the schema type class that corresponds to the subElement that is being processed.
        - `subElement`- The subElement that has a primitive data type.
        
        """
        
        dataTypeChildren = subElement.getchildren()

        dataTypeText   =  subElement.text
        
        dataTypeAttrib = subElement.items()
        
        if dataTypeText == None and len(dataTypeAttrib) == 0 \
               and len(dataTypeChildren) == 0:
            
            dataTypeVal = True
            
        elif dataTypeText:
            
            dataTypeVal = dataTypeText
            
        elif len(dataTypeAttrib) == 1:

            dataTypeVal = dataTypeAttrib[0][1]
            
        elif len(dataTypeChildren) == 1:
            
            dataTypeVal = dataTypeChildren[0]
            
        else:
            
            print "An error occured while reading the data in the %s element." \
                  % subElementName

            return
                   
        dataTypeValInst = subElCls(dataTypeVal)

        dataTypeValInst._attribs_ = subElement.attrib

        dataTypeValInst._value_   = dataTypeText

        dataTypeValInst._children_ = dataTypeChildren

        dataTypeVal

        return dataTypeValInst

    primitiveValueFor = classmethod(primitiveValueFor)
    #=======================================================
    #
    def addBaseDescriptors (cls):
        """
        Adds attribute descriptors from classes that are bases to the current class.
        Does this recursively down the list of bases. Everything returned as a dictionary.
        A classmethod.

        Parameters:

        - `cls`- The schema type class in use. NOTE: since this function is a classmethod, `cls` is the first argument, not the instance of the class.

        """

        descriptors = {}
        for key, value in vars(cls).iteritems():
            if isinstance(value, Attribute):
                descriptors[key] = value

        for bcls in cls.__bases__:
            if hasattr(bcls,'addBaseDescriptors'):
                bclsDescriptors = bcls.addBaseDescriptors()
                for key, value in bclsDescriptors.iteritems():
                    if key in descriptors.keys():
                        continue
                    descriptors[key] = value

        return descriptors
    addBaseDescriptors = classmethod(addBaseDescriptors)
    #=======================================================
    #
    def descAttributes(self):
        """
        Returns a dictionary of the  descriptor attributes. These attributes are from the schema and use descriptors,
        which are specified in the Attribute class in elementReprsentatives, that help check element attribute values.
        Uses lazy evulation by storing the descriptor attributes in a variable  called '_descAttrs_', which it returns
        if this variable is specified.

        No parameters.
        """
        if '_descAttrs_' in self.__dict__.keys():
            return self._descAttrs_
        attrs = {}
        for key, value in vars(self.__class__).iteritems():
            if isinstance(value, Attribute):
                attrs[key] = value

        for base in self.__class__.__bases__:
            if hasattr(base, 'addBaseDescriptors'):
                bclsDescriptors = base.addBaseDescriptors()
                for key, value in bclsDescriptors.iteritems():
                    if key in attrs.keys():
                        continue
                    attrs[key] = value
                
        self.__dict__['_descAttrs_'] = attrs
                        
        return attrs

    #=======================================================
    #
    def descAttributeNames(self):
        """
        Returns a list that has all of the names of attribute descriptors. Calls descAttributes(), and returns a list of the keys from
        that dictionary.
        """
        return self.descAttributes().keys()
        
    #=======================================================
    #
    def checkAttributes(self, usedAttrs, elementTag):
        """
        Checks to see that required attributes are used in the xml, and does other such checks on the attributes.
        Note: the attribute descriptors check the values in element attributes.

        Parameters:

        - `usedAttrs`- a list containing the names of attributes that were put into the instance.
        - `elementTag`- The ElementTree tag for the instance that is being checked.
        
        """

        descriptorAttributes     = self.descAttributes()

        descriptorAttributeNames = self.descAttributeNames()

        attrInElementTag = list(elementTag.attrib.keys())

        if len(usedAttrs) > len(attrInElementTag):
            print "Parser Error: For an unknown reason, in %s, the program parsed more attributes than there are in the XML file." \
                  %self.__class__.__name__
        elif len(usedAttrs) < len(attrInElementTag):
            print "Parser Error: Not all attributes in the XML file in %s were parsed." %self.__class__.__name__
            print "Attributes not processed:" 
            for attrET in attrInElementTag:   
                if not attrET in usedAttrs:   
                    print "   ", attrET
        for descriptorAttrName in descriptorAttributeNames:
            found = False
            attrUse = descriptorAttributes[descriptorAttrName].getUse()
            for usedAttr in usedAttrs:
                if usedAttr == descriptorAttrName:
                    found = True
            if attrUse == 'required' and not found:
                print "Parser Error: the %s in the %s element is required but was not found."\
                      %  (self.descriptorAttrName, self.tagName)
                


    #=======================================================
    #
    def dumpCls(cls):

        """
        For debugging purposes only. Prints out the contents of a class. A staticmethod.

        parameters:

        - `cls`- The class to dump the contents of. NOTE: this is the only arguement, since it is a staticmethod.
        
        """

        print " In dumpCls[%s] bases = %s " % \
              (cls.__name__, cls.__bases__)
        
        for key,value in cls.__dict__.iteritems():
            print "   %s - %s" % (key, repr(value))

    dumpCls = staticmethod(dumpCls)

    #=======================================================
    #

import elementRepresentatives.elementRepresentative 
from elementRepresentatives.attribute import Attribute

                             

    



