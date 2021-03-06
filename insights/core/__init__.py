import datetime
import io
import json
import logging
import operator
import os
import re
import shlex
import yaml
import six
from fnmatch import fnmatch

from insights.configtree import from_dict, iniconfig, Root, select, first
from insights.configtree import Directive, SearchResult, Section
from insights.contrib.ConfigParser import RawConfigParser

from insights.parsers import ParseException
from insights.core.plugins import ContentException
from insights.core.serde import deserializer, serializer
from insights.util import deprecated

import sys
# Since XPath expression is not supported by the ElementTree in Python 2.6,
# import insights.contrib.ElementTree when running python is prior to 2.6 for compatibility.
# Script insights.contrib.ElementTree is the same with xml.etree.ElementTree in Python 2.7.14
# Otherwise, import xml.etree.ElementTree instead.
if sys.version_info[0] == 2 and sys.version_info[1] <= 6:
    import insights.contrib.ElementTree as ET
else:
    import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)


class Parser(object):
    """
    Base class designed to be subclassed by parsers.

    The framework will construct your object with a `Context` that will
    provide *at least* the content as an interable of lines and the path
    that the content was retrieved from.

    Facts should be exposed as instance members where applicable. For
    example::

        self.fact = "123"

    Examples:
        >>> class MyParser(Parser):
        ...     def parse_content(self, content):
        ...         self.facts = []
        ...         for line in content:
        ...             if 'fact' in line:
        ...                 self.facts.append(line)
        >>> content = '''
        ... # Comment line
        ... fact=fact 1
        ... fact=fact 2
        ... fact=fact 3
        ... '''.strip()
        >>> my_parser = MyParser(context_wrap(content, path='/etc/path_to_content/content.conf'))
        >>> my_parser.facts
        ['fact=fact 1', 'fact=fact 2', 'fact=fact 3']
        >>> my_parser.file_path
        '/etc/path_to_content/content.conf'
        >>> my_parser.file_name
        'content.conf'
    """

    def __init__(self, context):
        self.file_path = os.path.join("/", context.relative_path) if context.relative_path is not None else None
        """str: Full context path of the input file."""
        self.file_name = os.path.basename(context.path) \
            if context.path is not None else None
        """str: Filename portion of the input file."""
        if hasattr(context, "last_client_run"):
            self.last_client_run = context.last_client_run
        else:
            self.last_client_run = None
        self.args = context.args if hasattr(context, "args") else None
        self.parse_content(context.content)

    def parse_content(self, content):
        """This method must be implemented by classes based on this class."""
        msg = "Parser subclasses must implement parse_content(self, content)."
        raise NotImplementedError(msg)


@serializer(Parser)
def default_parser_serializer(obj):
    return vars(obj)


@deserializer(Parser)
def default_parser_deserializer(_type, data):
    obj = _type.__new__(_type)
    obj.file_path = None
    obj.file_name = None
    obj.last_client_run = None
    obj.args = None
    for k, v in data.items():
        setattr(obj, k, v)
    return obj


def find_main(confs, name):
    for c in confs:
        if c.file_name == name:
            return c


def flatten(docs, pred):
    def inner(children):
        results = []
        for c in children:
            if select(pred)([c]) and c.children:
                results.extend(inner(c.children))
            else:
                results.append(c)
        return results
    return inner(docs)


class ConfigComponent(object):
    def select(self, *queries, **kwargs):
        """
        Given a list of queries, executes those queries against the set of
        Nodes. A Node has three primary attributes: name (str), attrs
        ([str|int]), and children ([Node]).

        Nodes also have a value attribute that is either the first attribute
        (in the case of simple directives that only have one), or the string
        representation of all attributes joined by a single space.

        Each positional argument to select represents a query against the name
        and/or attributes of the corresponding level of the configuration tree.
        The first argument queries root nodes, the second argument queries
        children of the root nodes, etc.

        An individual query is either a single value or a tuple. A single value
        queries the name of a Node. A tuple queries the name and the attrs.

        So: `select(name_predicate)` or `select((name_predicate,
        attrs_predicate))`

        In general, `select(pred1, pred2, pred3, ...)`

        If a predicate is a simple value (string or int), an exact match is
        required for names, and an exact match of any attribute is required for
        attributes.

        Examples:
        `select("Directory")` queries for all root nodes named Directory.

        `select("Directory", "Options")` queries for all root nodes named
        Directory that contain at least one child node named Options. Notice
        the argument positions: Directory is in position 1, and Options is in
        position 2.

        `select(("Directory", "/"))` queries for all root nodes named Directory
        that contain an attribute exactly matching "/". Notice this is one
        argument to select: a 2-tuple with predicates for name and attrs.

        If you are only interested in attributes, just pass `None` for the name
        predicate in the tuple: `select((None, "/"))` will return all root
        nodes with at least one attribute of "/"

        In addition to exact matches, the elements of a query can be functions
        that accept the value corresponding to their position in the query. A
        handful of useful functions and boolean operators between them are
        provided.

        `select(startswith("Dir"))` queries for all root nodes with names
        starting with "Dir".

        `select(~startswith("Dir"))` queries for all root nodes with names not
        starting with "Dir".

        `select(startswith("Dir") | startswith("Ali"))` queries for all root
        nodes with names starting with "Dir" or "Ali". The return of `|` is a
        single callable passed in the first argument position of select.

        `select(~startswith("Dir") & ~startswith("Ali"))` queries for all root
        nodes with names not starting with "Dir" or "Ali".

        If a function is in an attribute position, it is considered True if it
        returns True for any attribute.

        For example, `select((None, 80))` often will return the list of one
        Node [Listen 80]

        `select(("Directory", startswith("/var")))` will return all root nodes
        named Directory that also have an attribute starting with "/var"

        If you know that your selection will only return one element, or you
        only want the first or last result of the query , pass `one=first` or
        `one=last`.

        `select(("Directory", startswith("/")), one=last)` will return the
        single root node for the last Directory entry starting with "/"

        If instead of the root nodes that match you want the child nodes that
        caused the match, pass `roots=False`.

        `node = select(("Directory", "/var/www/html"), "Options", one=last,
        roots=False)` might return the Options node if the Directory for
        "/var/www/html" was defined and contained an Options Directive. You
        could then access the attributes with `node.attrs`. If the query didn't
        match anything, it would have returned None.

        If you want to slide the query down the branches of the config, pass
        deep=True to select.  That allows you to do `conf.select("Directory",
        deep=True, roots=False)` and get back all Directory nodes regardless of
        nesting depth.

        conf.select() returns everything.

        Available predicates are:
        & (infix boolean and)
        | (infix boolean or)
        ~ (prefix boolean not)

        For ints or strings:
        eq (==)  e.g. conf.select("Directory, ("StartServers", eq(4)))
        ge (>=)  e.g. conf.select("Directory, ("StartServers", ge(4)))
        gt (>)
        le (<=)
        lt (<)

        For strings:
        contains
        endswith
        startswith
        """
        return self.doc.select(*queries, **kwargs)

    def find(self, *queries, **kwargs):
        """
        Finds the first result found anywhere in the configuration. Pass
        `one=last` for the last result. Returns `None` if no results are found.
        """
        kwargs["deep"] = True
        kwargs["roots"] = False
        if "one" not in kwargs:
            kwargs["one"] = first
        return self.select(*queries, **kwargs)

    def find_all(self, *queries):
        """
        Find all results matching the query anywhere in the configuration.
        Returns an empty `SearchResult` if no results are found.
        """
        return self.select(*queries, deep=True, roots=False)

    def _children_of_type(self, t):
        return [c for c in self.doc.children if isinstance(c, t)]

    @property
    def sections(self):
        return SearchResult(children=self._children_of_type(Section))

    @property
    def directives(self):
        return SearchResult(children=self._children_of_type(Directive))

    def __getitem__(self, query):
        """
        Similar to select, except tuples are constructed without parentheses:
        `conf["Directory", "/"]` is the same as `conf.select(("Directory", "/"))`

        Notice that queries return `SearchResult` instances, which also have
        `__getitem__` delegating to `select` except the select is against
        grandchild nodes in the tree. This allows more complicated queries,
        like this one that gets all Options entries beneath the Directory
        entries starting with "/var":

        `conf["Directory", startswith("/var")]["Options"]`

        or equivalently

        `conf.select(("Directory", startswith("/var")), "Options, roots=False)

        Note you can recover the enclosing section with `node.parent` or a
        node's ultimate root with `node.root`. If a `Node` is a root,
        `node.root` is just the node itself.

        To get all root level Directory and Alias directives, you could do
        something like `conf[eq("Directory") | eq("Alias")]`

        To get loaded auth modules:
        `conf["LoadModule", contains("auth")]`
        """
        if isinstance(query, (int, slice)):
            return self.doc[query]
        return self.select(query)

    def __contains__(self, item):
        return item in self.doc

    def __len__(self):
        return len(self.doc)

    def __iter__(self):
        return iter(self.doc)

    def __repr__(self):
        return str(self.doc)

    def __str__(self):
        return self.__repr__()


class ConfigParser(Parser, ConfigComponent):
    """
    Base Insights component class for Parsers of configuration files.
    """
    def parse_content(self, content):
        self.content = content
        self.doc = self.parse_doc(content)

    def parse_doc(self, content):
        raise NotImplemented()

    def lineat(self, pos):
        return self.content[pos] if pos is not None else None


class ConfigCombiner(ConfigComponent):
    """
    Base Insights component class for Combiners of configuration files with
    include directives for supplementary configuration files. httpd and nginx
    are examples.
    """
    def __init__(self, confs, main_file, include_finder):
        self.confs = confs
        self.main = find_main(confs, main_file)
        server_root = self.conf_path

        # Set the children of all include directives to the contents of the
        # included configs
        for conf in confs:
            for node in conf.doc.select(include_finder, deep=True, roots=False):
                pattern = node.value
                if not pattern.startswith("/"):
                    pattern = os.path.join(server_root, pattern)
                includes = self.find_matches(confs, pattern)
                for inc in includes:
                    node.children.extend(inc.doc.children)

        # flatten all content from nested includes into a main doc
        self.doc = Root(children=flatten(self.main.doc.children, include_finder))

    def find_matches(self, confs, pattern):
        results = [c for c in confs if fnmatch(c.file_path, pattern)]
        return sorted(results, key=operator.attrgetter("file_name"))


class SysconfigOptions(Parser):
    """
    A parser to handle the standard 'keyword=value' format of files in the
    ``/etc/sysconfig`` directory.  These are provided in the standard 'data'
    dictionary.

    Examples:

        >>> ntpconf = shared[NtpConf]
        >>> 'OPTIONS' in ntpconf.data
        True
        >>> 'NOT_SET' in ntpconf.data
        False
        >>> 'COMMENTED_OUT' in ntpconf.data
        False
        >>> ntpconf.data['OPTIONS']
        '-x -g'

    For common variables such as OPTIONS, it is recommended to set a specific
    property in the subclass that fetches this option with a fallback to a
    default value.

    Example subclass::

        class DirsrvSysconfig(SysconfigOptions):

            @property
            def options(self):
                return self.data.get('OPTIONS', '')
    """

    def parse_content(self, content):
        result = {}
        unparsed_lines = []

        # Do not use get_active_lines, it strips comments within quotes
        for line in content:
            if not line or line.startswith('#'):
                continue

            try:
                words = shlex.split(line)
            except ValueError:
                # Handle foo=bar # unpaired ' or " here
                line, comment = line.split(' #', 1)
                words = shlex.split(line)

            # Either only one thing or line or rest starts with comment
            # but either way we need to have an equals in the first word.
            if (len(words) == 1 or (len(words) > 1 and words[1][0] == '#')) \
                    and '=' in words[0]:
                key, value = words[0].split('=', 1)
                result[key] = value
            # Only store lines if they aren't comments or blank
            elif len(words) > 0 and words[0][0] != '#':
                unparsed_lines.append(line)
        self.data = result
        self.unparsed_lines = unparsed_lines

    def __getitem__(self, option):
        """ Retrieves an item from the underlying data dictionary."""
        return self.data[option]

    def __contains__(self, option):
        """ Does the underlying dictionary contain this option?"""
        return option in self.data

    def keys(self):
        """ Return the list of keys (in no order) in the underlying dictionary."""
        return self.data.keys()

    def get(self, item, default=None):
        """
        Returns value of key ``item`` in self.data or ``default``
        if key is not present.

        Parameters:
            item (str): Key to get from ``self.data``.
            default (str): Default value to return if key is not present.

        Returns:
            (str): String value of the stored item, or the default if not found.
        """
        return self.data.get(item, default)


class LegacyItemAccess(object):
    """
    Mixin class to provide legacy access to ``self.data`` attribute.

    Provides expected passthru functionality for classes that still use
    ``self.data`` as the primary data structure for all parsed information.  Use
    this as a mixin on parsers that expect these methods to be present as they
    were previously.

    Examples:
        >>> class MyParser(LegacyItemAccess, Parser):
        ...     def parse_content(self, content):
        ...         self.data = {}
        ...         for line in content:
        ...             if 'fact' in line:
        ...                 k, v = line.split('=')
        ...                 self.data[k.strip()] = v.strip()
        >>> content = '''
        ... # Comment line
        ... fact1=fact 1
        ... fact2=fact 2
        ... fact3=fact 3
        ... '''.strip()
        >>> my_parser = MyParser(context_wrap(content, path='/etc/path_to_content/content.conf'))
        >>> my_parser.data
        {'fact1': 'fact 1', 'fact2': 'fact 2', 'fact3': 'fact 3'}
        >>> my_parser.file_path
        '/etc/path_to_content/content.conf'
        >>> my_parser.file_name
        'content.conf'
        >>> my_parser['fact1']
        'fact 1'
        >>> 'fact2' in my_parser
        True
        >>> my_parser.get('fact3', default='no fact')
        'fact 3'
    """

    def __getitem__(self, item):
        return self.data[item]

    def __contains__(self, item):
        return item in self.data

    def get(self, item, default=None):
        """Returns value of key ``item`` in self.data or ``default``
        if key is not present.

        Parameters:
            item: Key to get from ``self.data``.
            default: Default value to return if key is not present.

        Returns:
            (str): String value of the stored item, or the default if not found.
        """
        return self.data.get(item, default)


class CommandParser(Parser):
    """
    This class checks output from the command defined in the spec.
    If `context.content` contains a single line and that line is
    included in the `bad_lines` list a `ContentException` is raised
    """

    bad_lines = ["no such file or directory", "command not found"]
    """
    This variable contains filters for bad responses from commands defined
    with command specs.
    When adding a new lin to the list make sure text is all lower case.
    """

    def validate_lines(self, results):
        """
        If `results` contains a single line and that line is included
        in the `bad_lines` list, this function returns `False`. If no bad
        line is found the function returns `True`

        Parameters:
            results(str): The results string of the output from the command
                defined by the command spec.

        Returns:
            (Boolean): True for no bad lines or False for bad line found.
        """

        if results and len(results) == 1:
            first = results[0]
            if any(l in first.lower() for l in self.bad_lines):
                return False
        return True

    def __init__(self, context):
        """
            This __init__ calls `validate_lines` function to check for bad lines.
            If `validate_lines` returns False, indicating bad line found, a
            ContentException is thrown.
        """

        if not self.validate_lines(context.content):
            first = context.content[0] if context.content else "<no content>"
            name = self.__class__.__name__
            raise ContentException(name + ": " + first)
        super(CommandParser, self).__init__(context)


class XMLParser(LegacyItemAccess, Parser):
    """
    A parser class that reads XML files.  Base your own parser on this.

    Examples:
        >>> content = '''
        ... <?xml version="1.0"?>
        ... <data xmlns:fictional="http://characters.example.com"
        ...       xmlns="http://people.example.com">
        ...     <country name="Liechtenstein">
        ...         <rank updated="yes">2</rank>
        ...         <year>2008</year>
        ...         <gdppc>141100</gdppc>
        ...         <neighbor name="Austria" direction="E"/>
        ...         <neighbor name="Switzerland" direction="W"/>
        ...     </country>
        ...     <country name="Singapore">
        ...         <rank updated="yes">5</rank>
        ...         <year>2011</year>
        ...         <gdppc>59900</gdppc>
        ...         <neighbor name="Malaysia" direction="N"/>
        ...     </country>
        ...     <country name="Panama">
        ...         <rank>68</rank>
        ...         <year>2011</year>
        ...         <gdppc>13600</gdppc>
        ...         <neighbor name="Costa Rica" direction="W"/>
        ...     </country>
        ... </data>
        ... '''.strip()
        >>> xml_parser = XMLParser(context_wrap(content))
        >>> xml_parser.xmlns
        'http://people.example.com'
        >>> xml_parser.get_elements(".")[0].tag # Top-level elements
        'data'
        >>> len(xml_parser.get_elements("./country/neighbor", None)) # All 'neighbor' grand-children of 'country' children of the top-level elements
        3
        >>> len(xml_parser.get_elements(".//year/..[@name='Singapore']")[0]) # Nodes with name='Singapore' that have a 'year' child
        1
        >>> xml_parser.get_elements(".//*[@name='Singapore']/year")[0].text # 'year' nodes that are children of nodes with name='Singapore'
        '2011'
        >>> xml_parser.get_elements(".//neighbor[2]", "http://people.example.com")[0].get('name') # All 'neighbor' nodes that are the second child of their parent
        'Switzerland'
    Attributes:
        raw (str): raw XML content
        dom (Element): Root element of parsed XML file
        xmlns (str): The default XML namespace, an empty string when no
            namespace is declared.
        data (dict): All required specific properties can be included in data.
    """

    def parse_dom(self):
        """
        If ``self.data`` is required, all child classes need to overwrite this
        function to set it
        """
        return {}

    def parse_content(self, content):
        """
        All child classes inherit this function to parse XML file automatically.
        It will call the function :func:`parse_dom` by default to
        parser all necessary data to :attr:`data` and the :attr:`xmlns` (the
        default namespace) is ready for this function.
        """
        self.dom = self.xmlns = None
        self.data = {}
        # ignore empty xml file
        if len(content) > 3:
            self.raw = '\n'.join(content)
            self.dom = ET.fromstring(self.raw)
            self.xmlns = self.dom.tag.strip("{").split("}")[0] if all(c in self.dom.tag for c in ["{", "}"]) else ""
            self.data = self.parse_dom()

    def get_elements(self, element, xmlns=None):
        """
        Return a list of elements those match the searching condition.
        If the XML input has namespaces, elements and attributes with prefixes
        in the form prefix:sometag get expanded to {namespace}element where the
        prefix is replaced by the full URI.  Also, if there is a default
        namespace, that full URI gets prepended to all of the non-prefixed tags.
        Element names can contain letters, digits, hyphens, underscores, and
        periods.  But element names must start with a letter or underscore.
        Here the while-clause is to set searching condition from
        `/element1/element2` to `/{namespace}element1/{namespace}/element2`

        Parameters:
            element: Searching condition to search certain elements in an XML
                     file. For more details about how to set searching
                     condition, refer to section `19.7.2.1. Example` and
                     `19.7.2.2. Supported XPath syntax` in
                     https://docs.python.org/2/library/xml.etree.elementtree.html
            xmlns:   XML namespace, default value to None.
                     None means that xmlns equals to the `self.xmlns` (default
                     namespace) instead of "" all the time.  Only string type
                     parameter (including "") will be regarded as a valid xml
                     namespace.
        Returns:
            (list): List of elements those match the searching condition
        """
        real_element = ""
        real_xmlns = ""

        if xmlns is None:
            real_xmlns = "{" + self.xmlns + "}" if self.xmlns else ""
        else:
            real_xmlns = "{" + xmlns + "}"

        while "/" in element:
            l = element.split("/", 1)
            element = l[1]
            real_element += l[0] + "/"
            if element[0].isalpha() or element[0] == "_":
                real_element += real_xmlns

        real_element += element
        return self.dom.findall(real_element)


class YAMLParser(Parser, LegacyItemAccess):
    """
    A parser class that reads YAML files.  Base your own parser on this.
    """
    def parse_content(self, content):
        if type(content) is list:
            self.data = yaml.safe_load('\n'.join(content))
        else:
            self.data = yaml.safe_load(content)

        self.doc = from_dict(self.data)


class JSONParser(Parser, LegacyItemAccess):
    """
    A parser class that reads JSON files.  Base your own parser on this.
    """
    def parse_content(self, content):
        self.data = json.loads(''.join(content))
        self.doc = from_dict(self.data)


class ScanMeta(type):
    def __new__(cls, name, parents, dct):
        dct["scanners"] = []
        dct["scanner_keys"] = set()
        return super(ScanMeta, cls).__new__(cls, name, parents, dct)


class Scannable(six.with_metaclass(ScanMeta, Parser)):
    """
    A class to enable early and easy collection of data in a file.

    The `Scannable` class makes it easy to collect two common types of
    information from a data file:

    * A flag to indicate that the data contains one or more lines with a given
      string.
    * a list of lines containing a given string.

    To create a parser from the Scannable parser class, the main job is to
    override the `parse()` method, returning your choice of data structure
    to represent the information in the file.  This takes the form of a
    generator that yields structures for users of your parser.  You can
    yield more than object per line, or you can condense multiple lines into
    one object.  Each object is then scanned with all the defined scanners
    for this class.

    How does that work?  Well, the individual rules using your parser will
    use the `any()` and `collect()` methods on the class object itself to set
    up new attributes of the class that will be given values based on the
    results of a function that checks each object from your parser for the
    properties it's looking for.  That's pretty vague, so let's give some
    examples - imagine a parser defined as:

        class AnacondaLog(Scannable):
            pass

    (Which uses the default parse() function that simply yields each line in
    turn.)  A rule using this parser then does:

        def warnings(line):
            return line if 'WARNING' in line

        def has_fcoe_edd(line):
            return '/usr/libexec/fcoe/fcoe_edd.sh' in line

        AnacondaLog.any('has_fcoe', has_fcoe_edd)
        AnacondaLog.collect('warnings', warnings)

    These then act in the following way:

    * When an object is instantiated from the AnacondaLog class, it will have
      the 'has_fcoe' attribute.  This will be set to True if
      '/usr/libexec/fcoe/fcoe_edd.sh' was found in any line in the file, or
      False otherwise.
    * When an object is instantiated from the AnacondaLog class, it will have
      the 'warnings' attribute.  This will be a list containing all the lines
      found.

    Users of your class can supply any function to either `any()` or
    `collect()`.  Functions given to `collect()` can return anything they want
    to be collected - if they return something that evaluates to `False` then
    nothing is collected (so avoid returning empty lists, empty dicts, empty
    strings or False).

    """

    @classmethod
    def _scan(cls, result_key, scanner):
        """
        Registers a `scanner` which is a function that will be called once per
        logical line in a document. A scanners job is to evaluate the content
        of the line and set a so-called `result_key` on the class to be
        retrieved later by a rule.
        """

        if result_key in cls.scanner_keys:
            raise ValueError("'%s' is already a registered scanner key" % result_key)

        cls.scanners.append(scanner)
        cls.scanner_keys.add(result_key)

    @classmethod
    def any(cls, result_key, func):
        """
        Sets the `result_key` to the output of `func` if `func` ever returns
        truthy
        """
        def scanner(self, obj):
            current_value = getattr(self, result_key, None)
            setattr(self, result_key, current_value or func(obj))

        cls._scan(result_key, scanner)

    @classmethod
    def collect(cls, result_key, func):
        """
        Sets the `result_key` to an iterable of objects for which `func(obj)`
        returns True
        """
        def scanner(self, obj):
            if not getattr(self, result_key, None):
                setattr(self, result_key, [])
            rv = func(obj)
            if rv:
                getattr(self, result_key).append(rv)

        cls._scan(result_key, scanner)

    def parse(self, content):
        """
        Default 'parsing' method. Subclasses should override this method with
        their own custom parsing as necessary.
        """
        for line in content:
            yield line

    def parse_content(self, content):
        for obj in self.parse(content):
            for scanner in self.scanners:
                scanner(self, obj)


class LogFileOutput(six.with_metaclass(ScanMeta, Parser)):
    """Class for parsing log file content.

    Log file content is stored in raw format in the ``lines`` attribute.

    Attributes:
        lines (list): List of the lines from the log file content.

    Examples:
        >>> class MyLogger(LogFileOutput):
        ...     pass
        >>> contents = '''
        Log file line one
        Log file line two
        Log file line three, and more
        '''.strip()
        >>> my_logger = MyLogger(context_wrap(contents, path='/var/log/mylog'))
        >>> my_logger.file_path
        '/var/log/mylog'
        >>> my_logger.file_name
        'mylog'
        >>> my_logger.get('two')
        [{'raw_message': 'Log file line two'}]
        >>> 'three' in my_logger
        True
        >>> my_logger.get(['three', 'more'])
        [{'raw_message': 'Log file line three, and more'}]
        >>> my_logger.lines[0]
        'Log file line one'
    """

    time_format = '%Y-%m-%d %H:%M:%S'
    """
    The timestamp format assumed for the log files.  A subclass can override
    this for files that have a different timestamp format.  This can be:

    * A string in `strptime()` format.
    * A list of `strptime()` strings.
    * A dictionary with each item's value being a `strptime()` string.  This
      allows the item keys to provide some form of documentation.
    """

    def parse_content(self, content):
        """
        Use all the defined scanners to search the log file, setting the
        properties defined in the scanner.
        """
        self.lines = list(content)
        for scanner in self.scanners:
            scanner(self)

    def __contains__(self, s):
        """
        Returns true if any line contains the given text string.
        """
        return any(s in l for l in self.lines)

    def _parse_line(self, line):
        """
        Parse the line into a dictionary and return it. Only wrap with
        `raw_message` by default.
        """
        return {'raw_message': line}

    def _valid_search(self, s):
        """
        Check this given `s`, it must be a string or a list of strings.
        Otherwise, a TypeError will be raised.
        """
        if isinstance(s, six.string_types):
            return lambda l: s in l
        elif (isinstance(s, list) and len(s) > 0 and
              all(isinstance(w, six.string_types) for w in s)):
            return lambda l: all(w in l for w in s)
        elif s is not None:
            raise TypeError('Search items must be given as a string or a list of strings')

    def get(self, s):
        """
        Returns all lines that contain `s` anywhere and wrap them in a list of
        dictionaries.  `s` can be either a single string or a string list. For
        list, all keywords in the list must be found in each line.

        Parameters:
            s(str or list): one or more strings to search for.

        Returns:
            (list): list of dictionaries corresponding to the parsed lines
            contain the `s`.
        """
        ret = []
        search_by_expression = self._valid_search(s)
        for l in self.lines:
            if search_by_expression(l):
                ret.append(self._parse_line(l))
        return ret

    @classmethod
    def scan(cls, result_key, func):
        """
        Define computed fields based on a string to "grep for".  This is
        preferred to utilizing raw log lines in plugins because computed fields
        will be serialized, whereas raw log lines will not.
        """

        if result_key in cls.scanner_keys:
            raise ValueError("'%s' is already a registered scanner key" % result_key)

        def scanner(self):
            result = func(self)
            setattr(self, result_key, result)

        cls.scanners.append(scanner)
        cls.scanner_keys.add(result_key)

    @classmethod
    def token_scan(cls, result_key, token):
        """
        Define a property that is set to true if the given token is found in
        the log file.  Uses the __contains__ method of the log file.
        """
        def _scan(self):
            return token in self

        cls.scan(result_key, _scan)

    @classmethod
    def keep_scan(cls, result_key, token):
        """
        Define a property that is set to the list of lines that contain the
        given token.  Uses the get method of the log file.
        """
        def _scan(self):
            return self.get(token)

        cls.scan(result_key, _scan)

    def get_after(self, timestamp, s=None):
        """
        Find all the (available) logs that are after the given time stamp.

        If `s` is not supplied, then all lines are used.  Otherwise, only the
        lines contain the `s` are used.  `s` can be either a single string or a
        string list. For list, all keywords in the list must be found in each
        line.

        This method then finds all lines which have a time stamp after the
        given `timestamp`.  Lines that do not contain a time stamp
        are considered to be part of the previous line and are therefore
        included if the last log line was included or excluded otherwise.

        Time stamps are recognised by converting the time format into a
        regular expression which matches the time format in the string.  This
        is then searched for in each line in turn.  Only lines with a time
        stamp matching this expression will trigger the decision to include
        or exclude lines. Therefore, if the log for some reason does not
        contain a time stamp that matches this format, no lines will be
        returned.

        The time format is given in ``strptime()`` format, in the object's
        ``time_format`` property.  Users of the object should **not** change
        this property; instead, the parser should subclass
        :class:`LogFileOutput` and change the ``time_format`` property.

        Some logs, regrettably, change time stamps formats across different
        lines, or change time stamp formats in different versions of the
        program.  In order to accommodate this, the timestamp format can be a
        list of ``strptime()`` format strings.  These are combined as
        alternatives in the regular expression, and are given to ``strptime``
        in order.  These can also be listed as the values of a dict, e.g.::

            {'pre_10.1.5': '%y%m%d %H:%M:%S', 'post_10.1.5': '%Y-%m-%d %H:%M:%S'}

        .. note::
            Some logs - notably /var/log/messages - do not contain a year
            in the timestamp.  This detected by the absence of a '%y' or '%Y' in
            the time format.  If that year field is absent, the year is assumed
            to be the year in the given timestamp being sought.  Some attempt is
            made to handle logs with a rollover from December to January, by
            finding when the log's timestamp (with current year assumed) is over
            eleven months (specifically, 330 days) ahead of or behind the
            timestamp date and shifting that log date by 365 days so that it is
            more likely to be in the sought range.  This paragraph is sponsored
            by syslog.

        Parameters:
            timestamp(datetime.datetime): lines before this time are ignored.
            s(str or list): one or more strings to search for.
                If not supplied, all available lines are searched.

        Yields:
            dict:
                The parsed lines with timestamps after this date in
                the same format they were supplied.  It at least contains the
                ``raw_message`` as a key.

        Raises:
            ParseException: If the format conversion string contains a
                format that we don't recognise.  In particular, no attempt is
                made to recognise or parse the time zone or other obscure
                values like day of year or week of year.
        """
        time_format = self.time_format

        # Annoyingly, strptime insists that it get the whole time string and
        # nothing but the time string.  However, for most logs we only have a
        # string with the timestamp in it.  We can't just catch the ValueError
        # because at that point we do not actually have a valid datetime
        # object.  So we convert the time format string to a regex, use that
        # to find just the timestamp, and then use strptime on that.  Thanks,
        # Python.  All these need to cope with different languages and
        # character sets.  Note that we don't include time zone or other
        # outputs (e.g. day-of-year) that don't usually occur in time stamps.
        format_conversion_for = {
            'a': r'\w{3}', 'A': r'\w+',  # Week day name
            'w': r'[0123456]',  # Week day number
            'd': r'([0 ][123456789]|[12]\d|3[01])',  # Day of month
            'b': r'\w{3}', 'B': r'\w+',  # Month name
            'm': r'([0 ]\d|1[012])',  # Month number
            'y': r'\d{2}', 'Y': r'\d{4}',  # Year
            'H': r'([01 ]\d|2[0123])',  # Hour - 24 hour format
            'I': r'([0 ]?\d|1[012])',  # Hour - 12 hour format
            'p': r'\w{2}',  # AM / PM
            'M': r'([012345]\d)',  # Minutes
            'S': r'([012345]\d|60)',  # Seconds, including leap second
            'f': r'\d{6}',  # Microseconds
        }

        # Construct the regex from the time string
        timefmt_re = re.compile(r'%(\w)')

        def replacer(match):
            if match.group(1) in format_conversion_for:
                return format_conversion_for[match.group(1)]
            else:
                raise ParseException(
                    "get_after does not understand strptime format '{c}'".format(
                        c=match.group(0)
                    )
                )

        # Please do not attempt to be tricky and put a regular expression
        # inside your time format, as we are going to also use it in
        # strptime too and that may not work out so well.

        # Check time_format - must be string or list.  Set the 'logs_have_year'
        # flag and timestamp parser function appropriately.
        # Grab values of dict as a list first
        if isinstance(time_format, dict):
            time_format = list(time_format.values())
        if isinstance(time_format, six.string_types):
            logs_have_year = ('%Y' in time_format or '%y' in time_format)
            time_re = re.compile('(' + timefmt_re.sub(replacer, time_format) + ')')

            # Curry strptime with time_format string.
            def test_parser(logstamp):
                return datetime.datetime.strptime(logstamp, time_format)
            parse_fn = test_parser
        elif isinstance(time_format, list):
            logs_have_year = all('%Y' in tf or '%y' in tf for tf in time_format)
            time_re = re.compile('(' + '|'.join(
                timefmt_re.sub(replacer, tf) for tf in time_format
            ) + ')')

            def test_all_parsers(logstamp):
                # One of these must match, because the regex has selected only
                # strings that will match.
                for tf in time_format:
                    try:
                        ts = datetime.datetime.strptime(logstamp, tf)
                    except ValueError:
                        pass
                return ts
            parse_fn = test_all_parsers
        else:
            raise ParseException(
                "get_after does not recognise time formats of type {t}".format(
                    t=type(time_format)
                )
            )

        # Most logs will appear in string format, but some logs (e.g.
        # Messages) are available in list-of-dicts format.  So we choose one
        # of two 'date_compare' functions.  HOWEVER: we still have to check
        # the string found for a valid date, because log parsing often fails.
        # Because of generators, we check this per line

        # Now try to find the time stamp in each log line and add lines to
        # our output if they are currently being included in the log.

        eleven_months = datetime.timedelta(days=330)
        including_lines = False
        search_by_expression = self._valid_search(s)
        for line in self.lines:
            # If `s` is not None, keywords must be found in the line
            if s and not search_by_expression(line):
                continue
            # Otherwise, search all lines
            match = time_re.search(line)
            if match:
                logstamp = parse_fn(match.group(0))
                if not logs_have_year:
                    # Substitute timestamp year for logstamp year
                    logstamp = logstamp.replace(year=timestamp.year)
                    if logstamp - timestamp > eleven_months:
                        # If timestamp in January and log in December, move
                        # log to previous year
                        logstamp = logstamp.replace(year=timestamp.year - 1)
                    elif timestamp - logstamp > eleven_months:
                        # If timestamp in December and log in January, move
                        # log to next year
                        logstamp = logstamp.replace(year=timestamp.year + 1)
                if logstamp >= timestamp:
                    # Later - include
                    including_lines = True
                    yield self._parse_line(line)
                else:
                    # Earlier - start excluding
                    including_lines = False
            else:
                # If we're including lines, add this continuation line
                if including_lines:
                    yield self._parse_line(line)


class Syslog(LogFileOutput):
    """Class for parsing syslog file content.

    The important function is :func:`get(s)`, which finds all lines with the
    string **s** and parses them into dictionaries with the following keys:

    * **timestamp** - the time the log line was written
    * **procname** - the process or facility that wrote the line
    * **hostname** - the host that generated the log line
    * **message** - the rest of the message (after the process name)
    * **raw_message** - the raw message before being split.

    It is best to use filters and/or scanners with the messages log, to speed up
    parsing.  These work on the raw message, before being parsed.

    Examples:

        >>> Syslog.filters.append('wrapper')
        >>> Syslog.token_scan('daemon_start', 'Wrapper Started as Daemon')
        >>> msgs = shared[Syslog]
        >>> len(msgs.lines)
        >>> wrapper_msgs = msgs.get('wrapper') # Can only rely on lines filtered being present
        >>> wrapper_msgs[0]
        {'timestamp': 'May 18 15:13:36', 'hostname': 'lxc-rhel68-sat56',
         'procname': wrapper[11375]', 'message': '--> Wrapper Started as Daemon',
         'raw_message': 'May 18 15:13:36 lxc-rhel68-sat56 wrapper[11375]: --> Wrapper Started as Daemon'
        }
        >>> msgs.daemon_start # Token set if matching lines present in logs
        True

    Sample log lines::

        May 18 15:13:34 lxc-rhel68-sat56 jabberd/sm[11057]: session started: jid=rhn-dispatcher-sat@lxc-rhel6-sat56.redhat.com/superclient
        May 18 15:13:36 lxc-rhel68-sat56 wrapper[11375]: --> Wrapper Started as Daemon
        May 18 15:13:36 lxc-rhel68-sat56 wrapper[11375]: Launching a JVM...
        May 18 15:24:28 lxc-rhel68-sat56 yum[11597]: Installed: lynx-2.8.6-27.el6.x86_64
        May 18 15:36:19 lxc-rhel68-sat56 yum[11954]: Updated: sos-3.2-40.el6.noarch

    .. note::
        Because syslog timestamps by default have no year,
        the year of the logs will be inferred from the year in your timestamp.
        This will also work around December/January crossovers.
    """
    time_format = '%b %d %H:%M:%S'

    def _parse_line(self, line):
        """
        Parsed result::

            {'timestamp':'May 18 14:24:14',
             'procname': 'kernel',
             'hostname':'lxc-rhel68-sat56',
             'message': '...',
             'raw_message': '...: ...'
            }
        """
        msg_info = {'raw_message': line}
        if ': ' in line:
            info, msg = [i.strip() for i in line.split(': ', 1)]
            msg_info['message'] = msg

            info_splits = info.split()
            if len(info_splits) == 5:
                msg_info['timestamp'] = ' '.join(info_splits[:3])
                msg_info['hostname'] = info_splits[3]
                msg_info['procname'] = info_splits[4]
        return msg_info


class IniConfigFile(ConfigParser):
    """
    A class specifically for reading configuration files in 'ini' format.

    The input file format supported by this class is::

           [section 1]
           key = value
           ; comment
           # comment
           [section 2]
           key with spaces = value string
           [section 3]
           # Must implement parse_content in child class
           # and pass allow_no_value=True to parent class
           # to enable keys with no values
           key_with_no_value

    References:
        See Python ``RawConfigParser`` documentation for more information
        https://docs.python.org/2/library/configparser.html#rawconfigparser-objects

    Examples:
        >>> class MyConfig(IniConfigFile):
        ...     pass
        >>> content = '''
        ... [defaults]
        ... admin_token = ADMIN
        ... [program opts]
        ... memsize = 1024
        ... delay = 1.5
        ... [logging]
        ... log = true
        ... logging level = verbose
        ... '''.split()
        >>> my_config = MyConfig(context_wrap(content, path='/etc/myconfig.conf'))
        >>> 'program opts' in my_config
        True
        >>> my_config.sections()
        ['program opts', 'logging']
        >>> my_config.defaults()
        {'admin_token': 'ADMIN'}
        >>> my_config.items('program opts')
        {'memsize': 1024, 'delay': 1.5}
        >>> my_config.get('logging', 'logging level')
        'verbose'
        >>> my_config.getint('program opts', 'memsize')
        1024
        >>> my_config.getfloat('program opts', 'delay')
        1.5
        >>> my_config.getboolean('logging', 'log')
        True
        >>> my_config.has_option('logging', 'log')
        True
    """

    def parse_content(self, content, allow_no_value=False):
        """Parses content of the config file.

        In child class overload and call super to set flag
        ``allow_no_values`` and allow keys with no value in
        config file::

            def parse_content(self, content):
                super(YourClass, self).parse_content(content,
                                                     allow_no_values=True)
        """
        super(IniConfigFile, self).parse_content(content)
        config = RawConfigParser(allow_no_value=allow_no_value)
        fp = io.StringIO(u"\n".join(content))
        config.readfp(fp, filename=self.file_name)
        self.data = config

    def parse_doc(self, content):
        return iniconfig.parse_doc(content)

    def sections(self):
        """list: Return a list of section names."""
        return self.data.sections()

    def defaults(self):
        """list: Return a dict of key/value pairs in the ``[default]`` section."""
        return self.data.defaults()

    def items(self, section):
        """dict: Return a dictionary of key/value pairs for ``section``."""
        return dict(self.data.items(section))

    def get(self, section, key):
        """value: Get value for ``section`` and ``key``."""
        return self.data.get(section, key)

    def getint(self, section, key):
        """int: Get int value for ``section`` and ``key``."""
        return self.data.getint(section, key)

    def getfloat(self, section, key):
        """float: Get float value for ``section`` and ``key``."""
        return self.data.getfloat(section, key)

    def getboolean(self, section, key):
        """boolean: Get boolean value for ``section`` and ``key``."""
        return self.data.getboolean(section, key)

    def has_option(self, section, key):
        """boolean: Returns ``True`` of ``section`` is present and has option
        ``key``."""
        return self.data.has_option(section, key)

    def __contains__(self, section):
        return section in self.data.sections()

    def __repr__(self):
        return "INI file '{filename}' - sections:{sections}".\
            format(filename=self.file_name,
                   sections=self.data.sections())


class FileListing(Parser):
    """
    Reads a series of concatenated directory listings and turns them into
    a dictionary of entities by name.  Stores all the information for
    each directory entry for every entry that can be parsed, containing:

    * type (one of [bcdlps-])
    * permission string including ACL character
    * number of links
    * owner and group (as given in the listing)
    * size, or major and minor number for block and character devices
    * date (in the format given in the listing)
    * name
    * name of linked file, if a symlink

    In addition, the raw line is always stored, even if the line doesn't look
    like a directory entry.

    Also provides a number of other conveniences, such as:

    * lists of regular and special files and subdirectory names for each
      directory, in the order found in the listing
    * total blocks allocated to all the entities in this directory

    .. note:: For listings that only contain one directory, ``ls`` does not
        output the directory name.  The directory is reverse engineered from
        the path given to the parser by Insights - this assumes the
        translation of spaces to underscores and '/' to '.' in paths.  For
        example, ``ls -l /var/www/html`` will be translated to
        ``ls_-l_.var.www.html``.  The reverse translation will make mistakes,
        for example in translating ``.etc.yum.repos.d`` to
        ``/etc/yum/repos/d``.  Use caution in checking the paths when
        requesting single directories.

    Parses SELinux directory listings if the 'selinux' option is True.
    SELinux directory listings contain:

    * the type of file
    * the permissions block
    * the owner and group as given in the directory listing
    * the SELinux user, role, type and MLS
    * the name, and link destination if it's a symlink

    Sample input data looks like this:

        | /example_dir:
        | total 20
        | dr-xr-xr-x.  3 0 0     4096 Mar  4 16:19 .
        | -rw-r--r--.  1 0 0   123891 Aug 25  2015 config-3.10.0-229.14.1.el7.x86_64
        | lrwxrwxrwx.  1 0 0       11 Aug  4  2014 menu.lst -> ./grub.conf
        | brw-rw----.  1 0 6 253,  10 Aug  4 16:56 dm-10
        | crw-------.  1 0 0 10,  236 Jul 25 10:00 control

    Examples:
        >>> '/example_dir' in shared[FileListing]
        True
        >>> shared[FileListing].dir_contains('/example_dir', 'menu.lst')
        True
        >>> dir = shared[FileListing].listing_of('/example_dir')
        >>> dir['.']['type']
        'd'
        >>> dir['config-3.10.0-229.14.q.el7.x86_64']['size']
        123891
        >>> dir['dm-10']['major']
        253
        >>> dir['menu.lst']['link']
        './grub.conf'
    """

    # I know I'm missing some types in the 'type' subexpression...
    # Modify the last '\S+' to '\S*' to match where there is no '.' at the end
    perms_regex = r'^(?P<type>[bcdlps-])' +\
        r'(?P<perms>[r-][w-][sSx-][r-][w-][sSx-][r-][w-][xsSt-]([.+-])?)'
    links_regex = r'(?P<links>\d+)'
    owner_regex = r'(?P<owner>[a-zA-Z0-9_-]+)\s+(?P<group>[a-zA-Z0-9_-]+)'
    # In 'size' we also cope with major, minor format character devices
    # by just catching the \d+, and then splitting it off later.
    size_regex = r'(?P<size>(?:\d+,\s+)?\d+)'
    # Note that we don't try to determine nonexistent month, day > 31, hour
    # > 23, minute > 59 or improbable year here.
    # TODO: handle non-English formatted dates here.
    date_regex = r'(?P<date>\w{3}\s[ 0-9][0-9]\s(?:[012]\d:\d{2}|\s\d{4}))'
    name_regex = r'(?P<name>[^/ ][^/]*?)(?: -> (?P<link>\S.*))?$'
    normal_regex = '\s+'.join((perms_regex, links_regex, owner_regex,
                              size_regex, date_regex, name_regex))
    normal_re = re.compile(normal_regex)

    context_regex = r'(?P<se_user>\w+_u):(?P<se_role>\w+_r):' +\
        r'(?P<se_type>\w+_t):(?P<se_mls>\S+)'
    selinux_regex = '\s+'.join((perms_regex, owner_regex, context_regex,
                               name_regex))
    selinux_re = re.compile(selinux_regex)

    def __init__(self, context, selinux=False):
        """
            You can set the 'selinux' parameter to True to have this
            directory listing parsed as a 'ls -Z' directory listing.
        """
        self.selinux = selinux
        # Pick the right regex to use and save that as a property
        if selinux:
            self.file_re = self.selinux_re
        else:
            self.file_re = self.normal_re
        # Try to pull out the directory path from the command line, in case
        # we're doing an ls on only one directory (which then doesn't list
        # the directory name in the output).  Obviously if we don't have the
        # '-R' flag we should grab this but it's probably not worth parsing
        # the flags to ls for this.
        self.first_path = None
        path_re = re.compile(r'ls_-\w+(?P<path>.*)$')
        match = path_re.search(context.path)
        if match:
            fpath = match.group('path')
            self.first_path = '/' if not fpath else fpath.replace('.', '/').replace('_', ' ')
        super(FileListing, self).__init__(context)

    def parse_file_match(self, this_dir, line):
        # Save all the raw directory entries, even if we can't parse them
        this_dir['raw_list'].append(line)
        match = self.file_re.search(line)
        if not match:
            # Can't do anything more with the line here
            return

        # Get the fields from the regex
        this_file = match.groupdict()
        this_file['raw_entry'] = line
        this_file['dir'] = this_dir['name']
        typ = match.group('type')

        # There's a bunch of stuff that the SELinux listing doesn't contain:
        if not self.selinux:
            # Type conversions
            this_file['links'] = int(this_file['links'])
            # Is this a character or block device?  If so, it should
            # have a major, minor 'size':
            size = match.group('size')
            if typ in 'bc':
                # What should we do if we expect a major, minor size but
                # don't get one?
                if ',' in size:
                    major, minor = match.group('size').split(',')
                    this_file['major'] = int(major.strip())
                    this_file['minor'] = int(minor.strip())
                    # Remove other 'size' entries
                    del(this_file['size'])
            else:
                # What should we do if we get a comma here?
                if ',' not in size:
                    this_file['size'] = int(match.group('size'))

        # If this is not a symlink, remove the link key
        if not this_file['link']:
            del this_file['link']
        # Now add it to our various properties
        file_name = this_file['name']
        this_dir['entries'][file_name] = this_file
        if typ in 'bc':
            this_dir['specials'].append(file_name)
        if typ == 'd':
            this_dir['dirs'].append(file_name)
        else:
            this_dir['files'].append(file_name)

    def parse_content(self, content):
        """
        Called automatically to process the directory listing(s) contained in
        the content.
        """
        listings = {}
        this_dir = {}
        for line in content:
            l = line.strip()
            if not l:
                continue
            if (l.startswith('/') and l.endswith(':')) or (self.first_path):
                # Directory name from output first and context path second
                if (l.startswith('/') and l.endswith(':')):
                    name = l[:-1]
                else:
                    name = self.first_path
                # Unset the first path so we don't come here again.
                self.first_path = None
                # New structures for a new directory
                this_dir = {'entries': {}, 'files': [], 'dirs': [],
                            'specials': [], 'total': 0, 'raw_list': [],
                            'name': name}
                listings[name] = this_dir
            elif l.startswith('total') and l[6:].isdigit():
                this_dir['total'] = int(l[6:])
            elif not this_dir:
                # This state can happen if processing an archive that filtered
                # a file listing due to an old spec definition.
                # Let's just skip these lines.
                continue
            else:
                self.parse_file_match(this_dir, l)

        self.listings = listings
        # No longer need the first path found, if any.
        delattr(self, 'first_path')

    # Now some helpers to make some things easier:
    def __contains__(self, directory):
        """
        Does the given directory appear in this set of directory listings?
        """
        return directory in self.listings

    def files_of(self, directory):
        """
        The list of non-special files (i.e. not block or character files)
        in the given directory.
        """
        return self.listings[directory]['files']

    def dirs_of(self, directory):
        """
        The list of subdirectories in the given directory.
        """
        return self.listings[directory]['dirs']

    def specials_of(self, directory):
        """
        The list of block and character special files in the given directory.
        """
        return self.listings[directory]['specials']

    def total_of(self, directory):
        """
        The total blocks of storage consumed by entries in this directory.
        """
        return self.listings[directory]['total']

    def listing_of(self, directory):
        """
        The listing of this directory, in a dictionary by entry name.  All
        entries contain the original line as is in the 'raw_entry' key.
        Entries that can be parsed then have fields as described in the class
        description above.
        """
        return self.listings[directory]['entries']

    def dir_contains(self, directory, name):
        """
        Does this directory contain this entry name?
        """
        return name in self.listings[directory]['entries']

    def dir_entry(self, directory, name):
        """
        The parsed data for the given entry name in the given directory.
        """
        return self.listings[directory]['entries'][name]

    def path_entry(self, path):
        """
        The parsed data given a path, which is separated into its directory
        and entry name.
        """
        if path[0] != '/':
            return None
        path_parts = path.split('/')
        # Note that here the first element will be '' because it's before the
        # first separator.  That's OK, the join puts it back together.
        directory = '/'.join(path_parts[:-1])
        name = path_parts[-1]
        if directory not in self.listings:
            return None
        if name not in self.listings[directory]['entries']:
            return None
        return self.listings[directory]['entries'][name]

    def raw_directory(self, directory):
        """
        The list of raw lines from the directory, as is.
        """
        return self.listings[directory]['raw_list']


class AttributeDict(dict):
    """
    Class to convert the access to each item in a dict as attribute.

    .. warning::
        Deprecated class, please set attributes explicitly.

    Examples:
        >>> data = {
        ... "fact1":"fact 1"
        ... "fact2":"fact 2"
        ... "fact3":"fact 3"
        ... }
        >>> d_obj = AttributeDict(data)
        {'fact1': 'fact 1', 'fact2': 'fact 2', 'fact3': 'fact 3'}
        >>> d_obj['fact1']
        'fact 1'
        >>> d_obj.get('fact1')
        'fact 1'
        >>> d_obj.fact1
        'fact 1'
        >>> 'fact2' in d_obj
        True
        >>> d_obj.get('fact3', default='no fact')
        'fact 3'
        >>> d_obj.get('fact4', default='no fact')
        'no fact'
    """

    def __init__(self, *args, **kwargs):
        deprecated(AttributeDict, "Please set attributes explicitly.")
        super(AttributeDict, self).__init__(*args, **kwargs)
        self.__dict__ = self
