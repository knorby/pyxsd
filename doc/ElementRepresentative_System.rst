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
