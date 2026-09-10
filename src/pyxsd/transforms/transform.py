"""Transform framework.

See the transform authoring guide in the documentation for tutorials
on writing transformers, an overview of this class, basic use
instructions, and documentation on the included transform libraries.
"""

import abc


class Transform(abc.ABC):
    """The base abstract class for all transforms.

    All methods should mix into the usable transform classes. Contains
    methods to retrieve elements from the tree.

    Subclasses must accept the tree root in their ``__init__``; that
    makes the class abstract until it does, so framework-only
    subclasses (like :class:`~pyxsd.transforms.displayer.Displayer`)
    cannot be instantiated by accident.
    """

    @abc.abstractmethod
    def __init__(self, root):
        """Initialize the transform with the root of the instance tree.

        Concrete transforms must override this and store the root (or
        whatever subset of the tree they operate on).
        """

    def makeElemObj(self, name):
        """Creates a new element that contains the proper tree
        structure.
        """

        class ElemObjClass:
            def __init__(self, name):
                self._children_ = []
                self._attribs_ = {}
                self._name_ = name
                self._value_ = None

        return ElemObjClass(name)

    def makeCommentElem(self, comment):
        """Makes a comment element."""
        obj = self.makeElemObj("_comment_")
        obj._value_ = comment
        return obj

    def iter_tree(self, instance):
        """Yield every tree node at or below ``instance``, depth-first.

        Lists (and tuples) are descended into item by item and
        dictionaries by value; anything without both ``_children_`` and
        ``_attribs_`` is skipped. Each yielded node is visited before
        its children (pre-order). This generator powers :meth:`walk`
        and is also the supported way to iterate a tree directly::

            for node in transform.iter_tree(root):
                ...

        - ``instance``: a tree node, or a list/dict of them.
        """
        if isinstance(instance, (list, tuple)):
            for item in instance:
                yield from self.iter_tree(item)
        elif isinstance(instance, dict):
            for item in instance.values():
                yield from self.iter_tree(item)
        elif hasattr(instance, "_children_") and hasattr(instance, "_attribs_"):
            yield instance
            for child in instance._children_:
                yield from self.iter_tree(child)

    def walk(self, instance, visitor, *args, **kwargs):
        """Walks through the tree structure and runs a provided visitor
        function on all elements.

        The visitor is called as ``visitor(node, attrNames, elemNames,
        *args, **kwargs)`` where ``node`` is the tree node being
        visited, ``attrNames`` is the list of its attribute names, and
        ``elemNames`` is the list of its children's names. Traversal is
        driven by :meth:`iter_tree`.
        """
        for node in self.iter_tree(instance):
            elemNames = [c._name_ for c in node._children_]
            attrNames = list(node._attribs_.keys())
            visitor(node, attrNames, elemNames, *args, **kwargs)

    def classCollector(self, instance, attrNames, elemNames, collectorDict):
        """Visitor function to make a dictionary that associates a class
        with its instances.

        The class name is the key, and the value is the list of
        associated instances. See ``getInstancesByClassName``.
        """
        className = instance.__class__.__name__
        collection = collectorDict.get(className, None)
        if collection is None:
            collection = []
            collectorDict[className] = collection
        collection.append(instance)

    def tagCollector(self, instance, attrNames, elemNames, collectorDict):
        """A visitor function that is used to make a dictionary that
        associates a tag name with its children.

        See ``getAllSubElements``.
        """
        for i, tagName in enumerate(elemNames):
            obj = instance._children_[i]
            if obj is None:
                continue
            collection = collectorDict.get(tagName, None)
            if collection is None:
                collection = []
                collectorDict[tagName] = collection
            collection.append(obj)

    def tagFinder(self, instance, attrNames, elemNames, collection, name):
        """A visitor function to collect all tags with a particular name
        and put them into a list.

        See ``getElementsByName``.
        """
        for i, tagName in enumerate(elemNames):
            if name == tagName:
                obj = instance._children_[i]
                if obj is not None:
                    collection.append(obj)

    def getInstancesByClassName(self, root):
        """Uses the ``walk`` function with the ``classCollector``
        visitor function to associate a class name with the class's
        instances.
        """
        collectorDict = {}
        self.walk(root, self.classCollector, collectorDict)
        return collectorDict

    def getAllSubElements(self, root):
        """Uses the ``walk`` function with the ``tagCollector`` visitor
        function to make a dictionary that associates all elements with
        their sub-elements.
        """
        collectorDict = {}
        self.walk(root, self.tagCollector, collectorDict)
        return collectorDict

    def getElementsByName(self, root, name):
        """Uses the ``walk`` function with the ``tagFinder`` visitor
        function to make a list containing all elements with a
        particular name.
        """
        collection = []
        self.walk(root, self.tagFinder, collection, name)
        return collection

    def find(self, tagName, baseElem):
        """Finds an element from a given tagName.

        Returns the first one found, or returns None. This function is
        an alternative to the walk/visitor functions. See
        ``getElementsByName``.
        """
        if baseElem._name_ == tagName:
            return baseElem
        for child in baseElem._children_:
            returnedElement = self.find(tagName, child)
            if returnedElement is not None:
                return returnedElement
        return None

    def findAll(self, tagName, baseElem):
        """Finds all elements with a given tagName.

        Returns a list of elements or None. This function is an
        alternative to the walk/visitor functions. See
        ``getElementsByName``.
        """
        found = []
        if baseElem._name_ == tagName:
            found.append(baseElem)
        for child in baseElem._children_:
            returnedElement = self.findAll(tagName, child)
            if returnedElement is not None:
                found.extend(returnedElement)
        if found:
            return found
        return None
