# Copyright 2015 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import time
import warnings

from collections import defaultdict

sys.path[0:0] = [""]

from bson.objectid import ObjectId
from bson.py3compat import text_type
from bson.son import SON
from pymongo import CursorType, monitoring, InsertOne, UpdateOne, DeleteOne
from pymongo.command_cursor import CommandCursor
from pymongo.errors import NotMasterError, OperationFailure
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern
from test import unittest, IntegrationTest, client_context, client_knobs
from test.utils import single_client


class EventListener(monitoring.Subscriber):

    def __init__(self):
        self.results = defaultdict(list)

    def started(self, event):
        self.results['started'].append(event)

    def succeeded(self, event):
        self.results['succeeded'].append(event)

    def failed(self, event):
        self.results['failed'].append(event)


class TestCommandMonitoring(IntegrationTest):

    @classmethod
    def setUpClass(cls):
        cls.listener = EventListener()
        cls.saved_subscribers = monitoring._SUBSCRIBERS
        monitoring.subscribe(cls.listener)
        super(TestCommandMonitoring, cls).setUpClass()

    @classmethod
    def tearDownClass(cls):
        monitoring._SUBSCRIBERS = cls.saved_subscribers

    def tearDown(self):
        self.listener.results.clear()

    def test_started_simple(self):
        self.client.pymongo_test.command('ismaster')
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertTrue(
            isinstance(succeeded, monitoring.CommandSucceededEvent))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertEqual(SON([('ismaster', 1)]), started.command)
        self.assertEqual('ismaster', started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertTrue(isinstance(started.request_id, int))

    def test_succeeded_simple(self):
        self.client.pymongo_test.command('ismaster')
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertTrue(
            isinstance(succeeded, monitoring.CommandSucceededEvent))
        self.assertEqual('ismaster', succeeded.command_name)
        self.assertEqual(self.client.address, succeeded.connection_id)
        self.assertEqual(1, succeeded.reply.get('ok'))
        self.assertTrue(isinstance(succeeded.request_id, int))
        self.assertTrue(isinstance(succeeded.duration_micros, int))

    def test_failed_simple(self):
        try:
            self.client.pymongo_test.command('oops!')
        except OperationFailure:
            pass
        results = self.listener.results
        started = results['started'][0]
        failed = results['failed'][0]
        self.assertEqual(0, len(results['succeeded']))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertTrue(
            isinstance(failed, monitoring.CommandFailedEvent))
        self.assertEqual('oops!', failed.command_name)
        self.assertEqual(self.client.address, failed.connection_id)
        self.assertEqual(0, failed.failure.get('ok'))
        self.assertTrue(isinstance(failed.request_id, int))
        self.assertTrue(isinstance(failed.duration_micros, int))

    def test_find_one(self):
        self.client.pymongo_test.test.find_one()
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertTrue(
            isinstance(succeeded, monitoring.CommandSucceededEvent))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertEqual(
            SON([('find', 'test'),
                 ('filter', {}),
                 ('limit', 1),
                 ('singleBatch', True)]),
            started.command)
        self.assertEqual('find', started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertTrue(isinstance(started.request_id, int))

    def test_find_and_get_more(self):
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert_many([{} for _ in range(10)])
        self.listener.results.clear()
        cursor = self.client.pymongo_test.test.find(
            projection={'_id': False},
            batch_size=4)
        for _ in range(4):
            next(cursor)
        cursor_id = cursor.cursor_id
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertEqual(
            SON([('find', 'test'),
                 ('filter', {}),
                 ('projection', {'_id': False}),
                 ('batchSize', 4)]),
            started.command)
        self.assertEqual('find', started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertTrue(isinstance(started.request_id, int))
        self.assertTrue(
            isinstance(succeeded, monitoring.CommandSucceededEvent))
        self.assertTrue(isinstance(succeeded.duration_micros, int))
        self.assertEqual('find', succeeded.command_name)
        self.assertTrue(isinstance(succeeded.request_id, int))
        self.assertEqual(cursor.address, succeeded.connection_id)
        expected_result = {
            'cursor': {'id': cursor_id,
                       'ns': 'pymongo_test.test',
                       'firstBatch': [{} for _ in range(4)]},
            'ok': 1}
        self.assertEqual(expected_result, succeeded.reply)

        self.listener.results.clear()
        # Next batch. Exhausting the cursor could cause a getMore
        # that returns id of 0 and no results.
        next(cursor)
        try:
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertTrue(
                isinstance(started, monitoring.CommandStartedEvent))
            self.assertEqual(
                SON([('getMore', cursor_id),
                     ('collection', 'test'),
                     ('batchSize', 4)]),
                started.command)
            self.assertEqual('getMore', started.command_name)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertTrue(isinstance(started.request_id, int))
            self.assertTrue(
                isinstance(succeeded, monitoring.CommandSucceededEvent))
            self.assertTrue(isinstance(succeeded.duration_micros, int))
            self.assertEqual('getMore', succeeded.command_name)
            self.assertTrue(isinstance(succeeded.request_id, int))
            self.assertEqual(cursor.address, succeeded.connection_id)
            expected_result = {
                'cursor': {'id': cursor_id,
                           'ns': 'pymongo_test.test',
                           'nextBatch': [{} for _ in range(4)]},
                'ok': 1}
            self.assertEqual(expected_result, succeeded.reply)
        finally:
            # Exhaust the cursor to avoid kill cursors.
            tuple(cursor)

    def test_find_with_explain(self):
        cmd = SON([('explain', SON([('find', 'test'),
                                    ('filter', {})]))])
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert_one({})
        self.listener.results.clear()
        coll = self.client.pymongo_test.test
        # Test that we publish the unwrapped command.
        if self.client.is_mongos and client_context.version.at_least(2, 4, 0):
            coll = coll.with_options(
                read_preference=ReadPreference.PRIMARY_PREFERRED)
        res = coll.find().explain()
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertEqual(cmd, started.command)
        self.assertEqual('explain', started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertTrue(isinstance(started.request_id, int))
        self.assertTrue(
            isinstance(succeeded, monitoring.CommandSucceededEvent))
        self.assertTrue(isinstance(succeeded.duration_micros, int))
        self.assertEqual('explain', succeeded.command_name)
        self.assertTrue(isinstance(succeeded.request_id, int))
        self.assertEqual(self.client.address, succeeded.connection_id)
        self.assertEqual(res, succeeded.reply)

    def test_find_options(self):
        cmd = SON([('find', 'test'),
                   ('filter', {}),
                   ('comment', 'this is a test'),
                   ('sort', SON([('_id', 1)])),
                   ('projection', {'x': False}),
                   ('skip', 1),
                   ('batchSize', 2),
                   ('noCursorTimeout', True),
                   ('allowPartialResults', True)])
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert_many([{'x': i} for i in range(5)])
        self.listener.results.clear()
        coll = self.client.pymongo_test.test
        # Test that we publish the unwrapped command.
        if self.client.is_mongos and client_context.version.at_least(2, 4, 0):
            coll = coll.with_options(
                read_preference=ReadPreference.PRIMARY_PREFERRED)
        cursor = coll.find(
            filter={},
            projection={'x': False},
            skip=1,
            no_cursor_timeout=True,
            sort=[('_id', 1)],
            allow_partial_results=True,
            modifiers=SON([('$comment', 'this is a test')]),
            batch_size=2)
        next(cursor)
        try:
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertTrue(
                isinstance(started, monitoring.CommandStartedEvent))
            self.assertEqual(cmd, started.command)
            self.assertEqual('find', started.command_name)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertTrue(isinstance(started.request_id, int))
            self.assertTrue(
                isinstance(succeeded, monitoring.CommandSucceededEvent))
            self.assertTrue(isinstance(succeeded.duration_micros, int))
            self.assertEqual('find', succeeded.command_name)
            self.assertTrue(isinstance(succeeded.request_id, int))
            self.assertEqual(self.client.address, succeeded.connection_id)
        finally:
            # Exhaust the cursor to avoid kill cursors.
            tuple(cursor)

    @client_context.require_version_min(2, 6, 0)
    def test_command_and_get_more(self):
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert_many(
            [{'x': 1} for _ in range(10)])
        self.listener.results.clear()
        coll = self.client.pymongo_test.test
        # Test that we publish the unwrapped command.
        if self.client.is_mongos and client_context.version.at_least(2, 4, 0):
            coll = coll.with_options(
                read_preference=ReadPreference.PRIMARY_PREFERRED)
        cursor = coll.aggregate(
            [{'$project': {'_id': False, 'x': 1}}], batchSize=4)
        for _ in range(4):
            next(cursor)
        cursor_id = cursor.cursor_id
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertEqual(
            SON([('aggregate', 'test'),
                 ('pipeline', [{'$project': {'_id': False, 'x': 1}}]),
                 ('cursor', {'batchSize': 4})]),
            started.command)
        self.assertEqual('aggregate', started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertTrue(isinstance(started.request_id, int))
        self.assertTrue(
            isinstance(succeeded, monitoring.CommandSucceededEvent))
        self.assertTrue(isinstance(succeeded.duration_micros, int))
        self.assertEqual('aggregate', succeeded.command_name)
        self.assertTrue(isinstance(succeeded.request_id, int))
        self.assertEqual(cursor.address, succeeded.connection_id)
        expected_cursor = {'id': cursor_id,
                           'ns': 'pymongo_test.test',
                           'firstBatch': [{'x': 1} for _ in range(4)]}
        self.assertEqual(expected_cursor, succeeded.reply.get('cursor'))

        self.listener.results.clear()
        next(cursor)
        try:
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertTrue(
                isinstance(started, monitoring.CommandStartedEvent))
            self.assertEqual(
                SON([('getMore', cursor_id),
                     ('collection', 'test'),
                     ('batchSize', 4)]),
                started.command)
            self.assertEqual('getMore', started.command_name)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertTrue(isinstance(started.request_id, int))
            self.assertTrue(
                isinstance(succeeded, monitoring.CommandSucceededEvent))
            self.assertTrue(isinstance(succeeded.duration_micros, int))
            self.assertEqual('getMore', succeeded.command_name)
            self.assertTrue(isinstance(succeeded.request_id, int))
            self.assertEqual(cursor.address, succeeded.connection_id)
            expected_result = {
                'cursor': {'id': cursor_id,
                           'ns': 'pymongo_test.test',
                           'nextBatch': [{'x': 1} for _ in range(4)]},
                'ok': 1}
            self.assertEqual(expected_result, succeeded.reply)
        finally:
            # Exhaust the cursor to avoid kill cursors.
            tuple(cursor)

    def test_get_more_failure(self):
        address = self.client.address
        coll = self.client.pymongo_test.test
        cursor_doc = {"id": 12345, "firstBatch": [], "ns": coll.full_name}
        cursor = CommandCursor(coll, cursor_doc, address)
        try:
            next(cursor)
        except Exception:
            pass
        results = self.listener.results
        started = results['started'][0]
        self.assertEqual(0, len(results['succeeded']))
        failed = results['failed'][0]
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertEqual(
            SON([('getMore', 12345),
                 ('collection', 'test')]),
            started.command)
        self.assertEqual('getMore', started.command_name)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertTrue(isinstance(started.request_id, int))
        self.assertTrue(
            isinstance(failed, monitoring.CommandFailedEvent))
        self.assertTrue(isinstance(failed.duration_micros, int))
        self.assertEqual('getMore', failed.command_name)
        self.assertTrue(isinstance(failed.request_id, int))
        self.assertEqual(cursor.address, failed.connection_id)
        self.assertEqual(0, failed.failure.get("ok"))

    @client_context.require_replica_set
    def test_not_master_error(self):
        address = next(iter(self.client.secondaries))
        client = single_client(*address)
        # Clear authentication command results from the listener.
        client.admin.command('ismaster')
        self.listener.results.clear()
        error = None
        try:
            client.pymongo_test.test.find_one_and_delete({})
        except NotMasterError as exc:
            error = exc.errors
        results = self.listener.results
        started = results['started'][0]
        failed = results['failed'][0]
        self.assertEqual(0, len(results['succeeded']))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertTrue(
            isinstance(failed, monitoring.CommandFailedEvent))
        self.assertEqual('findAndModify', failed.command_name)
        self.assertEqual(address, failed.connection_id)
        self.assertEqual(0, failed.failure.get('ok'))
        self.assertTrue(isinstance(failed.request_id, int))
        self.assertTrue(isinstance(failed.duration_micros, int))
        self.assertEqual(error, failed.failure)

    @client_context.require_no_mongos
    def test_exhaust(self):
        self.client.pymongo_test.test.drop()
        self.client.pymongo_test.test.insert_many([{} for _ in range(10)])
        self.listener.results.clear()
        cursor = self.client.pymongo_test.test.find(
            projection={'_id': False},
            batch_size=5,
            cursor_type=CursorType.EXHAUST)
        next(cursor)
        cursor_id = cursor.cursor_id
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertEqual(
            SON([('find', 'test'),
                 ('filter', {}),
                 ('projection', {'_id': False}),
                 ('batchSize', 5)]),
            started.command)
        self.assertEqual('find', started.command_name)
        self.assertEqual(cursor.address, started.connection_id)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertTrue(isinstance(started.request_id, int))
        self.assertTrue(
            isinstance(succeeded, monitoring.CommandSucceededEvent))
        self.assertTrue(isinstance(succeeded.duration_micros, int))
        self.assertEqual('find', succeeded.command_name)
        self.assertTrue(isinstance(succeeded.request_id, int))
        self.assertEqual(cursor.address, succeeded.connection_id)
        expected_result = {
            'cursor': {'id': cursor_id,
                       'ns': 'pymongo_test.test',
                       'firstBatch': [{} for _ in range(5)]},
            'ok': 1}
        self.assertEqual(expected_result, succeeded.reply)

        self.listener.results.clear()
        tuple(cursor)
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertTrue(
            isinstance(started, monitoring.CommandStartedEvent))
        self.assertEqual(
            SON([('getMore', cursor_id),
                 ('collection', 'test'),
                 ('batchSize', 5)]),
            started.command)
        self.assertEqual('getMore', started.command_name)
        self.assertEqual(cursor.address, started.connection_id)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertTrue(isinstance(started.request_id, int))
        self.assertTrue(
            isinstance(succeeded, monitoring.CommandSucceededEvent))
        self.assertTrue(isinstance(succeeded.duration_micros, int))
        self.assertEqual('getMore', succeeded.command_name)
        self.assertTrue(isinstance(succeeded.request_id, int))
        self.assertEqual(cursor.address, succeeded.connection_id)
        expected_result = {
            'cursor': {'id': 0,
                       'ns': 'pymongo_test.test',
                       'nextBatch': [{} for _ in range(5)]},
            'ok': 1}
        self.assertEqual(expected_result, succeeded.reply)

    def test_kill_cursors(self):
        with client_knobs(kill_cursor_frequency=0.01):
            self.client.pymongo_test.test.drop()
            self.client.pymongo_test.test.insert_many([{} for _ in range(10)])
            cursor = self.client.pymongo_test.test.find().batch_size(5)
            next(cursor)
            cursor_id = cursor.cursor_id
            self.listener.results.clear()
            cursor.close()
            time.sleep(2)
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertTrue(
                isinstance(started, monitoring.CommandStartedEvent))
            # There could be more than one cursor_id here depending on
            # when the thread last ran.
            self.assertIn(cursor_id, started.command['cursors'])
            self.assertEqual('killCursors', started.command_name)
            self.assertEqual(cursor.address, started.connection_id)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertTrue(isinstance(started.request_id, int))
            self.assertTrue(
                isinstance(succeeded, monitoring.CommandSucceededEvent))
            self.assertTrue(isinstance(succeeded.duration_micros, int))
            self.assertEqual('killCursors', succeeded.command_name)
            self.assertTrue(isinstance(succeeded.request_id, int))
            self.assertEqual(cursor.address, succeeded.connection_id)
            # There could be more than one cursor_id here depending on
            # when the thread last ran.
            self.assertIn(cursor_id, succeeded.reply['cursorsUnknown'])

    def test_non_bulk_writes(self):
        coll = self.client.pymongo_test.test
        coll.drop()
        self.listener.results.clear()

        # Implied write concern insert_one
        res = coll.insert_one({'x': 1})
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([('insert', coll.name),
                        ('ordered', True),
                        ('documents', [{'_id': res.inserted_id, 'x': 1}])])
        self.assertEqual(expected, started.command)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertEqual('insert', started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get('ok'))
        self.assertEqual(1, reply.get('n'))

        # Unacknowledged insert_one
        self.listener.results.clear()
        coll = coll.with_options(write_concern=WriteConcern(w=0))
        res = coll.insert_one({'x': 1})
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([('insert', coll.name),
                        ('ordered', True),
                        ('documents', [{'_id': res.inserted_id, 'x': 1}]),
                        ('writeConcern', {'w': 0})])
        self.assertEqual(expected, started.command)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertEqual('insert', started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        self.assertEqual(succeeded.reply, {'ok': 1})

        # Explicit write concern insert_one
        self.listener.results.clear()
        coll = coll.with_options(write_concern=WriteConcern(w=1))
        res = coll.insert_one({'x': 1})
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([('insert', coll.name),
                        ('ordered', True),
                        ('documents', [{'_id': res.inserted_id, 'x': 1}]),
                        ('writeConcern', {'w': 1})])
        self.assertEqual(expected, started.command)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertEqual('insert', started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get('ok'))
        self.assertEqual(1, reply.get('n'))

        # delete_many
        self.listener.results.clear()
        res = coll.delete_many({'x': 1})
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([('delete', coll.name),
                        ('ordered', True),
                        ('deletes', [SON([('q', {'x': 1}),
                                          ('limit', 0)])]),
                        ('writeConcern', {'w': 1})])
        self.assertEqual(expected, started.command)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertEqual('delete', started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get('ok'))
        self.assertEqual(res.deleted_count, reply.get('n'))

        # replace_one
        self.listener.results.clear()
        oid = ObjectId()
        res = coll.replace_one({'_id': oid}, {'_id': oid, 'x': 1}, upsert=True)
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([('update', coll.name),
                        ('ordered', True),
                        ('updates', [SON([('q', {'_id': oid}),
                                          ('u', {'_id': oid, 'x': 1}),
                                          ('multi', False),
                                          ('upsert', True)])]),
                        ('writeConcern', {'w': 1})])
        self.assertEqual(expected, started.command)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertEqual('update', started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get('ok'))
        self.assertEqual(1, reply.get('n'))
        self.assertEqual([{'index': 0, '_id': oid}], reply.get('upserted'))

        # update_one
        self.listener.results.clear()
        res = coll.update_one({'x': 1}, {'$inc': {'x': 1}})
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([('update', coll.name),
                        ('ordered', True),
                        ('updates', [SON([('q', {'x': 1}),
                                          ('u', {'$inc': {'x': 1}}),
                                          ('multi', False),
                                          ('upsert', False)])]),
                        ('writeConcern', {'w': 1})])
        self.assertEqual(expected, started.command)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertEqual('update', started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get('ok'))
        self.assertEqual(1, reply.get('n'))

        # update_many
        self.listener.results.clear()
        res = coll.update_many({'x': 2}, {'$inc': {'x': 1}})
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([('update', coll.name),
                        ('ordered', True),
                        ('updates', [SON([('q', {'x': 2}),
                                          ('u', {'$inc': {'x': 1}}),
                                          ('multi', True),
                                          ('upsert', False)])]),
                        ('writeConcern', {'w': 1})])
        self.assertEqual(expected, started.command)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertEqual('update', started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get('ok'))
        self.assertEqual(1, reply.get('n'))

        # delete_one
        self.listener.results.clear()
        res = coll.delete_one({'x': 3})
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([('delete', coll.name),
                        ('ordered', True),
                        ('deletes', [SON([('q', {'x': 3}),
                                          ('limit', 1)])]),
                        ('writeConcern', {'w': 1})])
        self.assertEqual(expected, started.command)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertEqual('delete', started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get('ok'))
        self.assertEqual(1, reply.get('n'))

        self.assertEqual(0, coll.count())

        # write errors
        coll.insert_one({'_id': 1})
        try:
            self.listener.results.clear()
            coll.insert_one({'_id': 1})
        except OperationFailure:
            pass
        results = self.listener.results
        started = results['started'][0]
        succeeded = results['succeeded'][0]
        self.assertEqual(0, len(results['failed']))
        self.assertIsInstance(started, monitoring.CommandStartedEvent)
        expected = SON([('insert', coll.name),
                        ('ordered', True),
                        ('documents', [{'_id': 1}]),
                        ('writeConcern', {'w': 1})])
        self.assertEqual(expected, started.command)
        self.assertEqual('pymongo_test', started.database_name)
        self.assertEqual('insert', started.command_name)
        self.assertIsInstance(started.request_id, int)
        self.assertEqual(self.client.address, started.connection_id)
        self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
        self.assertIsInstance(succeeded.duration_micros, int)
        self.assertEqual(started.command_name, succeeded.command_name)
        self.assertEqual(started.request_id, succeeded.request_id)
        self.assertEqual(started.connection_id, succeeded.connection_id)
        reply = succeeded.reply
        self.assertEqual(1, reply.get('ok'))
        self.assertEqual(0, reply.get('n'))
        errors = reply.get('writeErrors')
        self.assertIsInstance(errors, list)
        error = errors[0]
        self.assertEqual(0, error.get('index'))
        self.assertIsInstance(error.get('code'), int)
        self.assertIsInstance(error.get('errmsg'), text_type)

    def test_legacy_writes(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            coll = self.client.pymongo_test.test
            coll.drop()
            self.listener.results.clear()

            # Implied write concern insert
            _id = coll.insert({'x': 1})
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            expected = SON([('insert', coll.name),
                            ('ordered', True),
                            ('documents', [{'_id': _id, 'x': 1}])])
            self.assertEqual(expected, started.command)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertEqual('insert', started.command_name)
            self.assertIsInstance(started.request_id, int)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual(started.command_name, succeeded.command_name)
            self.assertEqual(started.request_id, succeeded.request_id)
            self.assertEqual(started.connection_id, succeeded.connection_id)
            reply = succeeded.reply
            self.assertEqual(1, reply.get('ok'))
            self.assertEqual(1, reply.get('n'))

            # Unacknowledged insert
            self.listener.results.clear()
            _id = coll.insert({'x': 1}, w=0)
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            expected = SON([('insert', coll.name),
                            ('ordered', True),
                            ('documents', [{'_id': _id, 'x': 1}]),
                            ('writeConcern', {'w': 0})])
            self.assertEqual(expected, started.command)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertEqual('insert', started.command_name)
            self.assertIsInstance(started.request_id, int)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual(started.command_name, succeeded.command_name)
            self.assertEqual(started.request_id, succeeded.request_id)
            self.assertEqual(started.connection_id, succeeded.connection_id)
            self.assertEqual(succeeded.reply, {'ok': 1})

            # Explicit write concern insert
            self.listener.results.clear()
            _id = coll.insert({'x': 1}, w=1)
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            expected = SON([('insert', coll.name),
                            ('ordered', True),
                            ('documents', [{'_id': _id, 'x': 1}]),
                            ('writeConcern', {'w': 1})])
            self.assertEqual(expected, started.command)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertEqual('insert', started.command_name)
            self.assertIsInstance(started.request_id, int)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual(started.command_name, succeeded.command_name)
            self.assertEqual(started.request_id, succeeded.request_id)
            self.assertEqual(started.connection_id, succeeded.connection_id)
            reply = succeeded.reply
            self.assertEqual(1, reply.get('ok'))
            self.assertEqual(1, reply.get('n'))

            # remove all
            self.listener.results.clear()
            res = coll.remove({'x': 1}, w=1)
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            expected = SON([('delete', coll.name),
                            ('ordered', True),
                            ('deletes', [SON([('q', {'x': 1}),
                                              ('limit', 0)])]),
                            ('writeConcern', {'w': 1})])
            self.assertEqual(expected, started.command)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertEqual('delete', started.command_name)
            self.assertIsInstance(started.request_id, int)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual(started.command_name, succeeded.command_name)
            self.assertEqual(started.request_id, succeeded.request_id)
            self.assertEqual(started.connection_id, succeeded.connection_id)
            reply = succeeded.reply
            self.assertEqual(1, reply.get('ok'))
            self.assertEqual(res['n'], reply.get('n'))

            # upsert
            self.listener.results.clear()
            oid = ObjectId()
            coll.update({'_id': oid}, {'_id': oid, 'x': 1}, upsert=True, w=1)
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            expected = SON([('update', coll.name),
                            ('ordered', True),
                            ('updates', [SON([('q', {'_id': oid}),
                                              ('u', {'_id': oid, 'x': 1}),
                                              ('multi', False),
                                              ('upsert', True)])]),
                            ('writeConcern', {'w': 1})])
            self.assertEqual(expected, started.command)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertEqual('update', started.command_name)
            self.assertIsInstance(started.request_id, int)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual(started.command_name, succeeded.command_name)
            self.assertEqual(started.request_id, succeeded.request_id)
            self.assertEqual(started.connection_id, succeeded.connection_id)
            reply = succeeded.reply
            self.assertEqual(1, reply.get('ok'))
            self.assertEqual(1, reply.get('n'))
            self.assertEqual([{'index': 0, '_id': oid}], reply.get('upserted'))

            # update one
            self.listener.results.clear()
            coll.update({'x': 1}, {'$inc': {'x': 1}})
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            expected = SON([('update', coll.name),
                            ('ordered', True),
                            ('updates', [SON([('q', {'x': 1}),
                                              ('u', {'$inc': {'x': 1}}),
                                              ('multi', False),
                                              ('upsert', False)])])])
            self.assertEqual(expected, started.command)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertEqual('update', started.command_name)
            self.assertIsInstance(started.request_id, int)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual(started.command_name, succeeded.command_name)
            self.assertEqual(started.request_id, succeeded.request_id)
            self.assertEqual(started.connection_id, succeeded.connection_id)
            reply = succeeded.reply
            self.assertEqual(1, reply.get('ok'))
            self.assertEqual(1, reply.get('n'))

            # update many
            self.listener.results.clear()
            coll.update({'x': 2}, {'$inc': {'x': 1}}, multi=True)
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            expected = SON([('update', coll.name),
                            ('ordered', True),
                            ('updates', [SON([('q', {'x': 2}),
                                              ('u', {'$inc': {'x': 1}}),
                                              ('multi', True),
                                              ('upsert', False)])])])
            self.assertEqual(expected, started.command)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertEqual('update', started.command_name)
            self.assertIsInstance(started.request_id, int)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual(started.command_name, succeeded.command_name)
            self.assertEqual(started.request_id, succeeded.request_id)
            self.assertEqual(started.connection_id, succeeded.connection_id)
            reply = succeeded.reply
            self.assertEqual(1, reply.get('ok'))
            self.assertEqual(1, reply.get('n'))

            # remove one
            self.listener.results.clear()
            coll.remove({'x': 3}, multi=False)
            results = self.listener.results
            started = results['started'][0]
            succeeded = results['succeeded'][0]
            self.assertEqual(0, len(results['failed']))
            self.assertIsInstance(started, monitoring.CommandStartedEvent)
            expected = SON([('delete', coll.name),
                            ('ordered', True),
                            ('deletes', [SON([('q', {'x': 3}),
                                              ('limit', 1)])])])
            self.assertEqual(expected, started.command)
            self.assertEqual('pymongo_test', started.database_name)
            self.assertEqual('delete', started.command_name)
            self.assertIsInstance(started.request_id, int)
            self.assertEqual(self.client.address, started.connection_id)
            self.assertIsInstance(succeeded, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeeded.duration_micros, int)
            self.assertEqual(started.command_name, succeeded.command_name)
            self.assertEqual(started.request_id, succeeded.request_id)
            self.assertEqual(started.connection_id, succeeded.connection_id)
            reply = succeeded.reply
            self.assertEqual(1, reply.get('ok'))
            self.assertEqual(1, reply.get('n'))

            self.assertEqual(0, coll.count())

    def test_insert_many(self):
        # This always uses the bulk API.
        coll = self.client.pymongo_test.test
        coll.drop()
        self.listener.results.clear()

        big = 'x' * (1024 * 1024 * 4)
        docs = [{'_id': i, 'big': big} for i in range(6)]
        coll.insert_many(docs)
        results = self.listener.results
        started = results['started']
        succeeded = results['succeeded']
        self.assertEqual(0, len(results['failed']))
        documents = []
        count = 0
        operation_id = started[0].operation_id
        self.assertIsInstance(operation_id, int)
        for start, succeed in zip(started, succeeded):
            self.assertIsInstance(start, monitoring.CommandStartedEvent)
            cmd = start.command
            self.assertEqual(['insert', 'ordered', 'documents'],
                             list(cmd.keys()))
            self.assertEqual(coll.name, cmd['insert'])
            self.assertIs(True, cmd['ordered'])
            documents.extend(cmd['documents'])
            self.assertEqual('pymongo_test', start.database_name)
            self.assertEqual('insert', start.command_name)
            self.assertIsInstance(start.request_id, int)
            self.assertEqual(self.client.address, start.connection_id)
            self.assertIsInstance(succeed, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeed.duration_micros, int)
            self.assertEqual(start.command_name, succeed.command_name)
            self.assertEqual(start.request_id, succeed.request_id)
            self.assertEqual(start.connection_id, succeed.connection_id)
            self.assertEqual(start.operation_id, operation_id)
            self.assertEqual(succeed.operation_id, operation_id)
            reply = succeed.reply
            self.assertEqual(1, reply.get('ok'))
            count += reply.get('n', 0)
        self.assertEqual(documents, docs)
        self.assertEqual(6, count)

    def test_legacy_insert_many(self):
        # On legacy servers this uses bulk OP_INSERT.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)

            coll = self.client.pymongo_test.test
            coll.drop()
            self.listener.results.clear()

            # Force two batches on legacy servers.
            big = 'x' * (1024 * 1024 * 12)
            docs = [{'_id': i, 'big': big} for i in range(6)]
            coll.insert(docs)
            results = self.listener.results
            started = results['started']
            succeeded = results['succeeded']
            self.assertEqual(0, len(results['failed']))
            documents = []
            count = 0
            operation_id = started[0].operation_id
            self.assertIsInstance(operation_id, int)
            for start, succeed in zip(started, succeeded):
                self.assertIsInstance(start, monitoring.CommandStartedEvent)
                cmd = start.command
                self.assertEqual(['insert', 'ordered', 'documents'],
                                 list(cmd.keys()))
                self.assertEqual(coll.name, cmd['insert'])
                self.assertIs(True, cmd['ordered'])
                documents.extend(cmd['documents'])
                self.assertEqual('pymongo_test', start.database_name)
                self.assertEqual('insert', start.command_name)
                self.assertIsInstance(start.request_id, int)
                self.assertEqual(self.client.address, start.connection_id)
                self.assertIsInstance(succeed, monitoring.CommandSucceededEvent)
                self.assertIsInstance(succeed.duration_micros, int)
                self.assertEqual(start.command_name, succeed.command_name)
                self.assertEqual(start.request_id, succeed.request_id)
                self.assertEqual(start.connection_id, succeed.connection_id)
                self.assertEqual(start.operation_id, operation_id)
                self.assertEqual(succeed.operation_id, operation_id)
                reply = succeed.reply
                self.assertEqual(1, reply.get('ok'))
                count += reply.get('n', 0)
            self.assertEqual(documents, docs)
            self.assertEqual(6, count)

    def test_bulk_write(self):
        coll = self.client.pymongo_test.test
        coll.drop()
        self.listener.results.clear()

        coll.bulk_write([InsertOne({'_id': 1}),
                         UpdateOne({'_id': 1}, {'$set': {'x': 1}}),
                         DeleteOne({'_id': 1})])
        results = self.listener.results
        started = results['started']
        succeeded = results['succeeded']
        self.assertEqual(0, len(results['failed']))
        operation_id = started[0].operation_id
        pairs = list(zip(started, succeeded))
        self.assertEqual(3, len(pairs))
        for start, succeed in pairs:
            self.assertIsInstance(start, monitoring.CommandStartedEvent)
            self.assertEqual('pymongo_test', start.database_name)
            self.assertIsInstance(start.request_id, int)
            self.assertEqual(self.client.address, start.connection_id)
            self.assertIsInstance(succeed, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeed.duration_micros, int)
            self.assertEqual(start.command_name, succeed.command_name)
            self.assertEqual(start.request_id, succeed.request_id)
            self.assertEqual(start.connection_id, succeed.connection_id)
            self.assertEqual(start.operation_id, operation_id)
            self.assertEqual(succeed.operation_id, operation_id)

        expected = SON([('insert', coll.name),
                        ('ordered', True),
                        ('documents', [{'_id': 1}])])
        self.assertEqual(expected, started[0].command)
        expected = SON([('update', coll.name),
                        ('ordered', True),
                        ('updates', [SON([('q', {'_id': 1}),
                                          ('u', {'$set': {'x': 1}}),
                                          ('multi', False),
                                          ('upsert', False)])])])
        self.assertEqual(expected, started[1].command)
        expected = SON([('delete', coll.name),
                        ('ordered', True),
                        ('deletes', [SON([('q', {'_id': 1}),
                                          ('limit', 1)])])])
        self.assertEqual(expected, started[2].command)

    def test_write_errors(self):
        coll = self.client.pymongo_test.test
        coll.drop()
        self.listener.results.clear()

        try:
            coll.bulk_write([InsertOne({'_id': 1}),
                             InsertOne({'_id': 1}),
                             InsertOne({'_id': 1}),
                             DeleteOne({'_id': 1})],
                             ordered=False)
        except OperationFailure:
            pass
        results = self.listener.results
        started = results['started']
        succeeded = results['succeeded']
        self.assertEqual(0, len(results['failed']))
        operation_id = started[0].operation_id
        pairs = list(zip(started, succeeded))
        errors = []
        for start, succeed in pairs:
            self.assertIsInstance(start, monitoring.CommandStartedEvent)
            self.assertEqual('pymongo_test', start.database_name)
            self.assertIsInstance(start.request_id, int)
            self.assertEqual(self.client.address, start.connection_id)
            self.assertIsInstance(succeed, monitoring.CommandSucceededEvent)
            self.assertIsInstance(succeed.duration_micros, int)
            self.assertEqual(start.command_name, succeed.command_name)
            self.assertEqual(start.request_id, succeed.request_id)
            self.assertEqual(start.connection_id, succeed.connection_id)
            self.assertEqual(start.operation_id, operation_id)
            self.assertEqual(succeed.operation_id, operation_id)
            if 'writeErrors' in succeed.reply:
                errors.extend(succeed.reply['writeErrors'])

        self.assertEqual(2, len(errors))
        fields = set(['index', 'code', 'errmsg'])
        for error in errors:
            self.assertEqual(fields, set(error))


if __name__ == "__main__":
    unittest.main()
