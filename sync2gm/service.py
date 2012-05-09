#!/usr/bin/env python

"""A server that syncs a local database to Google Music."""

import socket
from collections import namedtuple
import threading
import time
import contextlib
from functools import partial
from contextlib import closing
import os
import sqlite3
import json
import SocketServer


from gmusicapi import *
import appdirs



### The filenames making up a complete configuration.
config_fn = 'config'
#stores a dict encoding. keys:
#     db_path: the path of the mediaplayer database
#     mp_type: the mediaplayer type
#
change_fn = 'last_change'
id_db_fn = 'gmids.db'


#Defines the tables in the id mapping database. Keys are HandlerResult.item_types.
item_to_table = {'song': 'GMSongIds', 'playlist': 'GMPlaylistIds'}


### Various data structures used to define a config for a media player db.

#The configuration for a media player: the action pairs and how to connect.
MPConf = namedtuple('MPConf', ['action_pairs', 'make_connection'])

#A trigger/handler pair. A list of these defines how to respond to db changes.
ActionPair = namedtuple('ActionPair', ['trigger', 'handler'])

#A definition of a trigger.
TriggerDef = namedtuple('TriggerDef', ['name', 'table', 'when', 'id_text'])

#Holds the result from a handler, so the service can keep local -> remote mapping up to date.
# action: one of {'create', 'delete'}. Updates can just return an empty HandlerResult.
# itemType: one of {'song', 'playlist'}
# gm_id: <string>
HandlerResult = namedtuple('HandlerResult', ['action', 'item_type', 'gm_id'])

class GMSyncError(Exception):
    """Base class for any error originating from the service."""
    pass

class UnmappedId(GMSyncError):
    """Raised when we expect that a mapping exists between local/remote ids,
    but one does not."""
    pass


#A mediaplayer config defines handlers.
#These provide code for pushing out changes.

#They do not need to check for success, but can raise CallFailure,
# sqlite.Error or UnmappedId, which the service will handle.

#All handlers that create/delete remote items must return a HandlerResult.
#This allows the service to keep track of local -> remote mappings.

class Handler:
    """A Handler can push out local changes to Google Music.

    A mediaplayer config defines one for each kind of local change (eg the addition of a song)."""

    def __init__(self, local_id, api, mp_conn, gmid_conn, get_gm_id):
        """Create an instance of a Handler. This is done by the service when a specific change is detected."""
 
        self.local_id = local_id
        self.api = api

        #A cursor for the mediaplayer database.
        self.mp_cur = mp_conn.cursor()

        #A cursor for the id database - this shouldn't be needed in mediaplayer configs, they use gm{s,p}id.
        self.id_cur = gmid_conn.cursor()
        self._get_gm_id = get_gm_id #a func that takes localid, item_type, cursor and returns the matching GM id, or raises UnmappedId

    @property
    def gms_id(self):
        return self._get_gm_id(self.local_id, 'song', self.id_cur)

    @property
    def gmp_id(self):
        return self._get_gm_id(self.local_id, 'playlist', self.id_cur)

    def push_changes(self):
        """Send changes to Google Music. This is implemented in mediaplayer configurations.

        This function does not need to handle failure. The service will handle gmusicapi.CallFailure, 
        sqlite3.Error, or sync2gm.UnmappedId.

        api (already authenticated), mp_cur, gms_id, and gmp_id are provided for convinience."""

        raise NotImplementedError


#Dirty. There's an import loop with the mp config that needs the above structures.
#They should probably be moved elsewhere instead of doing this.
from mediamonkey import config as mm_config
### Map mediaplayer type to config
mp_confs = {'mediamonkey': mm_config}


### Utility functions involved in attaching/detaching from the local db.

def create_trigger(change_type, triggerdef, conn):
    keys = triggerdef._asdict()
    keys['change_type'] = change_type

    with conn:
        conn.execute("""
            CREATE TRIGGER {name} {when} ON {table}
            BEGIN
            INSERT INTO sync2gm_Changes (changeType, localId) VALUES ({change_type}, {id_text});
            END
            """.format(**keys))

def drop_trigger(triggerdef, conn):
    with conn:
        conn.execute("DROP TRIGGER IF EXISTS {name}".format(name=triggerdef.name))

def create_service_table(conn, num_triggers):
    with conn:
        conn.execute(
            """CREATE TABLE sync2gm_Changes(
changeId INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
changeType INTEGER CHECK (changeType BETWEEN 0 AND {changes}),
localId INTEGER NOT NULL
)""".format(changes=num_triggers))

def drop_service_table(conn):
    with conn:
        conn.execute("DROP TABLE IF EXISTS sync2gm_Changes")
            

def attach(conn, action_pairs):
    success = False

    try:
        create_service_tables(conn, len(action_pairs))

        for i in range(len(action_pairs)):
            triggerdef = action_pairs[i].trigger
            create_trigger(i, triggerdef, conn)

        success = True

    except sqlite3.Error:
        success = False

        detach(conn)

    finally:
        return success

def detach(conn, action_pairs):
    success = False

    try:
        drop_service_tables(conn)
        
        for triggerdef, handler in action_pairs:
            drop_trigger(triggerdef, conn)    

        success = True

    except sqlite3.Error:
        success = False
        
    finally:
        return success

def reattach(conn, action_pairs):
    return detach(conn, action_pairs) and attach(conn, action_pairs)



### Utilities for writing/reading configuration.

@contextlib.contextmanager
def backed_up(filename):
    """Context manager to back up a file and remove the backup.

    *filename*.bak will be overwritten in the process, if it exists.
    """

    exists = os.path.isfile(filename)
    bak_name = filename+'.bak'

    if exists: os.rename(filename, bak_name)
    try:
        yield
        #if we terminate unexpectedly (eg a reboot), 
        # the backup will remain
    finally:
        if exists: os.remove(bak_name)

def atomic_write(filename, text):
    """Return True if *filename* is overwritten with *text* successfully. The write will be atomic.

    *filename*.tmp may be overwritten.
    """

    tmp_name = filename+'.tmp'

    try:
        with open(tmp_name, 'w') as tmp:
            tmp.write(str(text))

        #this _should_ be atomic cross-platform
        with backed_up(filename):
            os.rename(tmp_name, filename)            

    except Exception as e:
        #TODO warn that bak may be able to be restored.
        return False


    return True

def get_conf_dir(confname):
    """Return the directory for this *confname*, with a trailing separator."""
    conf_dir = appdirs.user_data_dir(appname='sync2gm', appauthor='Simon Weber', version=confname)
    conf_dir += os.sep    

    return conf_dir

def get_conf_fn(confname):
    return get_conf_dir(confname) + config_fn

def write_conf_file(confname, config):
    """Given a dict, *config*, encode it and create or overwrite given filename."""
    with open(get_conf_fn(confname), 'w') as f:
        json.dump(config, f)

def read_config_file(confname):
    """Returns a dictionary of the configuration stored in *filename*."""
    with open(get_conf_fn(confname)) as f:
        return json.load(f)


def init_config(confname, mp_type, mp_db_fn):
    """Attach to the local database, and create or overwrite the configuration for the given *confname*.
    """

    conf_dir = get_conf_dir(confname)
    conf_fn = get_conf_fn(confname)

    #Ensure the conf dir exists.
    if not os.path.isdir(conf_dir):
        os.makedirs(conf_dir)

    #(re)create the config file.
    conf_dict = {'mp_type': mp_type, 'mp_db_fn': mp_db_fn}
    write_conf_file(confname, conf_dict)

    #(re)create the change file.
    if not os.path.isfile(conf_dir + change_fn):
        with open(conf_dir + change_fn, mode='w') as f:
            f.write("0")
    
    #(re)create the id mapping tables.
    with closing(sqlite3.connect(conf_dir + id_db_fn)) as conn:
        for table in item_to_table.values():
            conn.executescript("""
                DROP TABLE IF EXISTS {tablename};

                CREATE TABLE {tablename}(
                    localId INTEGER PRIMARY KEY,
                    gmId TEXT NOT NULL);
                """.format(tablename=table))
                

    #(re)attach to the db.
    action_pairs, make_connection = mp_confs[mp_type]

    with closing(make_connection(mp_db_fn)) as conn:
        reattach(conn, action_pairs)
    

    

class MockApi(Api):
    def _wc_call(self, service_name, *args, **kw):
        """Returns the response of a web client call.
        :param service_name: the name of the call, eg ``search``
        additional positional arguments are passed to ``build_body``for the retrieved protocol.
        if a 'query_args' key is present in kw, it is assumed to be a dictionary of additional key/val pairs to append to the query string.
        """

        #just log the request
        self.log.warning("wc_call %s %s", service_name, args)




class MockApi(Api):

    def is_authenticated(self):
        return True

    def _wc_call(self, service_name, *args, **kw):
        """Returns the response of a web client call.
        :param service_name: the name of the call, eg ``search``
        additional positional arguments are passed to ``build_body``for the retrieved protocol.
        if a 'query_args' key is present in kw, it is assumed to be a dictionary of additional key/val pairs to append to the query string.
        """

        #just log the request
        self.log.debug("wc_call %s %s", service_name, args)
        return {'id': 'test'} #super hack

class ChangePollThread(threading.Thread):
    """This thread does the work of polling for changes and pushing them out."""
    
    def __init__(self, make_conn, api, mp_db_fn, conf_dir, handlers):
        """makeconn - one param func to connect to a db, given a fn
        api - an already authenticated api
        mp_db_fn - filename of the mediaplayer db
        conf_dir - the config dir, with a trailing separator
        handlers - a list of Handlers, ordered by change type
        """
        
        #Most of this should eventually be pulled into protocol.
        threading.Thread.__init__(self)
        self._running = threading.Event()
        self._db = mp_db_fn
        self.make_conn = partial(make_conn, self._db)
        self._config_dir = conf_dir
        self._change_file = self._config_dir + change_fn 


        id_db_loc = self._config_dir + id_db_fn
        self.make_gmid_conn = partial(sqlite3.connect, id_db_loc)

        self.handlers = handlers
        self.activate() #we won't run until start()ed

        #cheat for debugging
        self.api = api
        
    def _get_gm_id(self, localId, item_type, cur):
        """Return the GM id for this *localId* and *item_type*, using sqlite cursor *cur*."""

        cur.execute("SELECT gmId FROM %s WHERE localId=?" % item_to_table[item_type], (localId,))
        gm_id = cur.fetchone()

        if not gm_id: raise UnmappedId

        return gm_id[0]                        


    def activate(self):
        self._running.set()

    def stop(self):
        self._running.clear()

    @property
    def active(self):
        return self._running.isSet()

    def update_id_mapping(self, local_id, handler_res):
        """Update the local to remote id mapping database with a HandlerResult (*handler_res*)."""
        action, item_type, gm_id = handler_res

        #two switches for the different events; they're too dissimilar to factor out
        if action == 'create':
            command = "REPLACE INTO {table} (localId, gmId) VALUES (?, ?)"
            values = (local_id, gm_id)
        elif action == 'delete':
            command = "DELETE FROM {table} WHERE localId=?"
            values = (local_id,)
        else:
            raise Exception("Unknown HandlerResult.action")

        command = command.format(table=item_to_table[item_type])


        #capture/log failure?
        with closing(self.make_gmid_conn()) as conn:
            conn.execute(command, values)
        

    def run(self):

        read_new_changeid = True #assumes a changeid exists. currently fulfilled in __init__

        while self.active:

            if read_new_changeid:
               with open(self._change_file) as f:
                   last_change_id = int(f.readline().strip())
                   
            print "polling. last change:", last_change_id

            #Buffer in changes to memory.
            #The limit is intended to limit risk of losing changes.
            max_changes = 10

            #opening a new conn every time - not sure if this is desirable
            with closing(self.make_conn()) as conn, closing(conn.cursor()) as cur:
            
                #continue to retry while db is locked
                while 1:
                    try:
                        cur.execute("SELECT changeId, changeType, localId FROM sync2gm_Changes WHERE changeId > ?", (last_change_id,))
                        break
                    except sqlite3.Error as e:
                        if "database is locked" in e.message:
                            print "locked - retrying"
                        else: raise
                    
                changes = cur.fetchmany(max_changes)

                if len(changes) is 0:
                    read_new_changeid = False
                else:
                    read_new_changeid = True

                    for change in changes:
                        c_id, c_type, local_id = change
                        print c_id, c_type, local_id
                        
                        try:
                            handler = self.handlers[c_type](local_id, self.api, conn, make_gmid_conn(), self._get_gm_id)
                            res = handler.push_changes()

                            #When the handler created a remote object, update our local mappings.
                            if res is not None: self.update_id_mapping(local_id, res)

                        except CallFailure as cf:
                            print "call failure!" #log failure to update; this is a big deal
                        except Exception as e:
                            #for debugging
                            print "exception while pushing change"
                            print e.message
                            print traceback.format_exc()
                        finally: #mark this change as handled, correctly or not
                            if not atomic_write(self._change_file, c_id): 
                                print "failed to write out change!"

                            

                        
        
            
            time.sleep(5) 




class ServiceHandler(SocketServer.StreamRequestHandler):
    """Respond if we are running, and handle shutdown requests.

    valid requests are: 'shutdown' and 'status'. 

    'status' receieves a response 'running'."""

    def handle(self):
        self.data = self.rfile.readline().strip()

        if self.data == 'shutdown':
            for t in threading.enumerate():
                if isinstance(t, ChangePollThread):
                    t.stop()
                    t.join()

            self.server.shutdown()

        elif self.data == 'status':
            self.wfile.write('running')

def send_service(port, s, receive=False):
    """Send a string *s* to the service running on port *port*.
    
    When *receieve* is True, return the service's response."""

  # Create a socket (SOCK_STREAM means a TCP socket)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        # Connect to server and send data
        sock.connect(('localhost', port))
        sock.sendall(s + "\n")

        if recieve:
            # Receive data from the server and shut down
            received = sock.recv(1024)
    finally:
            sock.close()

    if receieve: return received
   

def is_service_running(port):
    try:
        if send_service(port, 'status', receive=True): return True
        else: return False
    except:
        return False

def stop_service(port):
    """Send a signal to stop the service on port *port*."""
    if is_service_running(port): send_service(port, 'shutdown')

def start_service(confname, port, gm_email, gm_password):
    """Attempt to start the service on locally on port *port*, using config *confname*.

    Return True if the service started, or an error message."""

    #Read in the config.
    conf = read_config_file(confname)
    mp_conf = mp_confs[conf['mp_type']]
    api = Api()
    api.login(gm_email, gm_password) #need to use init here
    

    try:
        server = ThreadedTCPServer(('localhost', port), ServiceHandler)
        server_thread = threading.Thread(target=server.serve_forever)
        poll_thread = ChangePollThread(mp_conf.make_conn, api, conf['mp_db_fn'], get_conf_dir(confname), mp_conf.handlers)
        server_thread.start()
        poll_thread.start()
    except Exception as e:
        return "Could not start service:", repr(e)

    return True
