import datetime

from peewee import *
from playhouse.sqlite_changelog import ChangeLog
from playhouse.sqlite_changelog import COMPOSITE_KEY

from .base import ModelTestCase
from .base import TestModel
from .base import requires_models
from .base import skip_unless


database = SqliteDatabase(':memory:', pragmas={'foreign_keys': 1})


class Person(TestModel):
    name = TextField()
    dob = DateField()


class Note(TestModel):
    person = ForeignKeyField(Person, on_delete='CASCADE')
    content = TextField()
    timestamp = TimestampField()
    status = IntegerField(default=0)


class CT1(TestModel):
    f1 = TextField()
    f2 = IntegerField(null=True)
    f3 = FloatField()
    fi = IntegerField()


class CT2(TestModel):
    data = JSONField()  # Diff of json?


class Bin(TestModel):
    zone = TextField()
    slot = TextField()
    qty = IntegerField(default=0)

    class Meta:
        primary_key = CompositeKey('zone', 'slot')


class Legacy(TestModel):
    name = TextField()
    value = IntegerField(default=0)


changelog = ChangeLog(database)
CL = changelog.model


class TestChangeLog(ModelTestCase):
    database = database
    requires = [Person, Note]

    def setUp(self):
        super(TestChangeLog, self).setUp()
        changelog.install(Person)
        changelog.install(Note, skip_fields=['timestamp'])
        self.last_index = 0

    def assertChanges(self, changes, last_index=None):
        last_index = last_index or self.last_index
        query = (CL
                 .select(CL.action, CL.table, CL.changes)
                 .order_by(CL.id)
                 .offset(last_index))
        accum = list(query.tuples())
        self.last_index += len(accum)
        self.assertEqual(accum, changes)

    def test_changelog(self):
        huey = Person.create(name='huey', dob=datetime.date(2010, 5, 1))
        zaizee = Person.create(name='zaizee', dob=datetime.date(2013, 1, 1))
        self.assertChanges([
            ('INSERT', 'person', {'name': [None, 'huey'],
                                  'dob': [None, '2010-05-01']}),
            ('INSERT', 'person', {'name': [None, 'zaizee'],
                                  'dob': [None, '2013-01-01']})])

        zaizee.dob = datetime.date(2013, 2, 2)
        zaizee.save()
        self.assertChanges([
            ('UPDATE', 'person', {'dob': ['2013-01-01', '2013-02-02']})])

        zaizee.name = 'zaizee-x'
        zaizee.dob = datetime.date(2013, 3, 3)
        zaizee.save()

        huey.save()  # No changes.

        self.assertChanges([
            ('UPDATE', 'person', {'name': ['zaizee', 'zaizee-x'],
                                  'dob': ['2013-02-02', '2013-03-03']}),
            ('UPDATE', 'person', {})])

        zaizee.delete_instance()
        self.assertChanges([
            ('DELETE', 'person', {'name': ['zaizee-x', None],
                                  'dob': ['2013-03-03', None]})])

        nh1 = Note.create(person=huey, content='huey1', status=1)
        nh2 = Note.create(person=huey, content='huey2', status=2)
        self.assertChanges([
            ('INSERT', 'note', {'person_id': [None, huey.id],
                                'content': [None, 'huey1'],
                                'status': [None, 1]}),
            ('INSERT', 'note', {'person_id': [None, huey.id],
                                'content': [None, 'huey2'],
                                'status': [None, 2]})])

        nh1.content = 'huey1-x'
        nh1.status = 0
        nh1.save()

        mickey = Person.create(name='mickey', dob=datetime.date(2009, 8, 1))
        nh2.person = mickey
        nh2.save()

        self.assertChanges([
            ('UPDATE', 'note', {'content': ['huey1', 'huey1-x'],
                                'status': [1, 0]}),
            ('INSERT', 'person', {'name': [None, 'mickey'],
                                  'dob': [None, '2009-08-01']}),
            ('UPDATE', 'note', {'person_id': [huey.id, mickey.id]})])

        mickey.delete_instance()
        self.assertChanges([
            ('DELETE', 'note', {'person_id': [mickey.id, None],
                                'content': ['huey2', None],
                                'status': [2, None]}),
            ('DELETE', 'person', {'name': ['mickey', None],
                                  'dob': ['2009-08-01', None]})])

    @requires_models(CT1)
    def test_changelog_details(self):
        changelog.install(CT1, skip_fields=['fi'], insert=False, delete=False)

        c1 = CT1.create(f1='v1', f2=1, f3=1.5, fi=0)
        self.assertChanges([])

        CT1.update(f1='v1-x', f2=2, f3=2.5, fi=1).execute()
        self.assertChanges([
            ('UPDATE', 'ct1', {
                'f1': ['v1', 'v1-x'],
                'f2': [1, 2],
                'f3': [1.5, 2.5]})])

        c1.f2 = None
        c1.save()  # Overwrites previously-changed fields.
        self.assertChanges([('UPDATE', 'ct1', {
            'f1': ['v1-x', 'v1'],
            'f2': [2, None],
            'f3': [2.5, 1.5]})])

        c1.delete_instance()
        self.assertChanges([])

    @requires_models(CT2)
    def test_changelog_jsonfield(self):
        changelog.install(CT2)

        ca = CT2.create(data={'k1': 'v1'})
        cb = CT2.create(data=['i0', 'i1', 'i2'])
        cc = CT2.create(data='hello')

        self.assertChanges([
            ('INSERT', 'ct2', {'data': [None, {'k1': 'v1'}]}),
            ('INSERT', 'ct2', {'data': [None, ['i0', 'i1', 'i2']]}),
            ('INSERT', 'ct2', {'data': [None, 'hello']})])

        ca.data['k1'] = 'v1-x'
        cb.data.append('i3')
        cc.data = 'world'

        ca.save()
        cb.save()
        cc.save()

        self.assertChanges([
            ('UPDATE', 'ct2', {'data': [{'k1': 'v1'}, {'k1': 'v1-x'}]}),
            ('UPDATE', 'ct2', {'data': [['i0', 'i1', 'i2'],
                                        ['i0', 'i1', 'i2', 'i3']]}),
            ('UPDATE', 'ct2', {'data': ['hello', 'world']})])

        cc.data = 13.37
        cc.save()
        self.assertChanges([('UPDATE', 'ct2', {'data': ['world', 13.37]})])

        ca.delete_instance()
        self.assertChanges([
            ('DELETE', 'ct2', {'data': [{'k1': 'v1-x'}, None]})])


class TestChangeLogCompositeKey(ModelTestCase):
    database = database
    requires = [Bin, Legacy]

    def setUp(self):
        super(TestChangeLogCompositeKey, self).setUp()
        changelog.install(Bin)
        changelog.install(Legacy)

    def raw(self):
        return list(changelog.model.select().order_by(changelog.model.id))

    def test_primary_key_columns(self):
        rows = self.raw()
        self.assertEqual(rows, [])

        b = Bin.create(zone='A', slot='B', qty=1)
        row, = self.raw()
        self.assertEqual(row.primary_key, None)
        self.assertEqual(row.primary_key_type, COMPOSITE_KEY)
        self.assertEqual(row.primary_key_old, None)
        self.assertEqual(row.primary_key_new, ['A', 'B'])
        # Key columns are carried by the ordered key arrays, not duplicated
        # as ordinary field changes.
        self.assertEqual(row.changes, {'qty': [None, 1]})

    def test_key_only_update(self):
        b = Bin.create(zone='A', slot='B', qty=1)
        # Change one key column only, leaving the ordinary field untouched.
        (Bin.update(zone='Z')
         .where((Bin.zone == 'A') & (Bin.slot == 'B'))
         .execute())

        insert, update = self.raw()
        self.assertEqual(update.action, 'UPDATE')
        # No ordinary field changed, but the ordered key arrays still describe
        # the move.
        self.assertEqual(update.changes, {})
        self.assertEqual(update.primary_key_old, ['A', 'B'])
        self.assertEqual(update.primary_key_new, ['Z', 'B'])

    def test_same_name_after_concatenation(self):
        # ('A', 'BC') and ('AB', 'C') collide as 'ABC' when joined naively.
        a = Bin.create(zone='A', slot='BC', qty=1)
        b = Bin.create(zone='AB', slot='C', qty=2)
        self.assertEqual(Bin.select().count(), 2)

        events = list(changelog.events())
        self.assertEqual([e.new_key for e in events],
                         [('A', 'BC'), ('AB', 'C')])

        (Bin.update(zone='Z').where(
            (Bin.zone == 'A') & (Bin.slot == 'BC')).execute())
        dense = Bin.get((Bin.zone == 'Z') & (Bin.slot == 'BC'))
        dense.delete_instance()
        b.delete_instance()

        events = list(changelog.events())
        self.assertEqual(events[2].old_key, ('A', 'BC'))
        self.assertEqual(events[2].new_key, ('Z', 'BC'))
        self.assertEqual(events[3].old_key, ('Z', 'BC'))
        self.assertEqual(events[4].old_key, ('AB', 'C'))
        self.assertEqual(Bin.select().count(), 0)

    def test_replay_and_rollback(self):
        # Two inserts, a key-only update and a delete, in one transaction.
        with self.database.atomic():
            a = Bin.create(zone='A', slot='B', qty=1)
            b = Bin.create(zone='AB', slot='', qty=2)
            (Bin.update(zone='Z')
             .where((Bin.zone == 'A') & (Bin.slot == 'B'))
             .execute())
            b.delete_instance()

        # Replay the log in order onto an empty view of the table.
        state = {}
        for event in changelog.events():
            if event.action == 'INSERT':
                state[event.new_key] = dict(event.changes)
            elif event.action == 'UPDATE':
                state.pop(event.old_key)
                state[event.new_key] = dict(event.changes)
            else:
                state.pop(event.old_key)
        self.assertEqual(sorted(state), [('Z', 'B')])

        # A rolled-back key change leaves no events behind.
        try:
            with self.database.atomic():
                (Bin.update(zone='Q')
                 .where((Bin.zone == 'Z') & (Bin.slot == 'B'))
                 .execute())
                raise ValueError('rollback')
        except ValueError:
            pass

        self.assertEqual(Bin.get().zone, 'Z')
        self.assertEqual(len(list(changelog.events())), 4)

    def test_types_preserved(self):
        class Warehouse(TestModel):
            zone = CharField()
            slot = CharField()
            value = IntegerField(default=0)

            class Meta:
                database = database
                primary_key = CompositeKey('zone', 'slot')

        self.database.create_tables([Warehouse])
        self.addCleanup(self.database.drop_tables, [Warehouse])
        self.addCleanup(self.database.execute_sql,
                        changelog.drop_trigger_sql(Warehouse, 'INSERT'))
        self.addCleanup(self.database.execute_sql,
                        changelog.drop_trigger_sql(Warehouse, 'UPDATE'))
        self.addCleanup(self.database.execute_sql,
                        changelog.drop_trigger_sql(Warehouse, 'DELETE'))

        changelog.install(Warehouse)
        Warehouse.create(zone='1', slot='0', value=1)
        event, = list(changelog.events())
        self.assertEqual(event.new_key, ('1', '0'))
        self.assertIsInstance(event.new_key[0], str)
        self.assertIsInstance(event.new_key[1], str)

    def test_mixed_with_legacy_integer_key(self):
        p = Legacy.create(name='p1', value=1)
        p.value = 2
        p.save()
        p.delete_instance()
        b = Bin.create(zone='A', slot='B', qty=1)

        events = list(changelog.events())
        self.assertEqual(events[0].table, 'legacy')
        self.assertEqual(events[0].new_key, p.id)
        self.assertIsInstance(events[0].new_key, int)
        self.assertEqual(events[1].old_key, p.id)
        self.assertEqual(events[1].new_key, p.id)
        self.assertEqual(events[2].old_key, p.id)
        self.assertEqual(events[3].table, 'bin')
        self.assertEqual(events[3].new_key, ('A', 'B'))

        # The legacy primary_key column keeps its original scalar form for
        # legacy rows, and is untouched (NULL) for composite rows.
        rows = self.raw()
        self.assertEqual(rows[0].primary_key, p.id)
        self.assertEqual(rows[0].primary_key_type, None)
        self.assertEqual(rows[0].primary_key_old, None)
        self.assertEqual(rows[0].primary_key_new, None)
        self.assertEqual(rows[3].primary_key, None)
        self.assertEqual(rows[3].primary_key_type, COMPOSITE_KEY)

    def test_upgrade_legacy_changelog_table(self):
        # Simulate the previous schema: NOT NULL integer primary_key and no
        # composite columns.
        db = database
        db.execute_sql('DROP TABLE changelog')
        db.execute_sql(
            'CREATE TABLE changelog ('
            'id INTEGER NOT NULL PRIMARY KEY, '
            'timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, '
            'action TEXT NOT NULL, "table" TEXT NOT NULL, '
            'primary_key INTEGER NOT NULL, changes TEXT NOT NULL)')
        db.execute_sql(
            'INSERT INTO changelog (action, "table", primary_key, changes) '
            "VALUES ('INSERT', 'legacy', 100, '{\"value\": [null, 1]}')")
        try:
            changelog.install(Bin)

            columns = dict((c.name, c)
                           for c in db.get_columns('changelog'))
            self.assertTrue(columns['primary_key'].null)
            self.assertTrue(columns['primary_key_type'].null)
            self.assertTrue(columns['primary_key_old'].null)
            self.assertTrue(columns['primary_key_new'].null)

            Bin.create(zone='A', slot='B', qty=1)
            events = list(changelog.events())
            # History written before the upgrade is still readable.
            self.assertEqual(events[0].table, 'legacy')
            self.assertEqual(events[0].key, 100)
            self.assertEqual(events[1].new_key, ('A', 'B'))
        finally:
            db.execute_sql('DROP TABLE changelog')
            changelog._changelog_model.create_table()

    def test_batch_consumer(self):
        # One consumer folds legacy integer keys and composite keys into the
        # same batch -- no need to move the old history to another database.
        p = Legacy.create(name='p1', value=1)
        p.value = 2
        p.save()

        with self.database.atomic():
            a = Bin.create(zone='A', slot='B', qty=1)
            b = Bin.create(zone='AB', slot='', qty=2)
            (Bin.update(zone='Z')
             .where((Bin.zone == 'A') & (Bin.slot == 'B'))
             .execute())
            b.delete_instance()

        state = {}
        for event in changelog.events():
            key = event.new_key if event.action != 'DELETE' else event.old_key
            # Values are read wholly from the event, never from the live table;
            # in particular the deleted key is not reconstructed.
            if event.action == 'INSERT':
                state[key] = {'table': event.table, 'changes': event.changes}
            elif event.action == 'UPDATE':
                state.pop(event.old_key)
                state[key] = {'table': event.table, 'changes': event.changes}
            else:
                del state[key]

        self.assertEqual(list(state), [p.id, ('Z', 'B')])
        self.assertEqual(state[p.id]['table'], 'legacy')
        self.assertEqual(state[('Z', 'B')]['table'], 'bin')
        self.assertEqual(state[('Z', 'B')]['changes'], {})

    def test_key_columns_not_skippable(self):
        # Passing a key column in skip_fields must not drop it from the key
        # arrays; the old / new keys remain complete.
        changelog.install(Bin, skip_fields=['zone'])
        Bin.create(zone='A', slot='B', qty=1)
        event, = list(changelog.events())
        self.assertEqual(event.new_key, ('A', 'B'))

    def test_source_columns_with_reserved_names(self):
        class Weird(TestModel):
            code = TextField()
            primary_key_type = TextField()
            primary_key_new = TextField()

            class Meta:
                database = database
                primary_key = CompositeKey('code', 'primary_key_type')

        self.database.create_tables([Weird])
        self.addCleanup(self.database.drop_tables, [Weird])
        for action in ('INSERT', 'UPDATE', 'DELETE'):
            self.addCleanup(self.database.execute_sql,
                            changelog.drop_trigger_sql(Weird, action))

        changelog.install(Weird)
        Weird.create(code='c1', primary_key_type='t1', primary_key_new='n1')
        (Weird.update(primary_key_type='t2')
         .where(Weird.code == 'c1')
         .execute())

        insert, update = list(changelog.events())
        self.assertEqual(insert.new_key, ('c1', 't1'))
        self.assertEqual(insert.changes, {'primary_key_new': [None, 'n1']})
        self.assertEqual(update.old_key, ('c1', 't1'))
        self.assertEqual(update.new_key, ('c1', 't2'))
        self.assertEqual(update.changes, {})

