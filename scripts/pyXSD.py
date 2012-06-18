#!python
from optparse import OptionParser
from pyxsd.pyXSD import PyXSD
import sys

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
