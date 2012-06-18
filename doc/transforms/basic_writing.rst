=========================
Transforms: Basic Writing
=========================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Wed, 30 Aug 2006
:Description: A brief tutorial and set of redirects to other documentation to help users write transform classes

Contents
========

- `Related Files`_
    - `Transform Basics`_
    - `Tree Structure`_
    - `Example Templates`_
- `Introduction`_
    - `Basic Rules`_
    - `About the Templates`_
- `Documenting Transforms`_
    - `General Structure for a Transform`_
    - `General Structure for a Library`_
    - `Restructured Text`_
- `Copyrighting Transforms`_

Related Files
=============

Transform Basics
----------------  

- **basic_use** in doc/transforms/
    - **sampleTransformFile** in examples/

Tree Structure
--------------

- **writer_api** in doc/

Example Templates
-----------------

- **about_examples** in examples/
- **transform_template_fromTransform.py** in examples/
- **transform_template_fromLibrary.py** in examples/
- **transform_template_library.py** in examples/
    
Introduction
============

If you do not know what a transform does or how it works, read the file basic_use first.
If you are not familiar with Python, then you should not try to write a transform.
If you want to learn Python, the Python_ website has a good
Python tutorial.

.. _Python: http://python.org/ 

Basic Rules
-----------

Since the program must load transforms given just a name and a set of arguments to
pass in, transform classes are restricted to a basic structure. Every transform must
follow the following rules in order to function correctly:

- Every transform must consist of one class that uses the same name as the file that it is in, except the first letter must be capitalized.
- Every transform must specify a `__call__` function. The arguments to this function must be the ones that you want specified in the transform call. 
- Every transform must specify a `__init__` function. The function must take the rootInstance as its ONLY argument. The variable name you use does not matter, but there can be only one variable.
- The `__call__` function must return the root instance if you want to send the tree to the writer and/or send the tree to other transforms.  

About the Templates
-------------------

The templates list in the `Example Templates`_ section show how a transform class should
look. The **about_examples** file explains how to use these templates, and gives a
good outline of the procedures that a transform developer should follow. Consider these
files part of this tutorial. 

Documenting Transforms
======================

Unless you plan to not distribute your transform to anyone, you should write up some
documentation for your transform. By the time you are reading this document, there 
should be a form on the `pyXSD website`_ that allows you to easily
create documentation for a transform or transform library. If this site is not up or
you wish to create the documentation yourself, then you should follow the general
structure outlined here. You might look at the documentation for the included transforms.

General Structure for a Transform
---------------------------------

You should include the following in your documentation in an order similar to the following for a transform:

1. The Transform name as the title of the document
2. Your personal information such as your name, and your email and/or website
3. The date you last modified the documentation, and a short description of your transform.
4. The copyright (see `Copyrighting Transforms`_)
5. The inheritance structure in a diagram, going through all the libraries you use. Place Transform at the top.
6. Any other information relevant to the distribution of you transform
7. A sample call, with the argument names as argument, and make them keyword arguments with the default value, if they have one, as the value
8. The argument name, along with the type(s) it can be, and a brief description
9. A description of what your transform does and what it returns (in terms of root, or if it is not root, what it returns)

General Structure for a Library
-------------------------------

You should include the following in your documentation in an order similar to the following for a transform library:

1. The Transform Library name as the title of the document
2. Your personal information such as your name, and your email and/or website
3. The date you last modified the documentation, and a short description of your transform library. 
4. The copyright (see `Copyrighting Transforms`_)
5. The inheritance structure in a diagram, going through any other libraries you use. Place Transform at the top.
6. Any other information relevant to the distribution of you library
7. Every function, with the init include function at the top, in the following structure:
   i. Function name and arguments. Write the argument names, and make any with a default value keyword arguments equal to the default value
   ii. Write a description of what it does and what it returns. Describe the arguments, including what type they must be, very well.
8. Write a longer description of what your library should be used for

Restructured Text
-----------------
 
It is recommended that you write your documentation in restructured text. Restructured
text is a very readable mark up language, which can easily be converted to HTML or 
other formats. The Doc String API notes in the source and the regular documentation
is written in Restructured text. If you would like to learn more about Restructured
Text or if you would like to view tutorials for it, the documentation is located on
the website for the python program docutils <http://docutils.sourceforge.net/rst.html>,
which introduced the language. The mark up language is ideal, because it is so easy
to read, it is easy to convert it to other formats, it can be read by plone on the
`pyXSD website`_, it can easily be included in python source files, and it can be read 
by many documentation programs. Soon, the `pyXSD website`_ should include tools to
make a RST documentation file through a web form, and a form to build RST into 
a particularly formatted html file.
 
Copyrighting Transforms
=======================

Unless you want to include your transform in the official pyXSD package, you can
release transforms under your own copyright. It is recommended that you release
your transforms open source, and that you use a generally permissive license
such as the BSD or MIT licenses. Unless you wish to make the package part of
pyXSD, in which case your transform will be treated like any other contribution
(SEE **contributions** on the `pyXSD website`_) and be under the pyXSD license,
you are responsible for all aspects of your transform. In the near future, if 
not at the time you are reading this, there may be an area on the `pyXSD website`_
that lists and links to transforms on the transform developers request. But please
understand that the official pyXSD project takes absolutely no responsibility for
these transforms. 

You might include your license at the top of your code (if it is open source clearly),
or include a file with your license along with your transform class files. You should
name this file something that could be recognized to be associated with your transform
files in a directory with other transform files in it.  

.. _`pyXSD website`: http://pyxsd.org/

