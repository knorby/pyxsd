"""
See the transforms manual on the `pyXSD website <http://pyXSD.org/>`_
or in the /doc directory of the installation.

In the transform manual:

- Basic and advanced tutorials on writing transfomers
- An overview of this class
- Basic use instructions
- Documentation on included transform libraries

"""
class Transform (object):
    """
    The base abstract class for all transforms. All methods should
    mix into the usable transform classes. Contains methods to
    retrieve elements from the tree. 
    """
    #=======================================================
    #
    def __init__(self):
        """
        Cannot Initialize a true abstact class!
        """
        raise TypeError, "an abstract class cannot be instantiated"

    
    #=======================================================
    #
    def makeElemObj(self, name):
        """
        Creates a new element that contains the proper tree stucture
        """
        class ElemObjClass(object):
            def __init__(self, name):
                self._children_ = []
                self._attribs_  = {}
                self._name_     = name
                self._value_    = None

        return ElemObjClass(name)

    #=======================================================
    #
    """
    Makes a comment element
    """
    def makeCommentElem(self, comment):
        obj = self.makeElemObj('_comment_')
        obj._value_ = comment
        return obj

    #=======================================================
    #
    #
    """
    The *walk* function walks through the tree structure, and
    runs a provided visitor function on all elements.
    """
    def walk(self, instance, visitor, *args, **kwargs):

        if isinstance(instance, list):
            for item in instance:
                self.walk(item, visitor, *args, **kwargs)

        if isinstance(instance, dict):
            for item in instance.values():
                self.walk(item, visitor, *args, **kwargs)

        if not hasattr(instance, '_children_'):   return
        if not hasattr(instance, '_attribs_'): return

        getName = lambda x: x.name

        elemNames = map(lambda x: x._name_, instance._children_)
        attrNames = instance._attribs_.keys()

        visitor(instance, attrNames, elemNames, *args, **kwargs)

        for el in instance._children_:

            self.walk(el, visitor, *args, **kwargs)
        
    #=======================================================
    #
    def classCollector(self, instance, attrNames, elemNames, collectorDict):
        """
        Visitor function to make a dictionary that associates a class with
        its instances. The class name is the key, and the value is the list
        of associated instances. SEE *getInstancesByClassName*
        """
        className = instance.__class__.__name__
        
        collection = collectorDict.get(className, None)
        
        if collection == None:
            collection = []
            collectorDict[className] = collection
            
        collection.append[instance]
    
    #=======================================================
    #
    def tagCollector(self, instance, attrNames, elemNames, collectorDict):
        """
        A visitor function that is used to make a dictionary that associates
        a tag name with its children. SEE *getAllSubElements*
        """

        for i, tagName in list(enumerate(elemNames)):
            obj = instance._children_[i]
            if obj == None: continue


            collection = collectorDict.get(tagName, None)
        
            if collection == None:
                
                collection = []
                collectorDict[tagName] = collection

            collection.append(obj)
            

    #=======================================================
    #
    def tagFinder(self, instance, attrNames, elemNames, collection, name):
        """
        A visitor function to collect all tags with a particular name, and
        put them into a list. SEE *getElementsByName*
        """
        for i, tagName in list(enumerate(elemNames)):
            if name == tagName:
                obj = instance._children_[i]
                if not obj==None:
                    collection.append(obj)

    #=======================================================
    #
    def getInstancesByClassName(self, root):
        """
        Function to use the *walk* function with the *classCollector*
        visitor function. Used to associate a class name with the class'
        instances.
        """
        collectorDict = {}

        self.walk(root, self.classCollector, collectorDict)

        return collectorDict

    #=======================================================
    #
    def getAllSubElements(self, root):
        """
        Function to use the *walk* function with the *tagCollector*
        visitor function to make a dictionary that associates all
        elements with their sub-Elements.
        """
        collectorDict = {}

        self.walk(root, self.tagCollector, collectorDict)

        return collectorDict

    #=======================================================
    #
    def getElementsByName(self, root, name):
        """
        Function to use the *walk* function with the *tagFinder*
        visitor function to make a list containing all elements
        with a particular name.
        """
        collection = []

        self.walk(root, self.tagFinder, collection, name)

        return collection

    #=======================================================
        


    #=======================================================
    #
    """Finds an element from a given tagName. Returns the first one found,
    or returns None. This function is an alternative to the walk/vistor functions.
    SEE *getElementsByName*
    """
    def find(self, tagName, baseElem):

        if baseElem._name_ == tagName:

            return baseElem

        for child in baseElem._children_:

            returnedElement = self.find(tagName, child)

            if not returnedElement == None:

                return returnedElement

            continue

        return None

    #=======================================================
    #
    """
    Finds all elements with a given tagName. Returns a list of elements or None.
    This function is an alternative to the walk/vistor functions.
    SEE *getElementsByName*
    """
    def findAll(self, tagName, baseElem):

        found = []

        if baseElem._name_ == tagName:

            found.append(baseElem)

        for child in baseElem._children_:

            returnedElement = self.findAll(tagName, child)

            if not returnedElement == None:

                found.extend(returnedElement)

            continue


        if len(found) > 0:
            return found
        
        return None  
