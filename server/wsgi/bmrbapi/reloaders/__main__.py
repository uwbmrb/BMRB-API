#!/usr/bin/env python

""" Populate the Redis and PSQL databases. """

import logging
import optparse
import os
import re
import sys
import time
from multiprocessing import Pipe, cpu_count
from os import _exit as child_exit

from bmrbapi.reloaders.database import one_entry
from bmrbapi.reloaders.molprobity import molprobity
from bmrbapi.reloaders.sql_initialize import sql_initialize
from bmrbapi.reloaders.timedomain import timedomain
from bmrbapi.reloaders.uniprot import uniprot
from bmrbapi.utils import querymod
from bmrbapi.utils.configuration import configuration
from bmrbapi.utils.connections import PostgresConnection, RedisConnection

loaded = {'metabolomics': [], 'macromolecules': [], 'chemcomps': []}
to_process = {'metabolomics': [], 'macromolecules': [], 'chemcomps': []}


def add_to_loaded(loaded_entry):
    """ The entry loaded successfully, so put it in the list of
    loaded entries of the appropriate type based on its name."""

    if loaded_entry.startswith("chemcomp"):
        loaded['chemcomps'].append(loaded_entry)
    elif loaded_entry.startswith("bm"):
        loaded['metabolomics'].append(loaded_entry)
    else:
        loaded['macromolecules'].append(loaded_entry)


def _natural_sort_key(s, _nsre=re.compile('([0-9]+)')):
    """ Use as a key to do a natural sort. 1<12<2<23."""

    if type(s) == bytes:
        s = s.decode()

    return [int(text) if text.isdigit() else text.lower()
            for text in re.split(_nsre, s)]


# Put a few more things in REDIS
def make_entry_list(name: str):
    """ Calculate the list of entries to put in the DB."""

    # Sort the entries
    ent_list = sorted(loaded[name], key=_natural_sort_key)

    if len(ent_list) == 0:
        logging.critical('Could not load the entry set %s - no entries located!', name)
        return

    # Get the old entry list and delete ones that aren't there anymore
    old_entries = r_conn.lrange("%s:entry_list" % name, 0, -1)
    for each_entry in old_entries:
        if each_entry not in ent_list:
            to_delete = "%s:entry:%s" % (name, each_entry)
            if r_conn.delete(to_delete):
                logging.info("Deleted stale entry: %s" % to_delete)

    # Set the update time, ready status, and entry list
    r_conn.hmset("%s:meta" % name, {"update_time": time.time(), "num_entries": len(ent_list)})
    loading = "%s:entry_list" % name + "_loading"
    r_conn.delete(loading)
    r_conn.rpush(loading, *ent_list)
    r_conn.rename(loading, "%s:entry_list" % name)

    dropped = [y[0] for y in to_process[name] if y[0] not in set(loaded[name])]
    logging.info("Entries not loaded in DB %s: %s" % (name, dropped))


# Specify some basic information about our command
opt = optparse.OptionParser(usage="usage: %prog", version="1.0",
                            description="Update the entries in the Redis database.")
opt.add_option("--metabolomics", action="store_true", dest="metabolomics", default=False,
               help="Update the metabolomics entries.")
opt.add_option("--macromolecules", action="store_true", dest="macromolecules", default=False,
               help="Update the macromolecule entries.")
opt.add_option("--chemcomps", action="store_true", dest="chemcomps", default=False, help="Update the chemcomp entries.")
opt.add_option("--molprobity", action="store_true", dest="molprobity", default=False,
               help="Update the MolProbity tables.")
opt.add_option("--timedomain", action="store_true", dest="timedomain", default=False,
               help="Update the timedomain tables.")
opt.add_option("--uniprot", action="store_true", dest="uniprot", default=False, help="Update the UniProt tables.")
opt.add_option("--sql", action="store_true", dest="sql", default=False,
               help="Run the SQL commands to prepare the correct indexes on the DB.")
opt.add_option("--sql-host", action="store", dest='sql_host', default=configuration['postgres']['host'],
               help="Host to run the SQL updater on.")
opt.add_option("--sql-database", action="store", dest='sql_database', default=configuration['postgres']['database'],
               help="Database to run the SQL updater on.")
opt.add_option("--sql-user", action="store", dest='sql_user', default=configuration['postgres']['user'],
               help="User to run the SQL updater as.")
opt.add_option("--all-entries", action="store_true", dest="all", default=False,
               help="Update all the databases, and run all reloaders. Equivalent to: --metabolomics --macromolecules "
                    "--chemcomps --molprobity --uniprot --sql --timedomain")
opt.add_option("--redis-db", action="store", dest="db", default=1, help="The Redis DB to use. 0 is master.")
opt.add_option("--flush", action="store_true", dest="flush", default=False,
               help="Flush all keys in the DB prior to reloading. This will interrupt service until the DB is rebuilt! "
                    "(So only use it on the staging DB.)")
opt.add_option("--verbose", action="store_true", dest="verbose", default=False, help="Be verbose")
# Parse the command line input
(options, cmd_input) = opt.parse_args()

logging.basicConfig()
logger = logging.getLogger()
if options.verbose:
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.WARNING)

# Make sure they specify a DB
if not (options.metabolomics or options.macromolecules or options.chemcomps or options.molprobity or options.uniprot
        or options.sql or options.timedomain or options.all):
    logging.exception("You must specify at least one of the reloaders.")
    sys.exit(1)

# Update the values if all is presented
if options.all:
    options.metabolomics = True
    options.macromolecules = True
    options.chemcomps = True
    options.molprobity = True
    options.uniprot = True
    options.sql = True
    options.timedomain = True

if options.timedomain:
    logger.info('Doing timedomain data reload...')
    timedomain(host=options.sql_host, database=options.sql_database, user=options.sql_user)
    logger.info('Finished timedomain data reload...')


if options.uniprot:
    logger.info('Doing UniProt reload...')
    uniprot(host=options.sql_host, database=options.sql_database, user=options.sql_user)
    logger.info('Finished UniProt reload...')

if options.sql:
    logger.info('Doing SQL initialization...')
    if sql_initialize(host=options.sql_host, database=options.sql_database, user=options.sql_user):
        logger.info('Finished SQL initialization...')
    else:
        logger.exception('SQL reloading exited with exception.')


# Load the metabolomics data
if options.metabolomics:
    entries = querymod.select(["Entry_ID"], "Release", database="metabolomics")
    entries = sorted(set(entries["Release.Entry_ID"]))

    substitution_count = configuration['metabolomics_entry_directory'].count("%s")
    for entry in entries:
        entry_dir = configuration['metabolomics_entry_directory'] % ((entry,) * substitution_count)
        to_process['metabolomics'].append((entry, entry_dir))

# Get the released entries from ETS
if options.macromolecules:
    with PostgresConnection(user=configuration['ets']['user'],
                            host=configuration['ets']['host'],
                            database=configuration['ets']['database'],
                            port=configuration['ets']['port']) as cur:

        cur.execute("SELECT bmrbnum FROM entrylog;")
        all_ids = [x[0] for x in cur.fetchall()]

        cur.execute("SELECT bmrbnum FROM entrylog WHERE status LIKE 'rel%';")
        valid_ids = sorted([int(x[0]) for x in cur.fetchall()])

    substitution_count = configuration['macromolecule_entry_directory'].count("%s")

    # Load the normal data
    for entry_id in valid_ids:
        entry_dir = configuration['macromolecule_entry_directory'] % ((entry_id,) * substitution_count)
        to_process['macromolecules'].append([str(entry_id), entry_dir])

# Load the chemcomps
if options.chemcomps:
    comp_ids = querymod.select(["BMRB_code"], "Entity", database="chemcomps")
    comp_ids = comp_ids['Entity.BMRB_code']
    chemcomps = ["chemcomp_" + x for x in comp_ids]
    to_process['chemcomps'].extend([[x, None] for x in chemcomps])

# Generate the flat list of entries to process
to_process['combined'] = (to_process['chemcomps'] + to_process['macromolecules'] + to_process['metabolomics'])

# If specified, flush the DB
if options.flush:
    logging.info("Flushing the DB.")
    with RedisConnection(db=options.db) as r:
        r.flushdb()

processes = []
num_threads = cpu_count()

for thread in range(0, num_threads):

    # Set up the pipes
    parent_conn, child_conn = Pipe()
    # Start the process
    processes.append([parent_conn, child_conn])

    # Use the fork to get through!
    new_pid = os.fork()
    # Okay, we are the child
    if new_pid == 0:

        # Each child gets a Redis
        with RedisConnection(db=options.db) as red:

            child_conn.send("ready")
            while True:
                parent_message = child_conn.recv()
                if parent_message == "die":
                    child_conn.close()
                    parent_conn.close()
                    child_exit(0)

                # Do work based on parent_message
                result = one_entry(parent_message[0], parent_message[1], red)

                # Tell our parent we are ready for the next job
                child_conn.send(result)

    # We are the parent, don't need the child connection
    else:
        child_conn.close()

# Check if entries have completed by listening on the sockets
while len(to_process['combined']) > 0:

    time.sleep(.001)
    # Poll for processes ready to listen
    for process in processes:
        if process[0].poll():
            data = process[0].recv()
            if data:
                if data != "ready":
                    add_to_loaded(data)
            process[0].send(to_process['combined'].pop())
            break

# Reap the children
for thread in range(0, num_threads):
    # Get the last ready message from the child
    data = processes[thread][0].recv()
    # Tell the child to shut down
    processes[thread][0].send("die")

    res = os.wait()
    if data:
        add_to_loaded(data)

if options.metabolomics or options.macromolecules or options.chemcomps:
    with RedisConnection(db=options.db) as r_conn:
        # Use a Redis list so other applications can read the list of entries
        if options.metabolomics:
            make_entry_list('metabolomics')
        if options.macromolecules:
            make_entry_list('macromolecules')
        if options.chemcomps:
            make_entry_list('chemcomps')

        # Make the full list from the existing lists regardless of update type
        loaded['combined'] = (r_conn.lrange('metabolomics:entry_list', 0, -1) +
                              r_conn.lrange('macromolecules:entry_list', 0, -1) +
                              r_conn.lrange('chemcomps:entry_list', 0, -1))
        make_entry_list('combined')

# MolProbity should run last since it takes so long
if options.molprobity:
    logger.info('Doing MolProbity reload...')
    molprobity(host=options.sql_host, database=options.sql_database, user=options.sql_user)
    logger.info('Finished MolProbity reload...')
