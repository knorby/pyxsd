from transform import Transform

#Import anything else you want, just remember to import Transform (or the
#library you are adding to, in which case you do not need to import Transform)
#Just make sure that the library has access to Transform at some level

class YourTransformLibrary (Transform): #Or a libray that you are adding to
    def YourTransformLibraryInit(self):
        self.somevar = None
        #If you need to use vars that are specific to your library, then
        #call an 

    def yourFunction(self, arg1, arg2):
        self.getElementsByName(self.root, 'someTag') #You will always have access
        #your code
        return
    
    def anotherFunction(self, arg1, arg2):
        return
        
