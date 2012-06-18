from yourTransformLibrary import YourTransformLibrary
#Import anything else you want, just remember to import Transform (or your
#custom library)

class myTransform(YourTransformLibrary):

    #============================================================
    # The next two lines MUST remain unchanged in your transform class

    def __init__(self, root):
        self.root = root
    
    #============================================================
    #Just define the call method and specify arguments, and the program
    #will do your transform

    def __call__(self, arg1, arg2, arg3):

        #More could be in the call, including your entire function.
        #Usually, it is cleaner looking to use another method.

        return self.yourMethod(arg1, arg2, arg3)

    #============================================================
    #

    def yourMethod(self, arg1, arg2, arg3):
        #your transform code
        #some function from your mix-in class
        return #something
