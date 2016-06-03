#!/usr/bin/python

""" This module provides methods to service the different query types that are
provided through the REST and JSON-RPC interfaces. This is where the real work
is done; jsonapi.wsgi and restapi.wsgi mainly just call the methods here and
return the results."""

import json
import zlib
import logging

import psycopg2
from psycopg2.extensions import AsIs
import redis
from redis.sentinel import Sentinel

# Local imports
import bmrb
from jsonrpc.exceptions import JSONRPCDispatchException as JSONException

# Load the configuration file
configuration = json.loads(open("../../../api_config.json", "r").read())
# Set up logging
logging.basicConfig()

def get_postgres_connection(user=configuration['postgres']['user'],
                            host=configuration['postgres']['host'],
                            database=configuration['postgres']['database']):
    """ Returns a connection to postgres and a cursor."""

    # Errors connecting will be handled upstream
    conn = psycopg2.connect(user=user, host=host, database=database)
    cur = conn.cursor()

    return conn, cur

def get_redis_connection():
    """ Figures out where the master redis instance is (and other paramaters
    needed to connect like which database to use), and opens a connection
    to it. It passes back that connection object."""

    # Connect to redis
    try:
        # Figure out where we should connect
        sentinel = Sentinel(configuration['redis']['sentinels'],
                            socket_timeout=0.5)
        redis_host, redis_port = sentinel.discover_master('tarpon_master')

        # Get the redis instance
        r = redis.StrictRedis(host=redis_host,
                              port=redis_port,
                              password=configuration['redis']['password'],
                              db=configuration['redis']['db'])

        # If the redis instance is being updated during the request then
        #  write a warning to the log
        if not int(r.get("ready")):
            logging.warning("Serviced request during update.")

    # Raise an exception if we cannot connect to the database server
    except (redis.exceptions.ConnectionError,
            redis.sentinel.MasterNotFoundError):
        raise JSONException(-32603, 'Could not connect to database server.')

    return r

def get_valid_entries_from_redis(search_ids, format_="object"):
    """ Given a list of entries, yield the subset that exist in the database
    as the appropriate type as determined by the "format_" variable.

    Valid entry formats:
    nmrstar: Return the entry as NMR-STAR text
    json: Return the entry in serialized JSON format
    dict: Return the entry JSON data as a python dict
    object: Return the PyNMR-STAR object for the entry
    zlib: Return the entry straight from the DB as zlib compressed JSON
    """

    # Wrap the IDs in a list if necessary
    if not isinstance(search_ids, list):
        search_ids = [search_ids]

    # Make sure there are not too many entries
    if len(search_ids) > 500:
        raise JSONException(-32602, 'Too many IDs queried. Please query 500 or '
                                    'fewer entries at a time. You attempted to '
                                    'query %d IDs.' % len(search_ids))

    # Get the connection to redis
    r = get_redis_connection()
    all_ids = r.lrange("loaded", 0, -1)

    valid_ids = []

    # Figure out which IDs in the query exist in the database
    for request_id in [str(x) for x in search_ids]:
        if request_id in all_ids:
            valid_ids.append(request_id)

    # Go through the IDs
    for entry_id in valid_ids:

        entry = r.get(entry_id)

        # See if it is in redis
        if entry:
            # Return the compressed entry
            if format_ == "zlib":
                yield entry

            else:
                # Uncompress the zlib into serialized JSON
                entry = zlib.decompress(entry)
                if format_ == "json":
                    yield entry
                else:
                    # Parse the JSON into python dict
                    entry = json.loads(entry)
                    if format_ == "dict":
                        yield entry
                    else:
                        # Parse the dict into object
                        entry = bmrb.entry.fromJSON(entry)
                        if format_ == "object":
                            yield entry
                        else:
                            # Return NMR-STAR
                            if format_ == "nmrstar":
                                yield str(entry)

                            # Unknown format
                            else:
                                raise JSONException(-32702, "Invalid format: %s"
                                                    "." % format_)

def get_raw_entry(entry_id):
    """ Get one serialized entry. """

    # See if it is a chem comp entry
    if entry_id.startswith("chemcomp_") and entry_id in list_entries(database="chemcomps"):

        #select * from information_schema.tables where table_schema = 'chemcomps';
        #select * from dict.validator_sfcats where internalflag = 'N' order by order_num;

        url = "http://octopus.bmrb.wisc.edu/ligand-expo?what=print&print_alltags=yes&print_entity=yes&print_chem_comp=yes&%s=Fetch" % entry_id[10:]
        chem_frame = bmrb._interpretFile(url).read()
        if chem_frame.strip() == "":
            return json.dumps({"error": "Entry '%s' does not exist in the "
                                        "public database." % entry_id})
        else:
            entry = bmrb.entry.fromString("data_%s\n\n" % entry_id + chem_frame)
        return '{"%s": ' % entry_id + entry.getJSON() + "}"
    else:
        # Look for the entry in Redis
        entry = get_redis_connection().get(entry_id)

        # See if the entry is in the database
        if entry is None:
            return json.dumps({"error": "Entry '%s' does not exist in the "
                                        "public database." % entry_id})
        else:
            return '{"%s": ' % entry_id + zlib.decompress(entry) + "}"

def list_entries(**kwargs):
    """ Returns all valid entry IDs by default. If a database is specified than
    only entries from that database are returned. """

    entry_list = get_redis_connection().lrange("loaded", 0, -1)

    db = kwargs.get("database", None)
    if db:
        if db in ["metabolomics", "macromolecules"]:
            if db == "metabolomics":
                entry_list = [x for x in entry_list if x.startswith("bm")]
            if db == "macromolecules":
                entry_list = [x for x in entry_list if not x.startswith("bm")]
        elif db == "chemcomps":
            res = get_fields_by_fields(["BMRB_code"], "Entity",
                                       schema="chemcomps")
            return ["chemcomp_" + x for x in res["Entity.BMRB_code"]]

    return entry_list

def get_chemical_shifts(**kwargs):
    """ Returns all of the chemical shifts matching the given atom type (if
    specified) and database (if specified)."""


    # Create the search dicationary
    wd = {}
    schema = "macromolecules"

    # See if they specified a specific atom type
    if kwargs.get('atom_type', None):
        wd['Atom_ID'] = kwargs['atom_type'].replace("*", "%").upper()

    # See if they specified a database (a schema)
    if kwargs.get('database', None):
        schema = kwargs['database']

    chem_shift_fields = ["Entry_ID", "Entity_ID", "Comp_index_ID", "Comp_ID",
                         "Atom_ID", "Atom_type", "Val", "Val_err",
                         "Ambiguity_code", "Assigned_chem_shift_list_ID"]

    # Perform the query
    query_result = get_fields_by_fields(chem_shift_fields, "Atom_chem_shift",
                                        as_hash=False, where_dict=wd,
                                        schema=schema)

    return query_result

def get_tags(**kwargs):
    """ Returns results for the queried tags."""

    # Get the valid IDs and redis connection
    search_tags = process_STAR_query(kwargs)
    result = {}

    # Go through the IDs
    for entry in get_valid_entries_from_redis(kwargs['ids']):
        result[entry.bmrb_id] = entry.getTags(search_tags)

    return result

def get_loops(**kwargs):
    """ Returns the matching loops."""

    # Get the valid IDs and redis connection
    loop_categories = process_STAR_query(kwargs)
    result = {}

    # Go through the IDs
    for entry in get_valid_entries_from_redis(kwargs['ids']):
        result[entry.bmrb_id] = {}
        for loop_category in loop_categories:
            matches = entry.getLoopsByCategory(loop_category)

            if kwargs.get('format', "json") == "nmrstar":
                matching_loops = [str(x) for x in matches]
            else:
                matching_loops = [x.getJSON(serialize=False) for x in matches]
            result[entry.bmrb_id][loop_category] = matching_loops

    return result

def get_saveframes(**kwargs):
    """ Returns the matching saveframes."""

    # Get the valid IDs and redis connection
    saveframe_categories = process_STAR_query(kwargs)
    result = {}

    # Go through the IDs
    for entry in get_valid_entries_from_redis(kwargs['ids']):
        result[entry.bmrb_id] = {}
        for saveframe_category in saveframe_categories:
            matches = entry.getSaveframesByCategory(saveframe_category)
            if kwargs.get('format', "json") == "nmrstar":
                matching_frames = [str(x) for x in matches]
            else:
                matching_frames = [x.getJSON(serialize=False) for x in matches]
            result[entry.bmrb_id][saveframe_category] = matching_frames
    return result

def get_entries(**kwargs):
    """ Returns the full entries."""

    # Check their paramters before proceeding
    process_STAR_query(kwargs)
    result = {}

    # Go through the IDs
    format_ = kwargs.get('format', "json")

    if format_ == "nmrstar":
        for entry in get_valid_entries_from_redis(kwargs['ids']):
            result[entry.bmrb_id] = str(entry)
    else:
        for entry in get_valid_entries_from_redis(kwargs['ids'], format_="dict"):
            result[entry.bmrb_id] = entry

    return result

def wrap_it_up(item):
    """ Quote items in a way that postgres accepts and that doesn't allow
    SQL injection."""
    return AsIs('"' + item + '"')

def get_fields_by_fields(fetch_list, table, where_dict=None,
                         schema="macromolecules", modifiers=None, as_hash=True):
    """ Performs a SELECT query constructed from the supplied arguments."""

    # Turn None parameters into the proper empty type
    if where_dict is None:
        where_dict = {}
    if modifiers is None:
        modifiers = []

    # Make sure they aren't tring to inject (paramterized queries are safe while
    # this is not, but there is no way to parameterize a table name...)
    if '"' in table:
        raise JSONException(-32701, "Invalid 'from' parameter.")

    # Errors connecting will be handled upstream
    conn, cur = get_postgres_connection()

    # Prepare the query
    if len(fetch_list) == 1 and fetch_list[0] == "*":
        parameters = []
    else:
        parameters = [wrap_it_up(x) for x in fetch_list]
    query = "SELECT "
    if "count" in modifiers:
        # Build the 'select * from *' part of the query
        query += "count(" + "),count(".join(["%s"]*len(fetch_list))
        query += ') from %s."%s"' % (schema, table)
    else:
        if len(fetch_list) == 1 and fetch_list[0] == "*":
            query += '* from %s."%s"' % (schema, table)
        else:
            # Build the 'select * from *' part of the query
            query += ",".join(["%s"]*len(fetch_list))
            query += ' from %s."%s"' % (schema, table)

    if len(where_dict) > 0:
        query += " WHERE"
        need_and = False

        for key in where_dict:
            if need_and:
                query += " AND"
            if "lower" in modifiers:
                query += " LOWER(%s) LIKE LOWER(%s)"
            else:
                query += " %s LIKE %s"
            parameters.extend([wrap_it_up(key), where_dict[key]])
            need_and = True

    if "count" not in modifiers:
        query += ' ORDER BY "Entry_ID"'
        # Order the parameters as ints if they are normal BMRB IDS
        if schema == "macromolecule":
            query += "::int "

    query += ';'

    # Do the query
    try:
        cur.execute(query, parameters)
        rows = cur.fetchall()
    except psycopg2.ProgrammingError:
        raise JSONException(-32701, "Invalid 'from' parameter.")

    # Get the column names from the DB
    colnames = [desc[0] for desc in cur.description]

    if not as_hash:
        return {'data':rows, 'columns': [table + "." + x for x in colnames]}

    # Turn the results into a dictionary
    result = {}

    if "count" in modifiers:
        for pos, search_field in enumerate(fetch_list):
            result[table + "." + search_field] = rows[0][pos]
    else:
        for search_field in colnames:
            result[table + "." + search_field] = []
            s_index = colnames.index(search_field)
            for row in rows:
                result[table + "." + search_field].append(row[s_index])

    if configuration['debug']:
        result['debug'] = cur.query

    return result


def process_STAR_query(params):
    """ A helper method that parses the keys out of the query and validates
    the 'ids' parameter."""

    # Make sure they have IDS
    if "ids" not in params:
        raise JSONException(-32602, 'You must specify one or more entry IDs '
                                    'with the "ids" parameter.')

    # Set the keys to the empty list if not specified
    if 'keys' not in params:
        params['keys'] = []

    # Wrap the key in a list if necessary
    if not isinstance(params['keys'], list):
        params['keys'] = [params['keys']]

    return params['keys']

def process_select(**params):
    """ Checks the parameters submitted before calling the get_fields_by_fields
    method with them."""

    # Get the database name
    schema = params.get("database", "macromolecules")

    if schema == "combined":
        raise JSONException(-32602, 'Merged database not yet available.')
    if schema not in ["chemcomps", "macromolecules", "metabolomics", "dict"]:
        raise JSONException(-32602, "Invalid database specified.")

    # Okay, now we need to go through each query and get the results
    if not isinstance(params['query'], list):
        params['query'] = [params['query']]

    result_list = []

    select_example = """select distinct cast(T0."ID" as integer) as "Entry.ID"
    from "Entry" T0 join "Citation" T1 on T0."ID"=T1."Entry_ID" join "Chem_comp"
    T2 on T0."ID"=T2."Entry_ID" where T0."ID" ~* '1' and T1."Title" ~* 'T'
    and T2."Entry_ID" ~* '1' order by cast(T0."ID" as integer)"""

    # Build the amalgamation of queries
    for each_query in params['query']:

        # For one query:
        each_query['select'] = each_query.get("select", ["Entry_ID"])
        if not isinstance(each_query['select'], list):
            each_query['select'] = [each_query['select']]
        # We need the ID to join if they are doing multiple queries
        if len(params['query']) > 1:
            each_query['select'].append("Entry_ID")
        if "from" not in each_query:
            raise JSONException(-32602, 'You must specify which table to query '
                                        'with the "from" parameter.')
        if "hash" not in each_query:
            each_query['hash'] = True

        # Get the query modifiers
        each_query['modifiers'] = each_query.get("modifiers", [])
        if not isinstance(each_query['modifiers'], list):
            each_query['modifiers'] = [each_query['modifiers']]

        each_query['where'] = each_query.get("where", {})

        if len(params['query']) > 1:
            # If there are multiple queries then add their results to the list
            cur_res = get_fields_by_fields(each_query['select'],
                                           each_query['from'],
                                           where_dict=each_query['where'],
                                           schema=schema,
                                           modifiers=each_query['modifiers'],
                                           as_hash=False)
            result_list.append(cur_res)
        else:
            # If there is only one query just return it
            return get_fields_by_fields(each_query['select'],
                                        each_query['from'],
                                        where_dict=each_query['where'],
                                        schema=schema,
                                        modifiers=each_query['modifiers'],
                                        as_hash=each_query['hash'])

    return result_list

    # Synchronized list generation - in progress
    common_ids = []
    for pos, result in enumerate(result_list):
        id_pos = params['query'][pos]['select'].index('Entry_ID')
        common_ids.append([x[id_pos] for x in result])

    # Determine the IDs that are in all results
    common_ids = list(set.intersection(*map(set, common_ids)))

    new_response = {}
    for each_id in common_ids:
        for pos, each_query in enumerate(params['query']):
            for field in each_query['select']:
                if each_query['from'] + "." + field not in new_response:
                    new_response[each_query['from'] + "." + field] = []

    return new_response



def create_saveframe_from_db(schema, category, entry_id, id_search_field,
                             cur=None):
    """ Builds a saveframe from the database. You specify the schema:
    (metabolomics, macromolecule, chemcomps, combined), the category of the
    saveframe, the identifier of the saveframe, and the name of the column that
    we should search for the identifier (within the saveframe's table).

    You can optionally pass a cursor to reuse an existing postgresql
    connection."""

    # Connect to the database unless passed a handle
    # Why? If building a whole entry we don't want to have to
    # reconnect a bunch of times. This allows the calling method to
    # provide a connection and cursor.
    if cur is None:
        conn, cur = get_postgres_connection()

    # Set the search path
    cur.execute('''SET search_path=%(path)s, pg_catalog;''', {'path':schema})

    # Check if we are allowed to print it
    cur.execute('''SELECT internalflag,printflag FROM dict.cat_grp
                WHERE sfcategory=%(sf_cat)s ORDER BY groupid''',
                {'sf_cat': category})
    internalflag, printflag = cur.fetchone()

    # Sorry, we won't print internal saveframes
    if internalflag == "Y":
        logging.warning("Something tried to format an internal saveframe: "
                        "%s.%s" % (schema, category))
        return None
    # Nor frames that don't get printed
    if printflag == "N":
        logging.warning("Something tried to format an no-print saveframe: "
                        "%s.%s" % (schema, category))
        return None

    # Get table name from category name
    cur.execute("""SELECT DISTINCT tagcategory FROM dict.val_item_tbl
                WHERE originalcategory=%(category)s AND loopflag<>'Y'""",
                {"category":category})
    table_name = cur.fetchone()[0]

    if configuration['debug']:
        print "Will look in table: %s" % table_name

    # Get the sf_id for later
    cur.execute('''SELECT "Sf_ID","Sf_framecode" FROM %(table_name)s
                WHERE %(search_field)s=%(id)s ORDER BY "Sf_ID"''',
                {"id":entry_id, 'table_name': wrap_it_up(table_name),
                 "search_field": wrap_it_up(id_search_field)})

    # There is no matching saveframe found for their search term
    # and search field
    if cur.rowcount == 0:
        raise JSONException(-32600, "No matching saveframe found.")
    sf_id, sf_framecode = cur.fetchone()

    # Create the NMR-STAR saveframe
    built_frame = bmrb.saveframe.fromScratch(sf_framecode)
    built_frame.tag_prefix = table_name

    # Insert the tags
    cur.execute('''SELECT * FROM %(table_name)s where "Sf_ID"=%(sf_id)s''',
                {'sf_id': sf_id, 'table_name': wrap_it_up(table_name)})
    tag_vals = cur.fetchone()
    for pos, tag in enumerate(cur.description):
        built_frame.addTag(tag.name, tag_vals[pos])

    # Figure out which loops we might need to insert
    cur.execute('''SELECT tagcategory,min(dictionaryseq) AS seq FROM dict.val_item_tbl
                WHERE originalcategory=%(category)s GROUP BY tagcategory ORDER BY seq''',
                {'category': category})

    # The first result is the saveframe, so drop it
    cur.fetchone()

    # Figure out which loops we might need to add
    loops = [x[0] for x in cur.fetchall()]

    # Add the loops
    for each_loop in loops:

        if configuration['debug']:
            print "Doing loop: %s" % each_loop

        # Figure out the loop tags
        cur.execute('''SELECT tagfield,internalflag,printflag,dictionaryseq FROM dict.val_item_tbl
                    WHERE tagcategory=%(loop_name)s ORDER BY dictionaryseq''',
                    {"loop_name": each_loop})
        tags_to_use = []
        for row in cur:
            if configuration['debug']:
                print row
            # Make sure it isn't internal and it should be printed
            if row[1] != "Y":
                # Make sure it should be printed
                if row[2] == "Y" or row[2] == "O":
                    tags_to_use.append(row[0])
                else:
                    if configuration['debug']:
                        print "Skipping noprint tag: %s" % row[0]
            else:
                if configuration['debug']:
                    print "Skipping private tag: %s" % row[0]

        # If there are any tags in the loop to use
        if len(tags_to_use) > 0:
            # Create the loop
            bmrb_loop = bmrb.loop.fromScratch(category=each_loop)
            bmrb_loop.addColumn(tags_to_use)

            # Get the loop data
            to_fetch = ",".join(['"' + x + '"' for x in tags_to_use])
            query = 'SELECT ' + to_fetch
            query += ' FROM %(table_name)s WHERE "Sf_ID" = %(id)s'
            for tag in tags_to_use:
                if "ordinal" in tag or "Ordinal" in tag:
                    query += ' ORDER BY "%s"' % tag
            cur.execute(query, {"id": sf_id,
                                "table_name":wrap_it_up(each_loop)})
            if configuration['debug']:
                print cur.query

            # Add the data
            for row in cur:
                bmrb_loop.addData(row)

            if bmrb_loop.data != []:
                built_frame.addLoop(bmrb_loop)

    return built_frame

def create_combined_view():

    # Connect as the user that has write privileges
    conn, cur = get_postgres_connection(user="bmrb")

    # Create the new schema if needed
    cur.execute("""SELECT EXISTS(SELECT 1 FROM pg_namespace
                WHERE nspname = 'combined');""")
    if cur.fetchall()[0][0] is False:
        cur.execute("CREATE SCHEMA combined;")
        # TODO: Once we have postgres 9.3 replace the above 3 lines with
        # cur.execute("CREATE SCHEMA IF NOT EXISTS combined;")

    # Get the tables we need to combine
    cur.execute('''SELECT table_name,table_schema FROM information_schema.tables
                WHERE table_catalog = 'bmrbeverything' AND
                (table_schema = 'metabolomics' OR table_schema = 'chemcomps'
                 OR table_schema = 'macromolecules');''')
    rows = cur.fetchall()

    # Figure out how to combine them
    combine_dict = {}
    for row in rows:
        if row[0] in combine_dict:
            combine_dict[row[0]].append(row[1])
        else:
            combine_dict[row[0]] = [row[1]]

    for table_name in combine_dict.keys():
        query = ''
        if len(combine_dict[table_name]) == 1:
            print("Warning. Table from only one schema found.")
        elif len(combine_dict[table_name]) == 2:
            query = '''
CREATE OR REPLACE VIEW combined."%s" AS
select * from %s."%s" t
 union all
select * from %s."%s" tt;''' % (table_name,
                    combine_dict[table_name][0], table_name,
                    combine_dict[table_name][1], table_name)
        elif len(combine_dict[table_name]) == 3:
            query = '''
CREATE OR REPLACE VIEW combined."%s" AS
select * from %s."%s" t
 union all
select * from %s."%s" tt
 union all
select * from %s."%s" ttt;''' % (table_name,
                    combine_dict[table_name][0], table_name,
                    combine_dict[table_name][1], table_name,
                    combine_dict[table_name][2], table_name)

        cur.execute(query)
        print query

    cur.execute("GRANT USAGE ON SCHEMA combined to web;")
    cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA combined TO web;")
    cur.execute("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA combined TO web;")

    # Let web see it
    conn.commit()
