=======================
PyXSD 0.1 Example Files
=======================

:Author: Kali Norby <kali.norby@gmail.com>
:Date: Wed, 30 Aug 2006
:Description: An explaination of the example files in the example directory

.. contents:: Directory of Files

File Explainations
==================

sampleTransformFile
-------------------

This file is an example of a transform call file. Normally, users can enter 
a list of transforms that the program should perform one of two ways, when
using the program at the command line. The user can enter the list of
transforms to perfrom directly at the command line, seperating each
transform with a **>** symbol. In some cases, the user might find this
method perfectly acceptable, but often times, a user might want to enter
in a lengthy list of calls in a place other than the command line. The user
might also want to save a list of calls for future use. For these reasons,
the user can also save the list of transform calls in a seperate file. These
files do not require any quotations or special symbols to seperate calls.
The user need only place one call per line. In order to use a transform file,
the user should use the **-T** at the command line, followed by the transform
filename.

The example file is a simple, but typical, use of transforms for a scientific
application. The "ExpandCell" and "SphereCutter" classes enable a user to
expand and change the shape of crystal that he or she is modeling. These two
classes where written for a particular schema and a particular layout that is
specific to them, so these classes do not have any general use. This example
includes the class "PrintData" in between the two classes just mentioned. This
particular class simpily prints out the tree at a particular point to a file
or to the screen using stdout. This class can be used with any transform. It
was created to help debug transforms, but it could be used for multiple other
purposes.
      
transform_template_fromTransform.py
-----------------------------------

This example is a template for transfroms classes. Every transform class
must include the same `__init__` file, although developer can *add* to the 
template, they cannot *change* what is already there including the arguments.
The user must also include the `__call__` method. The arguments for the 
`__call__` method correspond to the arguments specified at the beginning
of the program. When documenting the transform, developers should reference
these arguments in the argument section of the documentation.

In this example, the class inherits from the Transform class, which all
transform classes must be subclasses of. Normally, however, the transform
inherits from a specialized library.
Many transforms often do similar things. If your transform is so simple
that there is no generalized function that you could extract from it, then 
use *Transform* as a base. If you can extract some general function from your
class, then  put it into a library, so you can often times simplify future
work for yourself and others. (SEE transform_template_fromLibrary.py_)

transform_template_fromLibrary.py
---------------------------------

The first example is a template for transfroms classes. Every transform class
must include the same `__init__` file, although developer can *add* to the 
template, they cannot *change* what is already there including the arguments.
The user must also include the __call__ `method`. The arguments for the 
`__call__` method correspond to the arguments specified at the beginning
of the program. When documenting the transform, developers should reference
these arguments in the argument section of the documentation.

In this example, the program uses a custom transform library. Instead of
inheriting from *Transform* (SEE transform_template_fromTransform.py_),
this example uses some other library, which contains functions that are
more specific to a particular type of transform; however, these library
functions must also be general enough to be useful to a number of 
applications. For example, if transforms are being used for a particular
scientific application, each transform might be doing something a little
different from the other transforms, but every transform for this purpose
might still use the same schema, or even the same data. In such a case, 
there would be certain functions, such as functions to gather specific 
pieces of data from the tree, or functions to format the tree a certain way,   
that all of these transforms would need to use. By using a library to
mix these classes in, the developer has saved time and space by reducing
the need for redundancy. The *Transform* class, for which all
transforms must be a subclass of, includes basic functionality to access
and gather elements, and other functions that are useful to all transforms.

Since libraries can be so helpful, it is good to consruct your own if you
can extract some more general function from it. If you feel that your 
function belongs in a library someone else created, if their library is
open source, contact them to see if they want to take your contribution.
If not you can always create your own sub-library, just by making the
other library the superclass of your own. Make sure to check the license
of their transform library to make sure that you do not run into copyright
issues. In general, it is good to use a library when you can. You can save
yourself a lot of work if you find someone else's custom transform library.
If you want, you can make a library that inherit (or mixes in) another
library. If you plan on distributing your code, make sure that users can get
the libraries that your program is dependent apon. Some libraries are included
in the general package. SEE transform_template_library.py_

transform_template_library.py
-----------------------------

SEE transform_template_fromLibrary.py_ for explaination of when to use a library

In a library, include functions that are general and will be useful to many
different transforms. All libraries must be abstract classes, so do not
include an `__init__` function. The program will inherit the `__init__`
function from *Transform*, which raises an error when the class is initialized.
If you are using variables in your library functions other than root, which you
can always use, you should define a function that should be called in a transforms
`__init__`, but this function should have some a different name, in the example,
this function is called `YourTransformLibraryInit`, which is similar to a name
you should use. Otherwise, just include functions, and use variables as if the
function were written into the class that they will be used by. These methods are
"mixed-in."
