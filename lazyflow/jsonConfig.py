import json
import re
import collections

import numpy # We import numpy here so that it can be used in AutoEval fields.

class Namespace(object):
    """
    Provides the same functionality as:
    
    .. code_block:: python
    
        class Namespace(object):
            pass

    except that ``self.__dict__`` is replaced with an instance of collections.OrderedDict
        
    """
    def __init__(self):
        super(Namespace, self).__setattr__( '_items', collections.OrderedDict() )
    
    def __getattr__(self, key):
        return super(Namespace, self).__getattribute__('_items')[key]
    
    def __setattr__(self, key, val):
        self._items[key] = val
    
    @property
    def __dict__(self):
        return self._items

class AutoEval(object):
    """
    Callable that serves as a pseudo-type.
    Converts a value to a specific type, unless the value is a string, in which case it is evaluated first.
    """
    def __init__(self, t=None):
        """
        If a type t is provided, the value from the config will be converted using t as the constructor.
        If t is not provided, the (possibly eval'd) value will be returned 'as-is' with no conversion.
        """
        self._t = t
        if t is None:
            # If no conversion type was provided, we'll assume that the result of eval() is good enough. 
            self._t = lambda x:x
        
    def __call__(self, x):
        if type(x) is self._t:
            return x
        if type(x) is str or type(x) is unicode and self._t is not str:
            return self._t(eval(x))
        return self._t(x)

class FormattedField(object):
    """
    Callable that serves as a pseudo-type for config values that will be used by ilastik as format strings.
    Doesn't actually transform the given value, but does check it for the required format fields.
    """
    def __init__(self, requiredFields, optionalFields=[]):
        assert isinstance(requiredFields, list)
        assert isinstance(optionalFields, list)
        
        self._requiredFields = requiredFields
        self._optionalFields = optionalFields
    
    def __call__(self, x):
        """
        Convert x to str (no unicode), and check it for the required fields.
        """
        x = str(x)
        for f in self._requiredFields:
            fieldRegex = re.compile('{[^}]*' + f +  '}')
            if fieldRegex.search(x) is None:
                raise JsonConfigSchema.ParsingError( "Format string is missing required field: {{{f}}}".format(f=f) )

        # TODO: Also validate that all format fields the user provided are known required/optional fields.
        return x

#class AutoDirField(object):
#    def __init__(self, replaceString):
#        self._replaceString = replaceString
#    def __call__(self, x):
#        x = str(x)
#        if self._replaceString not in x:
#            return x
#        
#        # Must be /some/dir/<AUTO>, not /some/dir/<AUTO>/plus/otherstuff
#        replaceIndex = x.index(self._replaceString)
#        assert replaceIndex + len(self._replaceString) == len(x), "Auto-replaced dir name must appear at the end of the config value."
#        
#        baseDir, fileBase = os.path.split( x[0:replaceIndex] )
#        next_unused_index = 1
#        for filename in os.listdir(baseDir):
#            m = re.match("("+ fileBase + ")(\d+)", filename)
#            if m:
#                used_index = int(m.groups()[1])
#                next_unused_index = max( next_unused_index, used_index+1 )
#
#        return os.path.join( baseDir, fileBase + "{}".format(next_unused_index)  )


class JsonConfigSchema( object ):
    """
    Simple config schema for json config files.
    Currently, only a very small set of json is supported.
    The schema fields must be a single non-nested dictionary of name : type (or pseudo-type) pairs.
    """
    class ParsingError(Exception):
        pass
    
    def __init__(self, fields):
        self._fields = fields
    
    def parseConfigFile(self, configFilePath):
        with open(configFilePath) as configFile:
            try:
                jsonDict = json.load( configFile, object_pairs_hook=collections.OrderedDict )
                assert isinstance(jsonDict, collections.OrderedDict)
                configDict = collections.OrderedDict( (str(k) , v) for k,v in jsonDict.items() )
            except:
                import sys
                sys.stderr.write( "File '{}' is not valid json.  See stdout for exception details.".format(configFilePath) )
                raise

            try:
                return self._getNamespace(configDict)
            except JsonConfigSchema.ParsingError, e:
                raise JsonConfigSchema.ParsingError( "Error parsing config file '{f}':\n{msg}".format( f=configFilePath, msg=e.args[0] ) )

    def writeConfigFile(self, configFilePath, configNamespace):
        """
        Simply write the given object to a json file as a dict, 
        but check it for errors first by parsing each field with the schema.
        """
        # Check for errors by parsing the fields
        namespace = self._getNamespace(configNamespace.__dict__)
        with open(configFilePath, 'w') as configFile:
            json.dump( namespace.__dict__, configFile, indent=4 )

    def _getNamespace(self, configDict):
        namespace = Namespace()
        # Keys that the user gave us are 
        for key, value in configDict.items():
            if key in self._fields.keys():
                fieldType = self._fields[key]
                try:
                    finalValue = self._transformValue( fieldType, value )
                except JsonConfigSchema.ParsingError, e:
                    raise JsonConfigSchema.ParsingError( "Error parsing config field '{f}':\n{msg}".format( f=key, msg=e.args[0] ) )
                else:
                    setattr( namespace, key, finalValue )

        # All other config fields are None by default
        for key in self._fields.keys():
            if key not in namespace.__dict__.keys():
                setattr(namespace, key, None)
        
        return namespace
    
    def _transformValue(self, fieldType, val):
        # config file is allowed to contain null values, in which case the value is set to None
        if val is None:
            return None

        # Check special error cases
        if fieldType is bool and not isinstance(val, bool):
            raise JsonConfigSchema.ParsingError( "Expected bool, got {}".format( type(val) ) )
        
        # Other special types will error check when they construct.
        return fieldType( val )
    





