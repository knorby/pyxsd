=========================
Using the Transform Class
=========================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Tues, 5 Sept. 2006
:Description: An overview of the Transform class
:Copyright: pyXSD License

.. contents:

Introduction
============

Every transform must, at some level, inherit from the *Transform* class. A transform
class might not need to explicitly name it as a super class, but in these cases, 
some base class of the transform must have the *Transform* class as a base class.
The *Transform* class contains basic functionality to access the tree, and gather
data from it. It also contains methods to make an element object with the correct
structure. Any method that is added to the *Transform* class must be relevant to all
indivudal transform classes. In other words, these methods must only work on the
most basic structure of tree. Since the *Transform* class is abstract, the `__init__`
method will raise an error when it is called. 

The Walk/Visitor methods
========================
The `walk` method parses through the tree, and runs a specified visitor function on all
elements within it. These functions follow the classic walk/visitor arrangement, which
are normally used to parse through a directory structure; however, the tree structure
and a directory tree structure share many similarities, so these functions are very
similar to these more common functions. The *Transform* class includes a few standard
visitor functions, but a user can specify their own.

The walk method
---------------
The `walk` method takes in an element instance, gets its name, children, and attributes
from the instance, calls the visitor function on the instance, which is passed the instance,
the attribute names, the names of its children, and the args and kwargs, which usually
include some of the storage variables. `walk` is then called on the children. The method's
arguments are as follows:

 instance : instance of a schema class
     The element object that the `walk` method will call the visitor function on
 visitor : specialized function
     The function that `walk` will call on every element in the tree
 args/kwargs : (list)/(dictionary)
     Allows for other variables to be placed in the walk function. These are most commonly a variable which stores specific elements, but these variables could be set to anything
 
classCollector visitor function
-------------------------------
The `classCollector` visitor function to make a dictionary that associates a class with
its instances. The class name is the key, and the value is the list of associated instances.
It has the standard set of arguments (instance, attrNames, and elemNames in that order)
as well as collectorDict, which must be passed into the `walk` function as a blank dictionary when the `walk` function is first
called. This visitor function is used by the `getInstancesByClassName` method.

tagCollector visitor function
-----------------------------
The `tagCollector` method is a  visitor function that is used to make a dictionary that
associates a tag name with its children. The children are stored in a list are are the
values in the dictionary, and the tag names are the keys. It has the standard set of arguments
(instance, attrNames, and elemNames in that order) as well as collectorDict, which must be
passed into the `walk` function as a blank dictionary when the `walk` function is first
called. This visitor function is used by the `getAllSubElements` method.

tagFinder visitor function
--------------------------
The `tagFinder` method is a visitor function to collect all tags with a particular name,
and put them into a list. It has the standard set of arguments (instance, attrNames,
and elemNames in that order), as well as collection, which is a list which must be passed
into the `walk` function as a blank list, and name, which is the name of the tag that
it should put into the list and it must be passed in as a string containing this name
when `walk` is first called. This visitor function is used by the `getElementsByName` 
method.

Functions to call walk/visitor functions
========================================
All of these functions have been identified by the visitor function that these functions
are associated with. All of them take `root`, which is the root instance of the tree (or
the place that you want to start the walk/visitor functions from) as the first argument.

getInstanceByClassName
----------------------
Function to call the `classCollector visitor function`_. 
No Arguments in addition to `root`. 

getAllSubElements
-----------------
Function to call the `tagCollector visitor function`_. 
No Arguments in addition to `root`. 

getElementsByName
-----------------
Function to call the `tagFinder visitor function`_.
In addition to `root`, this function has the `name`
variable as an argument, which is the name of the
tag you are trying to find.

Find functions: alternatives to the walk/visitor functions
==========================================================
These functions do similar things to the `getElementsByName`
function, except without the use of a the walk/visitor functions.
It is recommended that you use the walk/visitor functions,
but these functions do work. Both have the same arguments,
which are as follows:

 tagName : string
     the name of the element that the function should find
 baseElem : Element Object
     The element on the tree that you wish to start the find from. Normally the root element.

find
----
This function finds an element from a given tagName. It returns the
first element with a matching name  that it finds, or returns None.
Quicker than the `findAll` function, but less precise. 

findAll
-------
Finds all elements with a given tagName. Returns a list of elements or None.

Other Functions in the Transform class
======================================
makeElemObj
-----------
Makes an instance of a class that contains a blank list called `_children_`,
a blank dictionary called `_attribs_`, a string `_name_`, which is set to 
whatever name is specified as an argument to the function, and a variable 
`_value_` set to None. These variables make up the basic structure that
the writer uses. This function is used to make a new element whenever one is
needed in a program. The only argument is `name`, which is a string that is
to be used as a name for the instance. 

makeCommentElem
---------------
Uses `makeElemObj` to make a comment element. A comment element does not have
any children or attributes, its name is always set to `_comment_`, and its value
is the text in the comment. The only argument is `comment`, which is the text of
the comment.

