Transforms: Basic Use
======================
:Author: Kali Norby <kali.norby@gmail.com>
:Date: Wed, 30 Aug 2006
:Description: Explains how to call Transforms and gives other tips for users on using transforms


Transforms allow for a user to modify an xml tree using python code. 
Each transform varies in its functionality, and some may require that
the xml tree follows a particular form. You should know what a transform
does before you use it. Each transform also takes in arguments specific 
to the individual transform. Some, but not many, transforms are provided
in the basic pyXSD package, but you can find more at <http://pyxsd.org/> or 
other locations. All the transforms must be stored in the transforms directory
in order to work. Each transform call follows the following syntax::

   TransformClassName(arg1, arg2,....)
   
The class name should be the same as the file in which the class is stored, except
with the first word captilized. The program will raise an error if this pattern
is broken.

You can load transforms when calling pyXSD from the command line two different
ways. One way is to specify the location of a "transform" file, using the **-T**
option. The transform file should have one transform call per line. Do not use quotes
around the call (see the example transform file in the `example/` directory. You can
also call a series of transform calls from the command line, using the **-t** option.
You must put quotes around the list of calls. Not all quotes may work on your 
system, so experimentation may be necessary. If you are not having success with the
normal single and double quotes, try using symbol commonly under the **~** on your 
keyboard (**`**). Seperate the individual calls with the greater-than sign **>**. 
The call might look something like the following::

     pyXSD.py ... -t `Transform1(arg1, arg2) > Transform2(arg3, arg4, arg5)`  

When using transform when pyXSD is functioning as a library, each transform call
must be a seperate entry in a list, and they must all be strings.

