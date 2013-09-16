# -*- coding=utf -*-
# import logging
import os.path
import json
import cStringIO
import csv
import codecs
import wildcards
from caching import cacheable

import cubes
from .common import API_VERSION, TEMPLATE_PATH, str_to_bool
from .common import RequestError, ServerError, NotFoundError
from .common import SlicerJSONEncoder
from ..errors import *

try:
    from werkzeug.wrappers import Response
    from werkzeug.utils import redirect
    from werkzeug.exceptions import NotFound
except ImportError:
    from cubes.common import MissingPackage
    _missing = MissingPackage("werkzeug", "Slicer server")
    Response = redirect = NotFound = _missing

try:
    import cubes_search
except ImportError:
    from cubes.common import MissingPackage
    cubes_search = None
    # SphinxSearcher = MissingPackage("cubes_search", "Sphinx search ", 
    #                         source = "https://github.com/Stiivi/cubes")
    # Get cubes sphinx search backend from: https://github.com/Stiivi/cubes

__all__ = (
    "ApplicationController",
    "ModelController",
    "CubesController",
    "SearchController"
)

class ApplicationController(object):
    def __init__(self, args, workspace, logger, config):

        self.workspace = workspace
        self.args = wildcards.proc_wildcards(args)
        self.config = config
        self.logger = logger

        self.locale = self.args.get("lang")
        self.locales = self.workspace.locales

        if config.has_option("server","json_record_limit"):
            self.json_record_limit = config.getint("server","json_record_limit")
        else:
            self.json_record_limit = 1000

        if config.has_option("server","prettyprint"):
            self.prettyprint = config.getboolean("server","prettyprint")
        else:
            self.prettyprint = None

        # Override server settings
        if "prettyprint" in self.args:
            self.prettyprint = str_to_bool(self.args.get("prettyprint"))

        # Read common parameters

        self.page = None
        if "page" in self.args:
            try:
                self.page = int(self.args.get("page"))
            except ValueError:
                raise RequestError("'page' should be a number")

        self.page_size = None
        if "pagesize" in self.args:
            try:
                self.page_size = int(self.args.get("pagesize"))
            except ValueError:
                raise RequestError("'pagesize' should be a number")

        # Collect orderings:
        # order is specified as order=<field>[:<direction>]
        # examples:
        #
        #     order=date.year     # order by year, unspecified direction
        #     order=date.year:asc # order by year ascending
        #

        self.order = []
        for orders in self.args.getlist("order"):
            for order in orders.split(","):
                split = order.split(":")
                if len(split) == 1:
                    self.order.append( (order, None) )
                else:
                    self.order.append( (split[0], split[1]) )


        if 'cache_host' in workspace.options:
            import caching
            import pymongo
            import cPickle as picklee

            ttl = int(workspace.options.get('ttl')) or 60 * 3
            client = pymongo.MongoClient(host=workspace.options['cache_host'])

            self.logger.info("Caching Enabled, host: %s, TTL: %d" % (workspace.options['cache_host'], ttl))

            cache = caching.MongoCache('CubesCache', client, ttl, dumps=caching.response_dumps, loads=caching.response_loads, logger=self.logger)
            self.cache = cache

    def index(self):
        handle = open(os.path.join(TEMPLATE_PATH, "index.html"))
        template = handle.read()
        handle.close()

        context = {}
        context.update(self.server_info())

        doc = template.format(**context)

        return Response(doc, mimetype = 'text/html')

    def server_info(self):
        info = {
            "version": cubes.__version__,
            # Backward compatibility key
            "server_version": cubes.__version__,
            "api_version": API_VERSION
        }
        return info

    def version(self):
        return self.json_response(self.server_info())

    def get_locales(self):
        """Return list of available model locales"""
        return self.json_response(self.locales)

    def json_response(self, obj):
        if self.prettyprint:
            indent = 4
        else:
            indent = None

        encoder = SlicerJSONEncoder(indent = indent)
        encoder.iterator_limit = self.json_record_limit
        reply = encoder.iterencode(obj)

        return Response(reply, mimetype='application/json')

    def json_request(self):
        content_type = self.request.headers.get('content-type')
        if content_type.split(';')[0] == 'application/json':
            try:
                result = json.loads(self.request.data)
            except Exception as e:
                raise RequestError("Problem loading request JSON data", reason=str(e))
            return result
        else:
            raise RequestError("JSON requested should have content type "
                               "application/json, is '%s'" % content_type)


class ModelController(ApplicationController):

    _cached_model_reply = None

    def show(self):
        d = self._cached_model_reply
        if d:
            return Response(d, mimetype='application/json')

        d = {}
        d["cubes"] = self.workspace.list_cubes()
        d["message"] = "this end-point is depreciated"
        d["locales"] = self.locales
        d = json.dumps(d)
        self._cached_model_reply = d

        return Response(d, mimetype='application/json')

    def dimension(self, dim_name):
        # TODO: better message
        raise RequestError("Depreciated")

    def _cube_dict(self, cube):
        d = cube.to_dict(expand_dimensions=True,
                         with_mappings=False,
                         full_attribute_names=True,
                         create_label=True
                         )

        return d

    def get_default_cube(self):
        raise RequestError("Depreciated")

    def get_cube(self, cube_name):
        cube = self.workspace.cube(cube_name)
        return self.json_response(self._cube_dict(cube))

    _cached_cubes_list = None
    def list_cubes(self):
        resp = self._cached_cubes_list
        if resp is None:
            cubes = self.workspace.list_cubes()
            resp = json.dumps(cubes)
            self._cached_cubes_list = resp

        return Response(resp, mimetype='application/json')


class CSVGenerator(object):
    def __init__(self, records, fields, include_header = True,
                dialect=csv.excel, encoding="utf-8", **kwds):
        # Redirect output to a queue
        self.include_header = include_header
        self.records = records
        self.fields = fields
        self.queue = cStringIO.StringIO()
        self.writer = csv.writer(self.queue, dialect=dialect, **kwds)
        self.encoder = codecs.getincrementalencoder(encoding)()

    def csvrows(self):
        if self.include_header:
            yield self._row_string(self.fields)

        for record in self.records:
            row = []
            for field in self.fields:
                value = record.get(field)
                if type(value) == unicode or type(value) == str:
                    row.append(value.encode("utf-8"))
                elif value:
                    row.append(unicode(value))
                else:
                    row.append(None)

            yield self._row_string(row)

    def _row_string(self, row):
        self.writer.writerow(row)
        # Fetch UTF-8 output from the queue ...
        data = self.queue.getvalue()
        data = data.decode("utf-8")
        # ... and reencode it into the target encoding
        data = self.encoder.encode(data)
        # empty queue
        self.queue.truncate(0)

        return data

class UnicodeCSVWriter:
    """
    A CSV writer which will write rows to CSV file "f",
    which is encoded in the given encoding.

    From: <http://docs.python.org/lib/csv-examples.html>
    """

    def __init__(self, f, dialect=csv.excel, encoding="utf-8", **kwds):
        # Redirect output to a queue
        self.queue = cStringIO.StringIO()
        self.writer = csv.writer(self.queue, dialect=dialect, **kwds)
        self.stream = f
        self.encoder = codecs.getincrementalencoder(encoding)()

    def writerow(self, row):
        new_row = []
        for value in row:
            if type(value) == unicode or type(value) == str:
                new_row.append(value.encode("utf-8"))
            elif value:
                new_row.append(unicode(value))
            else:
                new_row.append(None)

        self.writer.writerow(new_row)
        # Fetch UTF-8 output from the queue ...
        data = self.queue.getvalue()
        data = data.decode("utf-8")
        # ... and reencode it into the target encoding
        data = self.encoder.encode(data)
        # write to the target stream
        self.stream.write(data)
        # empty queue
        self.queue.truncate(0)

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)

class CubesController(ApplicationController):
    def create_browser(self, cube_name):
        """Initializes the controller:

        * tries to get cube name
        * if no cube name is specified, then tries to get default cube: either explicityly specified
          in configuration under ``[model]`` option ``cube`` or first cube in model cube list
        * assigns a browser for the controller

        """

        # FIXME: keep or remove default cube?
        if cube_name:
            self.cube = self.workspace.cube(cube_name)
        else:
            if self.config.has_option("model", "cube"):
                self.logger.debug("using default cube specified in cofiguration")
                cube_name = self.config.get("model", "cube")
                self.cube = self.workspace.cube(cube_name)
            else:
                self.logger.debug("using first cube from model")
                cubes = self.workspace.list_cubes()
                cube_name = cubes[0]["name"]
                self.cube = self.workspace.cube(name)

        self.logger.info("browsing cube '%s' (locale: %s)" % (cube_name, self.locale))
        self.browser = self.workspace.browser(self.cube, self.locale)

    def prepare_cell(self):
        cuts = self._parse_cut_spec(self.args.getlist("cut"), 'cell')
        self.cell = cubes.Cell(self.cube, cuts)

    def _parse_cut_spec(self, cut_strings, context):
        if cut_strings:
            cuts = []
            for cut_string in cut_strings:
                self.logger.debug("preparing %s from string: '%s'" % (context, cut_string))
                cuts += cubes.cuts_from_string(cut_string)
        else:
            self.logger.debug("preparing %s as whole cube" % context)
            cuts = []
        return cuts

    @cacheable
    def aggregate(self, cube):
        self.create_browser(cube)
        self.prepare_cell()

        ddlist = self.args.getlist("drilldown")

        measures = []
        mlist = self.args.getlist("measure")
        if mlist:
            for mstring in mlist:
                measures += mstring.split("|")

        drilldown = []

        if ddlist:
            for ddstring in ddlist:
                drilldown += ddstring.split("|")

        split = None
        split_cuts = self._parse_cut_spec(self.args.getlist("split"), 'split')
        if split_cuts:
            split = cubes.Cell(self.cube, split_cuts)

        result = self.browser.aggregate(self.cell,
                                        measures=measures,
                                        drilldown=drilldown, split=split,
                                        page=self.page,
                                        page_size=self.page_size,
                                        order=self.order)

        return self.json_response(result)

    @cacheable
    def facts(self, cube):
        self.create_browser(cube)
        self.prepare_cell()

        format = self.args.get("format")
        format = format.lower() if format else "json"

        fields_str = self.args.get("fields")
        if fields_str:
            fields = fields_str.lower().split(',')
        else:
            fields = None

        result = self.browser.facts(self.cell, order = self.order,
                                    page = self.page,
                                    page_size = self.page_size)

        if format == "json":
            return self.json_response(result)
        elif format == "csv":
            if not fields:
                fields = result.labels
            generator = CSVGenerator(result, fields)
            return Response(generator.csvrows(),
                            mimetype='text/csv')
        else:
            raise RequestError("unknown response format '%s'" % format)

    def fact(self, cube, fact_id):
        self.create_browser(cube)
        fact = self.browser.fact(fact_id)

        if fact:
            return self.json_response(fact)
        else:
            raise NotFoundError(fact_id, "fact", message="No fact with id '%s'" % fact_id)

    def values(self, cube, dimension_name):
        self.create_browser(cube)
        self.prepare_cell()

        depth_string = self.args.get("depth")
        if depth_string:
            try:
                depth = int(self.args.get("depth"))
            except ValueError:
                raise RequestError("depth should be an integer")
        else:
            depth = None

        try:
            dimension = self.cube.dimension(dimension_name)
        except KeyError:
            raise NotFoundError(dim_name, "dimension",
                                message="Dimension '%s' was not found" % dim_name)

        hier_name = self.args.get("hierarchy")
        hierarchy = dimension.hierarchy(hier_name)

        values = self.browser.values(self.cell, dimension, depth=depth,
                                     hierarchy=hierarchy,
                                     page=self.page, page_size=self.page_size)

        depth = depth or len(hierarchy)

        result = {
            "dimension": dimension.name,
            "depth": depth,
            "data": values
        }

        return self.json_response(result)

    def report(self, cube):
        """Create multi-query report response."""
        self.create_browser(cube)
        self.prepare_cell()

        report_request = self.json_request()

        try:
            queries = report_request["queries"]
        except KeyError:
            help = "Wrap all your report queries under a 'queries' key. The " \
                    "old documentation was mentioning this requirement, however it " \
                    "was not correctly implemented and wrong example was provided."
            raise RequestError("Report request does not contain 'queries' key",
                                        help=help)

        cell_cuts = report_request.get("cell")

        if cell_cuts:
            # Override URL cut with the one in report
            cuts = [cubes.cut_from_dict(cut) for cut in cell_cuts]
            cell = cubes.Cell(self.browser.cube, cuts)
            self.logger.info("using cell from report specification (URL parameters are ignored)")
        else:
            cell = self.cell

        result = self.browser.report(cell, queries)

        return self.json_response(result)

    def cell_details(self, cube):
        print self.request
        self.create_browser(cube)
        self.prepare_cell()

        details = self.browser.cell_details(self.cell)
        cell_dict = self.cell.to_dict()

        for cut, detail in zip(cell_dict["cuts"], details):
            cut["details"] = detail

        return self.json_response(cell_dict)

    def details(self, cube):
        raise RequestError("'details' request is depreciated, use 'cell' request")


class SearchController(ApplicationController):
    """docstring for SearchController

    Config options:

    sql_index_table: table name
    sql_schema
    sql_url
    search_backend: sphinx otherwise we raise exception.

    """

    def create_browser(self, cube_name):
        # FIXME: reuse? 
        if cube_name:
            self.cube = self.workspace.cube(cube_name)
        else:
            if self.config.has_option("model", "cube"):
                self.logger.debug("using default cube specified in cofiguration")
                cube_name = self.config.get("model", "cube")
                self.cube = self.workspace.cube(cube_name)
            else:
                self.logger.debug("using first cube from model")
                cube_name = self.workspace.list_cubes()[0]["name"]
                self.cube = self.workspace.cube(cube_name)

        self.browser = self.workspace.browser(self.cube,
                                                  locale=self.locale)

    def create_searcher(self):
        if self.config.has_section("search"):
            self.options = dict(self.config.items("search"))
            self.engine_name = self.config.get("search", "engine")
        else:
            raise CubesError("Search engine not configured.")
        self.logger.debug("using search engine: %s" % self.engine_name)
        options = dict(self.options)
        del options["engine"]
        self.searcher = cubes_search.create_searcher(self.engine_name,
                                            browser=self.browser,
                                            locales=self.locales,
                                            **options)

    def search(self, cube):
        self.create_browser(cube)
        self.create_searcher()


        dimension = self.args.get("dimension")
        if not dimension:
            raise RequestError("No dimension provided for search")

        query = self.args.get("q")
        if not query:
            query = self.args.get("query")

        if not query:
            raise RequestError("No search query provided")

        locale = self.locale
        if not locale and self.locales:
            locale = self.locales[0]

        self.logger.debug("searching for '%s' in %s, locale %s" % (query,
            dimension, locale))

        search_result = self.searcher.search(query, dimension, locale=locale)

        result = {
            "matches": search_result.dimension_matches(dimension),
            "dimension": dimension,
            "total_found": search_result.total_found,
            "locale": self.locale
        }

        if search_result.error:
            result["error"] = search_result.error
        if search_result.warning:
            result["warning"] = search_result.warning

        return self.json_response(result)

