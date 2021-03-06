r"""A module to pull course information from the UF Registrar's Schedule of
Courses (http://registrar.ufl.edu/soc/). Let's say you wanted to see who taught
Calculus 3 (MAC 2313) during the Fall 2011 semester::
    
    instructors = set()
    reader = CourseReader(2011, lib.tasks.courses.Semesters.FALL)
    for course in reader.lookup_course("MAC2313"):
        instructors.update(course.instructors)
    print("\n".join(instructors))

..
"""

from .. import *
from ...browser import parsers
from .. import courses
from ..courses import fuzzy_match
from . import department_matching
import time
import logging

logger = logging.getLogger("lib.tasks.registrar.course_listings")

class CourseReader(BaseUFTaskManager, BaseTaskManager):
    """Generates the url for, and uses the Registrar list of courses. If a
    matching url cannot be found or generated, a ``KeyError`` will be raised.
    
    *Keyword arguments:*
    
    ``year``
        Either a string or an int representing the year to look into, such as
        "2011". Please note that the registrar's records only date back to 2001.
    ``semester``
        A value from :class:`lib.tasks.courses.Semesters`.
    ``full``
        A boolean, ``True`` if you want all the course listings, ``False`` if
        you are an online student, and only want the web-based course listings.
    ``browser``
        A browser to use. If passed ``None``, a new one is automatically
        created.
    """
    def __init__(self, year, semester, full=True, browser=None):
        BaseUFTaskManager.__init__(self)
        BaseTaskManager.__init__(self, browser)
        
        year = int(year)
        # check argument validity
        if year < 2001:
            raise KeyError(
                "Course listings are unavailable for the year %d." % year
            )
        if not full and year < 2005:
            raise KeyError(
                "Web-Course listings are unavailable for the year %d." % year
            )
        
        if semester == courses.Semesters.SPRING:
            month = "01"
        elif semester == courses.Semesters.SUMMER:
            month = "06"
        elif semester == courses.Semesters.FALL:
            month = "08"
        self.__base_url = "http://www.registrar.ufl.edu/soc/%d%s/%s/" % \
                          (year, month, "all" if full else "web")
        self.__departments = None
        self.__loaded = False
    
    def get_base_url(self):
        """Gets the value of :attr:`base_url`."""
        return self.__base_url
    
    base_url = property(get_base_url, doc="""
        The url of the main schedule of courses page for a specific year and
        semester, such as http://www.registrar.ufl.edu/soc/201201/all/ .""")
    
    def get_departments(self):
        """Gets the value of :attr:`departments`."""
        self.auto_load()
        return self.__departments
    
    departments = property(get_departments, doc="""
        A tuple of :class:`Department` objects, representing all the departments
        that the schedule of courses lists.""")
    
    def lookup_prefix(self, prefix, fast=True):
        """Gives an iterator of all :class:`Department` objects in
        :attr:`departments` that contain courses with the given string prefix.
        If ``fast`` is ``False``, we will call :meth:`auto_load` on each
        :class:`Department` object, ensuring an accurate lookup, at the cost of
        a (potentially) very slow lookup. (see the documentation for
        :attr:`prefixes`)"""
        for dep in self.departments:
            if prefix.upper() in dep.get_prefixes(fast):
                yield dep
    
    def lookup_course(self, course_code, fast=True):
        """Gives an iterator of all the :class:`lib.tasks.courses.Course`
        objects contained by :class:`Department` objects in :attr:`departments`.
        ``course_code`` may be a string or a ``lib.tasks.courses.CourseCode``.
        The ``fast`` argument gets passed through to :meth:`lookup_prefix`.
        """
        if not hasattr(course_code, "prefix"):
            course_code = courses.CourseCode(course_code)
        for dep in self.lookup_prefix(course_code.prefix, fast):
            found = False
            for course in dep.course_list:
                if course.course_code == course_code:
                    yield course
                    found = True # any more instances of this course will only
                                 # exist within this one department.
            if found:
                break
    
    def auto_load(self):
        """Checks to see if the department page has been loaded before. If not,
        it loads it (calling :func:`force_load`)."""
        if not self.__loaded:
            self.force_load()
    
    def force_load(self):
        """Loads the department page, regardless of if it has already been
        loaded or not. It typically makes more sense to call :func:`auto_load`
        instead of this one, unless you need to refresh the page data for some
        reason.
        
        .. note::
            Calling this function, in addition to loading the webpage, performs
            some moderately CPU intensive tasks to attempt to determine what
            department name matches to what department name (with fuzzy string
            matching). This shouldn't be much of a concern though, as the
            processing shouldn't take more than about a tenth of a second. The
            largest bottleneck is still probably the page load time.
        """
        # process the raw html data into an intermediate form
        lxml_source = self.browser.load_page(self.base_url,
                                             parser=parsers.lxml_html)
        # get department names and thir html page names from the dropdown menu
        department_menu = self.__parse_department_menu(lxml_source.cssselect(
            ".soc_menu select"
        )[0])
        # get prefixes and matching department names from the central table
        # Note: There can be multiple prefixes for each department, and multiple
        #       departments for each prefix
        prefix_table = self.__parse_course_prefix_table(lxml_source.cssselect(
            "#soc_content table.filterable"
        )[0])
        
        start_time = time.time() # used for benchmarking how fast our pairing is
        
        # We need to build Department objects from all the data we have.
        # Unfortunately, UF calls departments by different names, depending on
        # where they're listed! This means that we need to bring in
        # lib.tasks.courses.fuzzy_match to align things!
        
        # build a list of department names according to department_menu
        department_menu_department_names = next(zip(*department_menu)) # unzip
        
        # build a list of department names according to prefix_table
        prefix_table_department_names = list(zip(*prefix_table))[1] # unzip
        
        # Build a list of department names, and their similars
        # I cannot begin to tell you how much work it was to get this working,
        # and to get it working relatively fast
        matched_depts = fuzzy_match.similar_zip(
            department_menu_department_names,
            set(prefix_table_department_names),
            scoring_algorithm=department_matching.scoring_algorithm,
            key=lambda v:department_matching.replace_abbreviations(v.lower()),
            single_match=True,
            high_is_similar=True,
            direct_first=True, # optimization
            max_processes=5    # optimization
        )
        
        # make sure we haven't lost any departments
        assert len(matched_depts) == len(department_menu_department_names)
        
        logger.debug("Matched department names:\n    %s" %
                     "\n    ".join(" -> ".join(i) for i in matched_depts))
        logger.info("Pairing %d department names took %.2f seconds." %
                    (len(matched_depts), time.time() - start_time))
        
        # build lookup tables to make constructing Department objects easier
        url_lookup = dict(department_menu)
        prefix_lookup = self.__parallel_lists_to_tuple_dict(
                                            *reversed(list(zip(*prefix_table))))
        departments = []
        for department_menu_name, prefix_table_name in matched_depts:
            # use the department names from prefix_table for the primary name,
            # because they are typically written out in a cleaner format
            alternate_names = [department_menu_name] if \
                              department_menu_name != prefix_table_name else []
            departments.append(Department(prefix_table_name, alternate_names,
                                          prefix_lookup[prefix_table_name],
                                          self.browser, self.base_url,
                                          url_lookup[department_menu_name]))
        self.__departments = tuple(departments)
        self.__loaded = True
    
    def __parse_course_prefix_table(self, table):
        """Given the lxml table element of course-prefix mappings, returns a
        list of tuples in the format
        ``("3-Letter Prefix Code", "Department Name")``"""
        result_list = []
        for row in table.cssselect("tr")[1:]:
            result_list.append(tuple(
                cell.text_content().strip() for cell in row.cssselect("td")
            ))
        return result_list
    
    def __parse_department_menu(self, menu):
        """Given the lxml drop-down list element of courses, returns a list of
        tuples in the format ``("DEPARTMENT NAME", "course_page.html")``"""
        options = menu.cssselect("option")[1:] # drop the first, garbage value
        result_list = []
        for o in options:
            name = o.text_content().strip()
            html_page = o.get("value").strip()
            result_list.append((name, html_page))
        return result_list
    
    def __parallel_lists_to_tuple_dict(self, list_a, list_b):
        r"""Given two lists, where list_a can have repeated content, creates a
        dictionary of tuples, where each tuple contains all the matching values
        in ``list_b`` for the repeated ``list_a`` key elements. For example,
        given the two lists::
            
            list_a = ["key_a", "key_b", "key_a", "key_a", "key_c", "key_b"]
            list_b = [0,       1,       2,       3,       4,       5      ]
        
        This function would return the dictionary::
            
            {"key_a":(0, 2, 3), "key_b":(1, 5), "key_c":(4,)}
        
        ..
        """
        tuple_dict = {}
        for a, b in zip(list_a, list_b):
            b_list = tuple_dict.get(a, [])
            b_list.append(b)
            tuple_dict[a] = b_list
        # convert dictionary values to tuples
        for key in tuple_dict:
            tuple_dict[key] = tuple(tuple_dict[key])
        return tuple_dict

class Department:
    """A class for the objects produced by :class:`CourseReader`. Typically, you
    will consume instances of this class created by a :class:`CourseReader`
    object, rather than instantiating them yourself."""
    
    def __init__(self, name, alternate_names=[], prefixes=[],
                 browser=None, base_url=None, relative_url=None):
        self.__name = name
        self.__alternate_names = alternate_names
        self._abbreviated_names = \
            [department_matching.replace_abbreviations(i) \
             for i in [name] + alternate_names]
        self.__prefixes = tuple(prefixes)
        self.__browser = browser
        self.__base_url = base_url
        self.__relative_url = relative_url
        self.__course_list = None
        self.__loaded = False
    
    def get_name(self):
        """Gets the value of :attr:`name`."""
        return self.__name
    
    name = property(get_name, doc="""
        A human-readable string containing the name of this department.""")
    
    def get_alternate_names(self):
        """Gets the value of :attr:`alternate_names`."""
        return self.__alternate_names
    
    alternate_names = property(get_alternate_names, doc="""
        A list of (non-primary) alias names (some of which may be guesses) that
        we've seen before. (UF doesn't refer to their departments the same way
        everywhere, so this is part of an attempt to compensate for that.)
        """)
    
    def get_all_names(self):
        """Gets the value of :attr:`all_names`."""
        return [self.name] + self.alternate_names
    
    all_names = property(get_all_names, doc="""
        The concatenation of :attr:`name` and :attr:`alternate_names`.""")
    
    def get_prefixes(self, fast=True):
        """Gets the value of :attr:`prefixes`. If ``fast`` is ``False``,
        :meth:`auto_load` gets called first, guaranteeing accurate results, at
        the cost of an extra page load (if the department page hasn't been
        loaded yet)."""
        if not fast:
            self.auto_load()
        return self.__prefixes
    
    prefixes = property(get_prefixes, doc="""
        A tuple of 3-letter prefix strings that match up with course codes,
        which this department provides. If one wants to determine if a course is
        (potentially) contained by this department, given a
        :class:`lib.tasks.courses.CourseCode` object, they could do something
        similar to::
            
            if course_code.prefix in department_object.prefixes:
                print("%s might be available in %s" % (course_code,
                                                       department_object.name))
            else:
                print("%s is not available in %s" % (course_code,
                                                     department_object.name))
        
        If :meth:`auto_load` has been called before (indicated by
        :attr:`loaded`), this list will be entirely accurate, meaning the tuple
        will contain the prefixes for all the courses offered by the department,
        and nothing more. However, if :attr:`loaded` is ``False``, a heuristic
        is used instead. When using the heuristic, the results should be valid
        most of the time, they are not always.
        """)
    
    def get_browser(self):
        """Gets the value of :attr:`browser`."""
        return self.__browser
    
    browser = property(get_browser, doc="""
        The :class:`lib.browser.Browser` instance passed down from our parent
        :class:`CourseReader`.""")
    
    def _get_base_url(self):
        """Gets the value of :attr:`_base_url`"""
        return self.__base_url
    
    _base_url = property(_get_base_url, doc="""
        The value of :attr:`CourseReader.base_url` passed down to us from our
        parent, upon instantiation.""")
    
    def _get_relative_url(self):
        """Gets the value of :attr:`relative_url`."""
        return self.__relative_url
    
    _relative_url = property(_get_relative_url, doc="""
        The url of the html page for this department, relative to
        :attr:`_base_url`.""")
    
    def _get_url(self):
        """Gets the value of :attr:`_url`."""
        return self.browser.expand_relative_url(self._relative_url,
                                                relative_to=self._base_url)
    
    _url = property(_get_url, doc="""
        The absolute url to the department's html page.""")
    
    def get_course_list(self):
        """Gets the value of :attr:`course_list`."""
        self.auto_load()
        return self.__course_list
    
    course_list = property(get_course_list, doc="""
        A :class:`lib.tasks.courses.CourseList` object filled with
        :class:`lib.tasks.courses.Course` objects. The ``course_code``,
        ``section_number``, ``title``, ``credits``, ``gen_ed_credit``,
        ``gordon_rule``, ``instructors``, and ``meetings`` fields are populated
        in each :class:`lib.tasks.courses.Course` object.""")
    
    def get_loaded(self):
        """Gets the value of :attr:`loaded`."""
        return self.__loaded
    
    loaded = property(get_loaded, doc="""
        ``True`` if the department page has been loaded before. This value is
        useful when determining the accuracy of :attr:`prefixes`, and as a hint
        about the performance of certain operations on this object (if this
        value is ``True``, certain values are already cached.).""")
    
    def auto_load(self):
        """If :attr:`loaded` is ``False``, this will call :attr:`force_load`,
        otherwise it will do nothing."""
        if not self.loaded:
            self.force_load()
    
    def force_load(self):
        """Regardless of whether or not :attr:`loaded` is ``True``, loads the
        department page."""
        # Load the department page's html and feed it to lxml:
        lxml_source = self.browser.load_page(self._url,
                                             parser=parsers.lxml_html)
        # We're only concerned about the table of courses: pull that out
        department_table = lxml_source.cssselect("#soc_content table")[1]
        department_table_rows = department_table.cssselect("tr")
        # The first few rows are are information about the department (0-2).
        #     We're not doing anything with them, so we'll just ignore them
        # Then we have the headers for the course table, we'll use these values
        #     as keys in a bunch of little dictionaries.
        header_row = department_table_rows[2]
        # The rest of the table contains the data about the courses
        course_rows = department_table_rows[3:]
        # Some data rows may contain junk comment data, discard it
        course_rows = [r for r in course_rows \
                       if not r.cssselect("th.soc_comment")]
        # process each header cell, converting lxml tags to strings
        headers = [i.text.strip().lower() for i in
                   header_row.cssselect(".colhelp a")]
        def stripped_or_none(tag): # utility function: gives stripped version of
                                   # a tag, or None if it's empty
            stripped = tag.text_content().strip()
            return stripped if stripped else None
        # turn each row in the table into little dicts, where we can look up
        #     data by the column (specified by the header)
        course_dicts = [
            dict(zip(headers, [stripped_or_none(i) for i in row]))
            for row in course_rows
        ]
        
        # We're done with our first stage of processing. Now we'll convert each
        # little dict into a Course object, and shove them all into a CourseList
        
        base_course_list = [] # the list we'll later build our CourseList from
        def build_meeting(d): # utility funciton: builds a meeting given a dict
            if not d["day(s)"] or "tba" in d["day(s)"].lower():
                return None
            return courses.CourseMeeting(days=d["day(s)"], periods=d["period"],
                                         building=d["bldg"], room=d["room"])
        for d in course_dicts:
            if d["course"]:
                credits = int(d["cred"]) if "var" not in d["cred"].lower() \
                                         else -1
                meeting = build_meeting(d)
                c = courses.Course(d["course"], d["sect"],
                                   title=d["course title & textbook(s)"],
                                   credits=credits,
                                   meetings=[meeting] if meeting else [],
                                   gen_ed_credit=d["ge"], gordon_rule=d["wm"],
                                   instructors=[i.strip() for i in
                                                d["instructor(s)"].split("\n")])
                base_course_list.append(c)
            else:
                meeting = build_meeting(d)
                if meeting:
                    base_course_list[-1].meetings.append(meeting)
        
        self.__course_list = courses.CourseList(base_course_list)
        
        # Using the course list, find the prefixes for this department
        self.__prefixes = []
        for c in self.__course_list:
            if c.course_code.prefix not in self.__prefixes:
                self.__prefixes.append(c.course_code.prefix)
        self.__prefixes = tuple(self.__prefixes)
        
        self.__loaded = True
    
    def rate_similarity(self, department_name, fast=False):
        """Returns a score from 0 to 1, rating how similar a department name is
        the primary or secondary (alternate) department names for this
        department, thus giving an idea of how likely a department name string
        is to be referring to this department. The ``fast`` argument determines
        what algorithm is used for generating the similarity score."""
        if fast:
            algorithm = department_name.scoring_algorithm
        else:
            algorithm = fuzzy_match.lev_ratio
        department_name = department_matching.\
                                        replace_abbreviations(department_name)
        return max(algorithm(department_name, i)
                   for i in self._abbreviated_names)
    
    def __str__(self):
        """Gives a simple one-line string with the name and matching prefixes of
        the department."""
        return "Department: %s; Prefixes: %s%s" % (
            self.name,
            " ".join(self.prefixes) if self.prefixes else "???",
            "" if self.__loaded else " (best guess)"
        )
