"""
SONiC ConfigDB connection module

Example:
    # Write to config DB
    config_db = ConfigDBConnector()
    config_db.connect()
    config_db.mod_entry('BGP_NEIGHBOR', '10.0.0.1', {
        'admin_status': state
        })

    # Daemon to watch config change in certain table:
    config_db = ConfigDBConnector()
    handler = lambda table, key, data: print (key, data)
    config_db.subscribe('BGP_NEIGHBOR', handler)
    config_db.connect()
    config_db.listen()

"""
import sys
import time
from .dbconnector import SonicV2Connector

PY3K = sys.version_info >= (3, 0)

class ConfigDBConnector(SonicV2Connector):

    INIT_INDICATOR = 'CONFIG_DB_INITIALIZED'
    TABLE_NAME_SEPARATOR = '|'
    KEY_SEPARATOR = '|'

    def __init__(self, decode_responses=True, **kwargs):
        # By default, connect to Redis through TCP, which does not requires root.
        if len(kwargs) == 0:
            kwargs['host'] = '127.0.0.1'

        if PY3K:
            if not decode_responses:
                raise NotImplementedError('ConfigDBConnector with decode_responses=False is not supported in python3')
            kwargs['decode_responses'] = True

        """The ConfigDBConnector class will accept the parameter 'namespace' which is used to
           load the database_config and connect to the redis DB instances in that namespace.
           By default namespace is set to None, which means it connects to local redis DB instances.

           When connecting to a different namespace set the use_unix_socket_path flag to true.
           Eg. ConfigDBConnector(use_unix_socket_path=True, namespace=namespace)

           'namespace' is implicitly passed to the parent SonicV2Connector class.
        """
        super(ConfigDBConnector, self).__init__(**kwargs)
        self.handlers = {}

    def __wait_for_db_init(self):
        client = self.get_redis_client(self.db_name)
        pubsub = client.pubsub()
        initialized = client.get(self.INIT_INDICATOR)
        if not initialized:
            pattern = "__keyspace@{}__:{}".format(self.get_dbid(self.db_name), self.INIT_INDICATOR)
            pubsub.psubscribe(pattern)
            for item in pubsub.listen():
                if item['type'] == 'pmessage':
                    key = item['channel'].split(':', 1)[1]
                    if key == self.INIT_INDICATOR:
                        initialized = client.get(self.INIT_INDICATOR)
                        if initialized:
                            break
            pubsub.punsubscribe(pattern)


    def db_connect(self, dbname, wait_for_init=False, retry_on=False):
        self.db_name = dbname
        self.KEY_SEPARATOR = self.TABLE_NAME_SEPARATOR = self.get_db_separator(self.db_name)
        SonicV2Connector.connect(self, self.db_name, retry_on)
        if wait_for_init:
            self.__wait_for_db_init()

    def connect(self, wait_for_init=True, retry_on=False):
        self.db_connect('CONFIG_DB', wait_for_init, retry_on)

    def subscribe(self, table, handler):
        """Set a handler to handle config change in certain table.
        Note that a single handler can be registered to different tables by 
        calling this fuction multiple times.
        Args:
            table: Table name.
            handler: a handler function that has signature of handler(table_name, key, data)
        """
        self.handlers[table] = handler

    def unsubscribe(self, table):
        """Remove registered handler from a certain table.
        Args:
            table: Table name.
        """
        if table in self.handlers:
            self.handlers.pop(table)

    def __fire(self, table, key, data):
        if table in self.handlers:
            handler = self.handlers[table]
            handler(table, key, data)

    def listen(self):
        """Start listen Redis keyspace events and will trigger corresponding handlers when content of a table changes.
        """
        self.pubsub = self.get_redis_client(self.db_name).pubsub()
        self.pubsub.psubscribe("__keyspace@{}__:*".format(self.get_dbid(self.db_name)))
        for item in self.pubsub.listen():
            if item['type'] == 'pmessage':
                key = item['channel'].split(':', 1)[1]
                try:
                    (table, row) = key.split(self.TABLE_NAME_SEPARATOR, 1)
                    if table in self.handlers:
                        client = self.get_redis_client(self.db_name)
                        data = self.raw_to_typed(client.hgetall(key))
                        self.__fire(table, row, data)
                except ValueError:
                    pass    #Ignore non table-formated redis entries

    def raw_to_typed(self, raw_data):
        if raw_data is None:
            return None
        typed_data = {}
        for raw_key in raw_data:
            key = raw_key

            # "NULL:NULL" is used as a placeholder for objects with no attributes
            if key == "NULL":
                pass
            # A column key with ending '@' is used to mark list-typed table items
            # TODO: Replace this with a schema-based typing mechanism.
            elif key.endswith("@"):
                value = raw_data[raw_key].split(',')
                typed_data[key[:-1]] = value
            else:
                typed_data[key] = raw_data[raw_key]
        return typed_data

    def typed_to_raw(self, typed_data):
        if typed_data is None:
            return None
        elif typed_data == {}:
            return { "NULL": "NULL" }
        raw_data = {}
        for key in typed_data:
            value = typed_data[key]
            if type(value) is list:
                raw_data[key+'@'] = ','.join(value)
            else:
                raw_data[key] = str(value)
        return raw_data

    @staticmethod
    def serialize_key(key):
        if type(key) is tuple:
            return ConfigDBConnector.KEY_SEPARATOR.join(key)
        else:
            return str(key)

    @staticmethod
    def deserialize_key(key):
        tokens = key.split(ConfigDBConnector.KEY_SEPARATOR)
        if len(tokens) > 1:
            return tuple(tokens)
        else:
            return key

    def set_entry(self, table, key, data):
        """Write a table entry to config db.
           Remove extra fields in the db which are not in the data.
        Args:
            table: Table name.
            key: Key of table entry, or a tuple of keys if it is a multi-key table.
            data: Table row data in a form of dictionary {'column_key': 'value', ...}.
                  Pass {} as data will create an entry with no column if not already existed.
                  Pass None as data will delete the entry.
        """
        key = self.serialize_key(key)
        client = self.get_redis_client(self.db_name)
        _hash = '{}{}{}'.format(table.upper(), self.TABLE_NAME_SEPARATOR, key)
        if data is None:
            client.delete(_hash)
        else:
            original = self.get_entry(table, key)
            client.hmset(_hash, self.typed_to_raw(data))
            for k in [ k for k in original if k not in data ]:
                if type(original[k]) == list:
                    k = k + '@'
                client.hdel(_hash, self.serialize_key(k))

    def mod_entry(self, table, key, data):
        """Modify a table entry to config db.
        Args:
            table: Table name.
            key: Key of table entry, or a tuple of keys if it is a multi-key table.
            data: Table row data in a form of dictionary {'column_key': 'value', ...}.
                  Pass {} as data will create an entry with no column if not already existed.
                  Pass None as data will delete the entry.
        """
        key = self.serialize_key(key)
        client = self.get_redis_client(self.db_name)
        _hash = '{}{}{}'.format(table.upper(), self.TABLE_NAME_SEPARATOR, key)
        if data is None:
            client.delete(_hash)
        else:
            client.hmset(_hash, self.typed_to_raw(data))

    def get_entry(self, table, key):
        """Read a table entry from config db.
        Args:
            table: Table name.
            key: Key of table entry, or a tuple of keys if it is a multi-key table.
        Returns: 
            Table row data in a form of dictionary {'column_key': 'value', ...}
            Empty dictionary if table does not exist or entry does not exist.
        """
        key = self.serialize_key(key)
        client = self.get_redis_client(self.db_name)
        _hash = '{}{}{}'.format(table.upper(), self.TABLE_NAME_SEPARATOR, key)
        return self.raw_to_typed(client.hgetall(_hash))

    def get_keys(self, table, split=True):
        """Read all keys of a table from config db.
        Args:
            table: Table name.
            split: split the first part and return second.
                   Useful for keys with two parts <tablename>:<key>
        Returns: 
            List of keys.
        """
        client = self.get_redis_client(self.db_name)
        pattern = '{}{}*'.format(table.upper(), self.TABLE_NAME_SEPARATOR)
        keys = client.keys(pattern)
        data = []
        for key in keys:
            try:
                if split:
                    (_, row) = key.split(self.TABLE_NAME_SEPARATOR, 1)
                    data.append(self.deserialize_key(row))
                else:
                    data.append(self.deserialize_key(key))
            except ValueError:
                pass    #Ignore non table-formated redis entries
        return data

    def get_table(self, table):
        """Read an entire table from config db.
        Args:
            table: Table name.
        Returns: 
            Table data in a dictionary form of 
            { 'row_key': {'column_key': value, ...}, ...}
            or { ('l1_key', 'l2_key', ...): {'column_key': value, ...}, ...} for a multi-key table.
            Empty dictionary if table does not exist.
        """
        client = self.get_redis_client(self.db_name)
        pattern = '{}{}*'.format(table.upper(), self.TABLE_NAME_SEPARATOR)
        keys = client.keys(pattern)
        data = {}
        for key in keys:
            try:
                entry = self.raw_to_typed(client.hgetall(key))
                if entry is not None:
                    (_, row) = key.split(self.TABLE_NAME_SEPARATOR, 1)
                    data[self.deserialize_key(row)] = entry
            except ValueError:
                pass    #Ignore non table-formated redis entries
        return data

    def delete_table(self, table):
        """Delete an entire table from config db.
        Args:
            table: Table name.
        """
        client = self.get_redis_client(self.db_name)
        pattern = '{}{}*'.format(table.upper(), self.TABLE_NAME_SEPARATOR)
        keys = client.keys(pattern)
        data = {}
        for key in keys:
            client.delete(key)

    def mod_config(self, data):
        """Write multiple tables into config db.
           Extra entries/fields in the db which are not in the data are kept.
        Args:
            data: config data in a dictionary form
            { 
                'TABLE_NAME': { 'row_key': {'column_key': 'value', ...}, ...},
                'MULTI_KEY_TABLE_NAME': { ('l1_key', 'l2_key', ...) : {'column_key': 'value', ...}, ...},
                ...
            }
        """
        for table_name in data:
            table_data = data[table_name]
            if table_data == None:
                self.delete_table(table_name)
                continue
            for key in table_data:
                self.mod_entry(table_name, key, table_data[key])

    def get_config(self):
        """Read all config data. 
        Returns:
            Config data in a dictionary form of 
            { 
                'TABLE_NAME': { 'row_key': {'column_key': 'value', ...}, ...},
                'MULTI_KEY_TABLE_NAME': { ('l1_key', 'l2_key', ...) : {'column_key': 'value', ...}, ...},
                ...
            }
        """
        client = self.get_redis_client(self.db_name)
        keys = client.keys('*')
        data = {}
        for key in keys:
            try:
                (table_name, row) = key.split(self.TABLE_NAME_SEPARATOR, 1)
                entry = self.raw_to_typed(client.hgetall(key))
                if entry != None:
                    data.setdefault(table_name, {})[self.deserialize_key(row)] = entry
            except ValueError:
                pass    #Ignore non table-formated redis entries
        return data


class ConfigDBPipeConnector(ConfigDBConnector):
    REDIS_SCAN_BATCH_SIZE = 30

    def __init__(self, **kwargs):
        super(ConfigDBPipeConnector, self).__init__(**kwargs)

    def __delete_entries(self, client, pipe, pattern, cursor):
        """Helper method to delete table entries from config db using Redis pipeline
        with batch size of REDIS_SCAN_BATCH_SIZE.
        The caller should call pipeline execute once ready 
        Args:
            client: Redis client
            pipe: Redis DB pipe
            pattern: key pattern
            cursor: position to start scanning from

        Returns:
            cur: poition of next item to scan
        """
        cur, keys = client.scan(cursor=cursor, match=pattern, count=self.REDIS_SCAN_BATCH_SIZE)
        for key in keys:
            pipe.delete(key)

        return cur

    def __delete_table(self, client, pipe, table):
        """Helper method to delete table entries from config db using Redis pipeline.
        The caller should call pipeline execute once ready
        Args:
            client: Redis client
            pipe: Redis DB pipe
            table: Table name.
        """
        pattern = '{}{}*'.format(table.upper(), self.TABLE_NAME_SEPARATOR)
        cur = self.__delete_entries(client, pipe, pattern, 0)
        while cur != 0:
            cur = self.__delete_entries(client, pipe, pattern, cur)

    def __mod_entry(self, pipe, table, key, data):
        """Modify a table entry to config db.
        Args:
            table: Table name.
            pipe: Redis DB pipe
            table: Table name.
            key: Key of table entry, or a tuple of keys if it is a multi-key table.
            data: Table row data in a form of dictionary {'column_key': 'value', ...}.
                  Pass {} as data will create an entry with no column if not already existed.
                  Pass None as data will delete the entry.
        """
        key = self.serialize_key(key)
        _hash = '{}{}{}'.format(table.upper(), self.TABLE_NAME_SEPARATOR, key)
        if data is None:
            pipe.delete(_hash)
        else:
            pipe.hmset(_hash, self.typed_to_raw(data))

    def mod_config(self, data):
        """Write multiple tables into config db.
           Extra entries/fields in the db which are not in the data are kept.
        Args:
            data: config data in a dictionary form
            { 
                'TABLE_NAME': { 'row_key': {'column_key': 'value', ...}, ...},
                'MULTI_KEY_TABLE_NAME': { ('l1_key', 'l2_key', ...) : {'column_key': 'value', ...}, ...},
                ...
            }
        """
        client = self.get_redis_client(self.db_name)
        pipe = client.pipeline()
        for table_name in data:
            table_data = data[table_name]
            if table_data is None:
                self.__delete_table(client, pipe, table_name)
                continue
            for key in table_data:
                self.__mod_entry(pipe, table_name, key, table_data[key])
        pipe.execute()

    def __get_config(self, client, pipe, data, cursor):
        """Read config data in batches of size REDIS_SCAN_BATCH_SIZE using Redis pipelines
        Args:
            client: Redis client
            pipe: Redis DB pipe
            data: config dictionary
            cursor: position to start scanning from

        Returns:
            cur: poition of next item to scan
        """
        cur, keys = client.scan(cursor=cursor, match='*', count=self.REDIS_SCAN_BATCH_SIZE)
        keys = [key for key in keys if key != self.INIT_INDICATOR]
        for key in keys:
            pipe.hgetall(key)
        records = pipe.execute()

        for index, key in enumerate(keys):
            (table_name, row) = key.split(self.TABLE_NAME_SEPARATOR, 1)
            entry = self.raw_to_typed(records[index])
            if entry is not None:
                data.setdefault(table_name, {})[self.deserialize_key(row)] = entry

        return cur

    def get_config(self):
        """Read all config data. 
        Returns:
            Config data in a dictionary form of 
            { 
                'TABLE_NAME': { 'row_key': {'column_key': 'value', ...}, ...},
                'MULTI_KEY_TABLE_NAME': { ('l1_key', 'l2_key', ...) : {'column_key': 'value', ...}, ...},
                ...
            }
        """
        client = self.get_redis_client(self.db_name)
        pipe = client.pipeline()
        data = {}

        cur = self.__get_config(client, pipe, data, 0)
        while cur != 0:
            cur = self.__get_config(client, pipe, data, cur)

        return data

