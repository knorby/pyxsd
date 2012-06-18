#! /usr/bin/python

"""
=================
pyXSD Version 0.1
=================

PyXSD was developed in order to map XML and the related schema (XSD) files into the
programming language Python. The program builds a Pythonic representation of the XML 
tree according to the specifications in the schema and raises non-fatal parser errors 
whenever possible in order to help the user validate their XML document. The program 
allows the user to specify *transform* classes, which manipulate and transform the 
XML tree in various ways. The program then writes the tree back out into XML. This 
program was written in order to replace many of the old tools that were written in 
FORTRAN with the more modern XML format and the more modern and powerful Python 
programming language. PyXSD allows users to create their own transform classes with 
the help of a transform library. These classes are fairly simple to write, making the 
system highly adaptable to very specific uses, as one might find in many scientific 
applications; however, the program has potential uses in other fields, since XML is 
widely used. The program allows the user to specify the desired transform classes, 
along with their arguments and sequence of application, so the user can create 
customised tools. The program can be used either as a standalone command line program 
or as a library in other programs.

For more information on pyXSD, see the `pyXSD website <http://pyxsd.org>`_
if you haven't already.

Overview:
=========

- Creates python classes for all types defined in an XSD schema file (xml)
- Reads in a xml file and builds a new pythonic tree according to classes. This tree of instances maintains the same overall structure of the original xml document.
- Provides some xml/schema parsing with non-fatal errors in order to help the user write a valid xml document, without requiring it
- Transforms the pythonic reprsentation according to built-in and add-on 'transform' classes that the user specifies
- Sends data to a writer to write the pythonic tree back into an xml file


Features:
=========

- Transforms allow users to easily adapt pyXSD to vast number of applications
        + Provides a framework and libraries to write transform so the user can more easily write these transform functions
        + Allows the user to specify the desired transform classes with arguments and the order in a file so the user can create a sort of custom tool
        + Allows for transforms that can export to other formats, giving pyXSD powerful flexibility
- The pythonic data tree format uses a very simple structure that allows for an easily understood API, so that users can easily manipute this tree in transforms and use the writer in other programs
- Can be used as a standalone program at the command line or as a library in other programs
- uses the cElementTree library for fast reading of the xml and xsd files

"""

__module__  = "pyXSD"
__version__ = "0.1"
__author__  = "Kali Norby and Mike Summers"

#=====Imports======
import sets, sys, traceback, imp, urllib, os.path

try:    # Thanks to the ElementTree library (http://www.effbot.org/zone/element-index.htm)
    import cElementTree as ET
except:
     try:
          from xml.etree import cElementTree as ET
     except:
          try:
               from xml.etree import ElementTree as ET
          except:
               try:
                    import elementtree.ElementTree as ET
               except:
                    raise ImportError, "Your system needs the ElementTree or cElementTree library in order to run this package"


from elementRepresentatives.elementRepresentative import ElementRepresentative
from writers.xmlTreeWriter                        import XmlTreeWriter
from schemaBase                                   import SchemaBase

class PyXSD(object):

    """main class of the program that is in charge of data flow.
    Has command line support when it is called as a script."""

    def __init__(self, xmlFileInput, xsdFile=None, xmlFileOutput=False, transformOutputName=None, transforms=[], classFile=None, verbose = False, quiet=False):
        """This class is init'ed from the command line normally. Use this information for uses of pyXSD as a library.

        Parameters:

        `- xmlFileInput`- The filename of the xml file to input. Can include path information. Will raise an error if not specified.
        `- xsdFile`- The filename/path information for the schema file. Will attempt to use the schemaLocation tag in the xml if not specified.
        `- xmlFileOutput`- location for xml output to be sent after it is parsed. Will use a default name if not specified. Will not output if value is set to _No_Output_
        `- transformOutputName`- location of the xml output after transform. Will make default filename if not specified.
        `- transforms`- A list containing the transform calls in the order they will be performed.
        `- classFile`- The location of the overlay class file. Experimental.
        `- verbose`- A boolean value. If set to true, will output more information.
        `- quiet`- A boolean value. If set to true, will output less information and errors than normal.

        """
        
        self.verbose = verbose
        self.quiet = quiet
        self.classes = {}

        if isinstance(xmlFileInput, basestring):
             self.xmlFileInput      = os.path.abspath(xmlFileInput)
             self.xmlPath, self.xmlFileInputName  = os.path.split(self.xmlFileInput)
             sys.path.append(self.xmlPath)
        else:
             self.xmlFileInput = xmlFileInput

        self.xsdFile           = xsdFile
        self.xmlFileOutput     = xmlFileOutput


        self.xmlRoot           = self.getXmlTree()
        
        if not self.xmlFileOutput == '_No_Output_':
             if self.xmlFileOutput == None:
                  if isinstance(xmlFileInput, basestring):
                       self.xmlFileOutput = self.getXmlOutputFileName()

        self.transforms = transforms

        if xsdFile == None:
            self.xsdFile = self.getSchemaInfo('l')
        self.getSchemaFile()

        self.nameSpace = self.getSchemaInfo('n')
        
        self.parseXSD()
        
        if classFile:
             if self.verbose:
                  print "Attempting to load overlay classes from the file '%s'..." % classFile
             self.loadClassesFromFile(classFile)
             
        rootInstance = self.parseXML()
        
        if not self.xmlFileOutput == '_No_Output_':
             rootInstance = self.writeParsedXMLFile(rootInstance)
        
        self.transformOutputName = transformOutputName
        self.executeAndWriteTransforms(rootInstance)
             
    #========================================
    def executeAndWriteTransforms(self, rootInstance):
        if len(self.transforms) > 0:
          if self.verbose:
               print "Loading the transforms..."
          if not self.transformOutputName:
               self.transformOutputName = self.getTransformsFileName()
               if self.verbose:
                    print "Loading the file '%s' for the transformed XML output..." % self.transformOutputName
               transformOutput = open(self.transformOutputName, 'w')
          elif self.transformOutputName == "stdout":
               if self.verbose:
                    print "Using stdout for the output of the transformed XML file..."
               transformOutput = sys.stdout
          elif isinstance(self.transformOutputName, basestring):
               if self.verbose:
                    print "Loading the file '%s' for the transformed XML output..." % self.transformOutputName
               transformOutput = open(self.transformOutputName, 'w')
          transformedRoot       = self.transform(self.transforms, rootInstance)
          if transformedRoot:
               if self.verbose:
                    print "Sending transformed tree to the writer..."
               self.writeXML(transformedRoot, transformOutput)

    #========================================
    def getSchemaFile(self):
         try:
              self.xsdFile, messages = urllib.urlretrieve(self.xsdFile)
         except Exception, e:
              if not self.quiet:
                   print "Warning: Something seems wrong with the xsd filename..."
                   if verbose:
                       print "the function urlretrieve in library 'urllib' enconutered the following errors:"
                       print e
         return
    
    #========================================
    def writeParsedXMLFile(self, rootInstance):
          """
          Function to write the xml output after it is parsed. Called from the __init__.
          
          Parameters:
          
          -`rootInstance`: The root instance of a tree. Must be formatted in program's tree structure.

          """
          if isinstance(self.xmlFileOutput, basestring):
               self.xmlFileOutput = open(self.xmlFileOutput, 'w')     
          if self.xmlFileOutput:
               self.writeXML(rootInstance, self.xmlFileOutput)
          return rootInstance

    #========================================

    def parseXSD(self):

        """
        Reads the given xsd file and creates a set of classes that corespond to the complex and simple type definitions.

        No parameters.
        """
        if self.verbose:
             print "Sending the schema file to the ElementTree Parser..."


        tree         = ET.parse(self.xsdFile)
   
        root         = tree.getroot()

        if self.verbose:
             print "Sending the schema ElementTree to the ElementRepresntative module..." 

        schemaER     = ElementRepresentative.factory(root, None)

        for simpleType in schemaER.simpleTypes.values():

            cls                           = simpleType.clsFor(self)

            self.classes[simpleType.name] = cls

            if self.verbose:
                 print "Class created for the %s type..." % simpleType.name 
            
        for complexType in schemaER.complexTypes.values():

            cls                            = complexType.clsFor(self)
        
            self.classes[complexType.name] = cls

            if self.verbose:
                 print "Class created for the %s type..." % complexType.name 

        return


    #========================================

    def parseXML(self):

        """
        Reads the given xml file in the context of the xsd file.
        Produces instances of the above classes.
        Does validation.
        returns a a schema instance object.
        
        no parameters
        """

        if self.verbose:
             print "Starting to parse the xml file." 

        schemaClass         = self.getClasses()['schema']

        schemaClassInstance = schemaClass()

        rootName            = self.xmlRoot.tag.split('}')[1]

        topLevelDescriptors = schemaClassInstance._getElements()

        if len(topLevelDescriptors) > 1:
            if not quiet:
                 print "Error: Invalid XML Schema-there is more than one root element in this document."
                 print "There are %s root elements in this documents:" % repr(len(topLevelDescriptors))
                 for element in topLevelDescriptors:
                      print element.name
                 print "The parser will proceed and attempt to parse only %s" % repr(topLevelDescriptors[0].name)
                 print
            
        if  len(topLevelDescriptors) == 0:
            raise "Error: Invalid XML Schema-the parser could not find any root elements in the schema"
            return None
    
        rootElement     = topLevelDescriptors[0]
        rootElementName = rootElement.name

        if rootElementName == rootName:
            subCls = rootElement.getType()
            self.generateCorrectSchemaTags()
            subInstance = subCls.makeInstanceFromTag(self.xmlRoot)
            setattr(schemaClassInstance, rootElementName, subInstance)

        return subInstance    

    #========================================

    def generateCorrectSchemaTags(self):
         """
         Generates the proper schema information and namespace information for a tag.
         ElementTree leaves the schema information in a form that is not valid XML
         on its own. 
         
         No parameters
         """

         locationTagName = self.getSchemaInfo('t')
         
         schemaLocation  = self.xmlRoot.attrib[locationTagName]

         del self.xmlRoot.attrib[locationTagName]

         ns = self.nameSpace

         self.xmlRoot.attrib['xmlns:xsi'] = 'http://www.w3.org/2001/XMLSchema-instance'

         if ns:
              schemaLocation = schemaLocation
              self.xmlRoot.attrib['xsi:schemaLocation'] = schemaLocation
              self.xmlRoot.attrib['xmlns'] = ns
              self.xmlRoot.attrib['xmlns:%s' % ns.lower()] = ns
              return

         self.xmlRoot.attrib['xsi:noNamespaceSchemaLocation'] = schemaLocation
         return

    #========================================

    def writeXML(self, rootInstance, output):
         """
         Sends a pythonic instance tree to the tree writer.
         
         parameters:
         
         - `rootInstance`: The root instance of a tree. Must be formatted in program's tree structure.
         - `output`: The file object to write the tree to.

         """
         if isinstance(output, basestring):
              output = open(output, 'w')
         writeTree   = XmlTreeWriter(rootInstance, output)
         if self.verbose:
              print "Data sent to the writer..."
         

    #========================================

    def getClasses(self):
        """ 
        Returns the dictionary of classes created by ElementRepresentative for each type specified in the schema.

        no parameters
        """
        return self.classes
   
    #========================================
    def loadClassFromFile(self, classFile):
        """
        Loads a file with overlay classes into the class dictionary.
        Overlay classes add to and override the schema type classes to allow for a user to create their own types without changing the schema file itself.
        **Consider this functionality experimental.**
        
        parameters:

        - `classFile`: A string that specifies the location of a user-created overlay class file

        """
        try:
             fp, pathname, description = imp.find_module(classFile)
        except ImportError:
             raise ImportError, "the file '%s' was not found. Please check your spelling." % classFile
        module =  imp.load_module(classFile, fp, pathname, description)
        newClasses = {}
        for var in vars(module):
             if isinstance(var, object):
                  if issubclass(var, SchemaBase):
                       try:
                            className = var.name
                       except:
                            if not self.quiet:
                                 print "Load Error: the class %s must have a 'name' attribute. Will attempt to use '__name__' instead." % var
                            if var.__name__:
                                 try:
                                      className = var.__name__
                                 except:
                                      if not self.quiet:
                                           print "Load Fail: the class %s could not be loaded. The program will continue to load classes." % var
                                      continue
                       newClasses[className] = var
                       if self.self.verbose:
                            print "Loaded the %s class" % className
        self.classes.update(newClasses)
    #========================================

    def getXmlTree(self):
         """
         Sends the xml file into the ElementTree library's parser. Allows for the program to get the schemaLocation before parsing the xml against the schema.

         No parameters.
         """

         try:

              tree =  ET.parse(self.xmlFileInput)
              if self.verbose:
                   print "XML file parsed by the ElementTree library Suceessfully..." 
         except Exception,e:
              print
              print "Program Error: The ElementTree library's parse function was unable to read your"
              print  "XML File correctly. The following are its errors (the program will halt):"
              print
              
              tree =  ET.parse(self.xmlFileInput)

         return tree.getroot()
    
    #========================================

    def getXmlOutputFileName(self):
        """
        Creates a default name for xml file that is parsed without any transforms.
        Uses the name from the inputed xml file.

        no parameters
        """

        inputName        = self.xmlFileInput

        path, inputName  = os.path.split(inputName)
        
        inputNameSplit   = inputName.split('.')

        nonExtensionName = inputNameSplit[-2]

        nonExtensionName = nonExtensionName + 'Parsed'

        return os.path.join(path, (nonExtensionName + '.xml'))

    #========================================
    def getTransformModuleAndLoad(self, className):
         """
         Loads a transform class from its class name.
         The file that it is located in must be the same as the className, except the first
         letter in the filename must be lowercase. The transform must be located in a
         directory called `transforms` that is in the installation folder of pyXSD, the
         directory you called the program from, or in the directory where the xml file is.

         parameters:

         - `className`: A string of the transform class name being called
         """
         fileName = className[:1].lower() + className[1:]
         transformMod = __import__(('transforms' + '.' + fileName), globals(), locals(), [className])
         return transformMod 

    #========================================
    #See the documentation for further details on this functionality

    def transform(self, transforms, root): 
         """
         Calls the transforms specified by the user. Each transform is loaded into memory by
         getTransformModuleAndLoad(). The transform class is passed the instance of the root
         element when it is initialized. The transform object is called with the specified
         arguements and the new root instance is set to whatever the transform returns,
         which is usually the root, but it is not required. Any user who uses a transform
         that does not return the root tree instance should be aware that any transform
         that uses the root instance will fail to work and raise a fatal error. Transforms
         add a great amount of power to the program, but users might need to tweak their
         transform calls and any user-written classes in order to get them to work correctly.

         Further documentation is located in the doc/ directory and on the `pyXSD website <http://pyxsd.org>`_.
         These documents can help users write transform classes and calls.

         Parameters:

         - `transforms`: a list containing the transform calls in the order that they should be called.
         - `root`: the root instance of a tree. Must be formatted in program's tree structure.
         
         """
         currentRoot = root
         def echoArgs(*args, **kwargs):
              return args, kwargs
         
         for transform in transforms:

              if not '(' in sets.Set(transform):
                   raise "Transform Call Error: the transform call '%s' does not use correct syntax." % transform

              transformSplit = transform.split('(')
              args, kwargs = eval(' echoArgs(' + '('.join(transformSplit[1:]))
              transformer = self.getTransformModuleAndLoad(transformSplit[0])
              argString = str(args)
              

              if not len(kwargs) == 0:

                   keywordArgString = ""
                   for key, value in kwargs.iteritems():
                        keywordArgString = keywordArgString + '%s=%s,' % (key, value)
                   argString = argString.rstrip(',').rstrip(')') + keywordArgString + ')'

              if self.verbose:
                   print "Starting the transform '%s' with the following args: %s" % (transformSplit[0], argString)
              currentRoot = eval('transformer.%s(currentRoot)%s' % (transformSplit[0], argString))

         return currentRoot 
              
    #========================================

    def getTransformsFileName(self):
        """
        Creates a default name for xml file that is written after all of the transforms.
        Uses the name from the inputed xml file.

        no parameters
        """

        inputName  = self.xmlFileInputName

        if '.' in sets.Set(self.xmlFileInputName):

            inputNameSplit   = self.xmlFileInputName.split('.')

            nonExtensionName = inputNameSplit[-2]

        else:

            nonExtensionName = self.xmlFileInputName

        nonExtensionName = nonExtensionName + 'Transformed'

        newName = nonExtensionName + '.xml'

        newName = os.path.join(self.xmlPath, self.xmlFileInputName)

        if self.verbose:

             print "Setting the transformed xml file name to the default:", newName 

        return newName

    #========================================

    def getSchemaInfo(self, nameOrLocation):

        """
        Extracts information from the *schemaLocation* tag or the *noNamespaceSchemaLocation* tag.
        Depending on the value of parameter `nameOrLocation`, the function outputs the namespace,
        schema location, or the tag type. This function is meant for use with other functions
        to easily grab bits of data that are used in various locations in the program.

        parameters:

        - `nameOrLocation`: a one letter string that is either 'l', 'n', or 't'. If the variable is
        'l', the location of the schema is returned. If it is 'n', the namespace is returned, if there
        is one. 't' returns the tag name to indicate if the xml uses *schemaLocation* or *noNamespaceSchemaLocation*

        """
    
        xsiNS               = 'http://www.w3.org/2001/XMLSchema-instance'

        if self.makeFullName(xsiNS, 'schemaLocation') in self.xmlRoot.attrib:
             
             schemaLocationTag   = self.xmlRoot.attrib[self.makeFullName(xsiNS, 'schemaLocation')]

             if '\n' in sets.Set(schemaLocationTag):
                  schemaLocationSplit = schemaLocationTag.split('\n')
             else:
                  schemaLocationSplit = schemaLocationTag.split(' ')

             if not len(schemaLocationSplit)==2:

                  print "Parser Error: the 'schemaLocation' tag must be a pair of values seperated by a space or line break"
                  print "with the namespace stated first, followed by the location of the schema."
                  print "The program will attempt to use the 'noNamespaceSchemaLocation' tag instead."
                  print
                  del self.xmlRoot.attrib[self.makeFullName(xsiNS, 'schemaLocation')]
                  self.xmlRoot.attrib[self.makeFullName(xsiNS, 'noNamespaceSchemaLocation')] = schemaLocationTag
                  
        if self.makeFullName(xsiNS, 'noNamespaceSchemaLocation') in self.xmlRoot.attrib:

             if nameOrLocation == 't':

                  return self.makeFullName(xsiNS, 'noNamespaceSchemaLocation')

             if nameOrLocation == 'n':

                  return None

             if nameOrLocation == 'l':

                  return self.xmlRoot.attrib[self.makeFullName(xsiNS, 'noNamespaceSchemaLocation')]
             

        schemaNS            = schemaLocationSplit[0]

        if nameOrLocation == 'n':

            return schemaNS
        
        schemaLocation      = schemaLocationSplit[-1]

        if nameOrLocation == 'l':

            return schemaLocation

        if nameOrLocation == 't':

            return self.makeFullName(xsiNS, 'schemaLocation')

    #=======================================================
    #
    def makeFullName(self, ns, text):
        """
        Makes a string that looks similar to some of the names in ElementTree when it contains namespace information.

        parameters:

        - `ns`: a string of the namespace used. For this function, this variable is usually set to a url.
        - `text`: a string of the name of the tag that the full name is being created for.
        
        """


        return "{%s}%s" % (ns, text)



from optparse import OptionParser



def main():
    """
    This function is called when pyXSD is being called from the command line.
    It runs the OptionParser found under optparse in the standard library. Some
    checks are performed on the data collected, and if these checks pass, it initializes
    the pyXSD class.

    No parameters
    """
    usage  = "usage: ./pyXSD.py [options] arg"
    parser = OptionParser(usage, version="PyXSD 0.1")
    
    parser.add_option("-i", "--inputXml", type="string", dest="inputXmlFile", default="stdin",
                      help="filename for the xml file to read in. Reads from stdin by default." )
    parser.add_option("-s", "--inputXsd", "--schema",  type="string", dest="inputXsdFile", default=None,
                      help="filename for the xsd (schema) file to read in. Trys to determine location from the input xml file by default." )

    parser.add_option("-p", "--parsedXml", "--parsedOutput",  type="string", dest="parsedOutputFile", default=None,
                      help="filename for the xml file that contains the parsed output of the xml file, which contains no further transformation. By default, the filename is the xml input filename followed by 'Parsed.'" )
    parser.add_option("-k", "--ParsedFile", action="store_false", dest="outputParsed", default=True,
                      help="outputs a parsed version of the xml file without transform. Use for debugging. Off by default. If no filename is specified, it will be determined from the xml filename.")
    parser.add_option("-o", "--transformOutput", action="store", type="string", dest="transformOutputFile", default="stdout",
                      help="filename for the output after the xml has been parsed and transformed. Output is sent to stdout by default. Any specified filename will override this option." )
    parser.add_option("-d", "--useDefaultFile", action="store_true", dest="transformDefaultOutput",
                      help="Uses the default filename for transformed output. If not specified and no filename is specified, uses stdout" )
    parser.add_option("-t", "--transform", action="store", type="string", dest="transformCall", default=None,
                      help="the transform class with args. See the documentation for syntax and further information." )
    parser.add_option("-T", "--transformFile", action="store", type="string", dest="transformFile", default=None,
                      help="file with transform class calls. See the documentation for information on the this function" )
    parser.add_option("-c", "--overlayClassesFile", action="store", type="string", dest="classFile", default=None,
                      help="Experimental. Allows for user defined schemas to override and add to the types defined in the schema file. See the documentation for information on the this function" )
    parser.add_option("-v", "--verbose", action="store_true", dest="verbose", default=False,
                      help="uses the verbose mode. Experts Only. (limited functionality)" )
    parser.add_option("-q", "--quiet", action="store_true", dest="quiet", default=False,
                      help="uses the quiet mode. Few errors reported. (limited functionalily)" )

    (options, args) = parser.parse_args()

    if len(args) > 0:
        parser.error("The arguement(s) '%s' is/are not valid. See the syntax help under -h." % args)

    if options.transformDefaultOutput:
         if options.transformOutputFile == 'stdout':
              options.transformOutputFile = None

    if options.inputXmlFile == "stdin":
        if sys.stdin.isatty():
            parser.error("if no input xml file is specified, the xml must\n\t\t  be fed in through the stdin (i.e. pipes)")
        inputXmlFile = 'stdin.xml'
        newFile = open(inputXmlFile, 'w')
        newFile.write(sys.stdin.read())
        newFile.close()
    else:
       inputXmlFile = options.inputXmlFile

    if options.transformCall and options.transformFile:
        parser.error("A transform file and a transform call cannot both be specified.")

    linestrip = lambda x: x.strip('>').strip('\n').strip()
    transforms = []

    if options.transformCall:
        if '>' in sets.Set(options.transformCall):
            transforms = options.transformCall.split('>')
            transforms = map(linestrip, transforms)
        else:
            transforms.append(options.transformCall)
    if options.transformFile:
        transformFile = open(options.transformFile, 'r')
        transforms = transformFile.readlines()
        transformFile.close()
        transforms = map(linestrip, transforms)

    if options.outputParsed:
        options.parsedOutputFile = "_No_Output_"

    if options.quiet and options.verbose:
         parser.error("Both the verbose mode and the quiet mode cannot be on at the same time")
    xmlParser = PyXSD(inputXmlFile, options.inputXsdFile, options.parsedOutputFile, options.transformOutputFile, transforms, options.classFile, options.verbose, options.quiet)

if __name__ == '__main__':

    main()
