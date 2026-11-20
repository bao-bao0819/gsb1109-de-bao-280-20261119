import collections

from peewee import *
from playhouse.sqlite_ext import JSONField


# Marker stored in the ``primary_key_type`` column for rows written by a
# trigger on a table with a composite primary key.  Legacy rows (and rows for
# tables with a regular, single-column primary key) leave the marker NULL, so
# a consumer that only understands the original integer ``primary_key`` column
# keeps working and does not mistake the new rows for integer keys.
COMPOSITE_KEY = 'composite'


class BaseChangeLog(Model):
    timestamp = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])
    action = TextField()
    table = TextField()
    # The original integer primary-key column.  It is NULL for composite-key
    # rows, and keeps its original value for legacy rows.
    primary_key = IntegerField(null=True)
    changes = JSONField()
    # Composite-key support.  ``primary_key_type`` is NULL for legacy rows and
    # for single-column integer keys, and ``COMPOSITE_KEY`` for composite rows.
    # The old / new key values are JSON arrays whose element order matches the
    # order in which the key fields were declared on the model.  Storing them
    # as arrays (rather than joining with a separator) preserves both the
    # values and their types even when the text contains any separator.
    primary_key_type = TextField(null=True)
    primary_key_old = JSONField(null=True)
    primary_key_new = JSONField(null=True)

    @property
    def is_composite_key(self):
        return (self.primary_key_type is not None and
                str(self.primary_key_type).lower() == COMPOSITE_KEY)

    @property
    def old_key(self):
        """Ordered key locating the row before an UPDATE / DELETE."""
        if self.is_composite_key:
            if self.primary_key_old is None:
                return None
            return tuple(self.primary_key_old)
        if self.action == 'INSERT':
            return None
        return self.primary_key

    @property
    def new_key(self):
        """Ordered key locating the row after an INSERT / UPDATE."""
        if self.is_composite_key:
            if self.primary_key_new is None:
                return None
            return tuple(self.primary_key_new)
        if self.action == 'DELETE':
            return None
        return self.primary_key

    @property
    def key(self):
        """The key identifying the affected row, or None.

        Legacy rows expose their original integer key.  Composite rows expose
        an ordered tuple -- the new key for INSERT / UPDATE and the old key for
        DELETE.
        """
        if self.action == 'DELETE':
            return self.old_key
        if self.action == 'INSERT':
            return self.new_key
        return self.new_key if self.new_key is not None else self.old_key


# A driver-neutral representation of a changelog row.  Consumers can use this
# to build a single batch from legacy integer-key rows and new composite-key
# rows without knowing which table emitted the event.
ChangeLogEvent = collections.namedtuple(
    'ChangeLogEvent',
    ('action', 'table', 'key', 'old_key', 'new_key', 'changes'))

# ``old_key`` / ``new_key`` hold ordered tuples for composite rows; for legacy
# rows they hold the scalar integer key where it is meaningful (NULL action
# side omitted).
ChangeLogEvent.old_primary_key = property(lambda self: self.old_key)
ChangeLogEvent.new_primary_key = property(lambda self: self.new_key)


def decode_event(row):
    marker = row.primary_key_type
    if marker is not None and str(marker).lower() == COMPOSITE_KEY:
        old_key = (tuple(row.primary_key_old)
                   if row.primary_key_old is not None else None)
        new_key = (tuple(row.primary_key_new)
                   if row.primary_key_new is not None else None)
        if row.action == 'DELETE':
            key = old_key
        elif row.action == 'INSERT':
            key = new_key
        else:
            key = new_key if new_key is not None else old_key
    else:
        # Legacy rows keep their original, scalar integer key -- the composite
        # columns stay NULL and are never interpreted as a key.  The old / new
        # slots mirror the composite ordering so both kinds of row replay the
        # same way: the old key locates the source of an UPDATE / DELETE and
        # the new key identifies the destination of an INSERT / UPDATE.
        key = row.primary_key
        old_key = None if row.action == 'INSERT' else key
        new_key = None if row.action == 'DELETE' else key
    return ChangeLogEvent(row.action, row.table, key, old_key, new_key,
                          row.changes)

# Alias matching the class name used by the change-log row model.
decode = decode_event


class ChangeLog(object):
    # Model class that will serve as the base for the changelog. This model
    # will be subclassed and mapped to your application database.
    base_model = BaseChangeLog

    # Template for the triggers that handle updating the changelog table.
    # table: table name
    # action: insert / update / delete
    # new_old: NEW or OLD (OLD is for DELETE)
    # primary_key: expression yielding the original integer primary key
    # key_type: expression yielding the composite-key marker (or NULL)
    # key_old / key_new: expressions yielding the ordered old / new key arrays
    # column_array: output of build_column_array()
    # change_table: changelog table name
    template = """CREATE TRIGGER IF NOT EXISTS %(table)s_changes_%(action)s
    AFTER %(action)s ON %(table)s
    BEGIN
        INSERT INTO %(change_table)s
            ("action", "table", "primary_key", "changes",
             "primary_key_type", "primary_key_old", "primary_key_new")
        SELECT
            '%(action)s', '%(table)s', %(primary_key)s, "changes",
            %(key_type)s, %(key_old)s, %(key_new)s
        FROM (
            SELECT json_group_object(
                col,
                json_array(
                    case when json_valid("oldval") then json("oldval")
                        else "oldval" end,
                    case when json_valid("newval") then json("newval")
                        else "newval" end)
                ) AS "changes"
            FROM (
                SELECT json_extract(value, '$[0]') as "col",
                       json_extract(value, '$[1]') as "oldval",
                       json_extract(value, '$[2]') as "newval"
                FROM json_each(json_array(%(column_array)s))
                WHERE "oldval" IS NOT "newval"
            )
        );
    END;"""

    drop_template = 'DROP TRIGGER IF EXISTS %(table)s_changes_%(action)s'

    _actions = ('INSERT', 'UPDATE', 'DELETE')

    def __init__(self, db, table_name='changelog'):
        self.db = db
        self.table_name = table_name

    @staticmethod
    def _composite_key_fields(model):
        meta = model._meta
        if not meta.composite_key:
            return None
        # Use the declared order of the composite key, not the table order.
        return [meta.fields[field_name]
                for field_name in meta.primary_key.field_names]

    def _build_column_array(self, model, use_old, use_new, skip_fields=None,
                            key_fields=None):
        # Builds a list of SQL expressions for each field we are tracking. This
        # is used as the data source for change tracking in our trigger.
        key_field_names = set(field.name for field in (key_fields or ()))
        col_array = []
        for field in model._meta.sorted_fields:
            if field.primary_key:
                continue

            if field.name in key_field_names:
                # Composite key columns are reported separately, in declared
                # key order, as the old / new key arrays.  Excluding them here
                # means a key-only move leaves "changes" empty, so consumers
                # do not have to treat key values as ordinary field changes.
                continue

            if skip_fields is not None and field.name in skip_fields:
                continue

            column = field.column_name
            new = 'NULL' if not use_new else 'NEW."%s"' % column
            old = 'NULL' if not use_old else 'OLD."%s"' % column

            if isinstance(field, JSONField):
                # Ensure that values are cast to JSON so that the serialization
                # is preserved when calculating the old / new.
                if use_old: old = 'json(%s)' % old
                if use_new: new = 'json(%s)' % new

            col_array.append("json_array('%s', %s, %s)" % (column, old, new))

        return ', '.join(col_array)

    def _key_expression(self, key_fields, source, use):
        if not use:
            return 'NULL'
        return 'json_array(%s)' % ', '.join(
            '%s."%s"' % (source, field.column_name) for field in key_fields)

    def trigger_sql(self, model, action, skip_fields=None):
        assert action in self._actions
        use_old = action != 'INSERT'
        use_new = action != 'DELETE'
        key_fields = self._composite_key_fields(model)
        cols = self._build_column_array(model, use_old, use_new, skip_fields,
                                        key_fields)
        new_old = 'NEW' if action != 'DELETE' else 'OLD'

        if key_fields is None:
            primary_key = '%s."%s"' % (new_old,
                                       model._meta.primary_key.column_name)
            key_type = 'NULL'
            key_old = key_new = 'NULL'
        else:
            # The integer primary_key column is reserved for legacy keys, so
            # composite rows leave it NULL and are identified by the marker.
            primary_key = 'NULL'
            key_type = "'%s'" % COMPOSITE_KEY
            key_old = self._key_expression(key_fields, 'OLD', use_old)
            key_new = self._key_expression(key_fields, 'NEW', use_new)

        return self.template % {
            'table': model._meta.table_name,
            'action': action,
            'new_old': new_old,
            'primary_key': primary_key,
            'key_type': key_type,
            'key_old': key_old,
            'key_new': key_new,
            'column_array': cols,
            'change_table': self.table_name}

    def drop_trigger_sql(self, model, action):
        assert action in self._actions
        return self.drop_template % {
            'table': model._meta.table_name,
            'action': action}

    @property
    def model(self):
        if not hasattr(self, '_changelog_model'):
            class ChangeLog(self.base_model):
                # Declared explicitly so that a custom ``base_model`` defined
                # before composite-key support still maps the new columns.
                primary_key_type = TextField(null=True)
                primary_key_old = JSONField(null=True)
                primary_key_new = JSONField(null=True)

                class Meta:
                    database = self.db
                    table_name = self.table_name
            self._changelog_model = ChangeLog

        return self._changelog_model

    def _quoted_table(self):
        return '"%s"' % self.table_name.replace('"', '""')

    def _ensure_schema(self, composite=False):
        # The changelog table is shared by legacy integer-key tables and new
        # composite-key tables, and it may have been created by an older
        # version of this module.  Add the composite columns in place rather
        # than moving history to a new table.
        columns = dict((column.name.lower(), column)
                       for column in self.db.get_columns(self.table_name))
        additions = (
            ('primary_key_type', 'TEXT'),
            ('primary_key_old', 'TEXT'),
            ('primary_key_new', 'TEXT'))

        for name, data_type in additions:
            if name not in columns:
                self.db.execute_sql(
                    'ALTER TABLE %s ADD COLUMN "%s" %s' % (
                        self._quoted_table(), name, data_type))

        pk = columns.get('primary_key')
        if composite and pk is not None and not pk.null:
            # Composite-key rows have no integer key, so the legacy column
            # must accept NULL.  SQLite can drop NOT NULL in place; for older
            # versions rebuild the table while preserving every row.
            if self.db.server_version >= (3, 35, 0):
                self.db.execute_sql(
                    'ALTER TABLE %s ALTER COLUMN "primary_key" '
                    'DROP NOT NULL' % self._quoted_table())
            else:
                self._relax_primary_key_nullable()

    def _relax_primary_key_nullable(self):
        from playhouse.migrate import SqliteMigrator
        migrator = SqliteMigrator(self.db)
        migrator.migrate(
            migrator.drop_not_null(self.table_name, 'primary_key'))

    def install(self, model, skip_fields=None, drop=True, insert=True,
                update=True, delete=True, create_table=True):
        ChangeLog = self.model
        if create_table:
            ChangeLog.create_table()

        # Add the composite-key columns in-place for a changelog table created
        # by an older release (or by a previous call using the original
        # schema).  When ``create_table`` is False the caller is responsible
        # for the table existing.
        if self.db.table_exists(self.table_name):
            self._ensure_schema(self._composite_key_fields(model) is not None)

        actions = list(zip((insert, update, delete), self._actions))
        if drop:
            for _, action in actions:
                self.db.execute_sql(self.drop_trigger_sql(model, action))

        for enabled, action in actions:
            if enabled:
                sql = self.trigger_sql(model, action, skip_fields)
                self.db.execute_sql(sql)

    def events(self, query=None):
        """Yield decoded events in insertion order.

        Both legacy integer-key rows and composite-key rows are returned as
        the same :class:`ChangeLogEvent` shape, so consumers can build one
        batch over history written before and after the upgrade.
        """
        ChangeLog = self.model
        if query is None:
            query = ChangeLog.select().order_by(ChangeLog.id)
        for row in query:
            yield decode_event(row)
