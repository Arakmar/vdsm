#
# Copyright 2015 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
import itertools
from collections import deque

from testlib import VdsmTestCase as TestCaseBase
from yajsonrpc.stomp import AsyncDispatcher, Command, Frame, Headers


class TestConnection(object):

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class TestFrameHandler(object):

    def __init__(self):
        self.handle_connect_called = False
        self._outbox = deque()

    def handle_connect(self, dispatcher):
        self.handle_connect_called = True

    def handle_frame(self, dispatcher, frame):
        self.queue_frame(frame)

    def peek_message(self):
        return self._outbox[0]

    def pop_message(self):
        return self._outbox.popleft()

    @property
    def has_outgoing_messages(self):
        return (len(self._outbox) > 0)

    def queue_frame(self, frame):
        self._outbox.append(frame)


class TestDispatcher(object):

    socket = None

    def __init__(self, data):
        self._data = data

    def recv(self, buffer_size):
        return self._data

    def send(self, data):
        return len(data)


class FakeTimeGen(object):

    def __init__(self, list):
        self._chain = itertools.chain(list)

    def get_fake_time(self):
        next = self._chain.next()
        print(next)
        return next


class AsyncDispatcherTest(TestCaseBase):

    def test_handle_connect(self):
        frame_handler = TestFrameHandler()
        dispatcher = AsyncDispatcher(TestConnection(), frame_handler)

        dispatcher.handle_connect(None)

        self.assertTrue(frame_handler.handle_connect_called)

    def test_handle_read(self):
        frame_handler = TestFrameHandler()
        headers = {Headers.CONTENT_LENGTH: '78',
                   Headers.DESTINATION: 'jms.topic.vdsm_responses',
                   Headers.CONTENT_TYPE: 'application/json',
                   Headers.SUBSCRIPTION: 'ad052acb-a934-4e10-8ec3-00c7417ef8d'}
        body = ('{"jsonrpc": "2.0", "id": "e8a936a6-d886-4cfa-97b9-2d54209053f'
                'f", "result": []}')
        frame = Frame(command=Command.MESSAGE, headers=headers, body=body)
        dispatcher = AsyncDispatcher(TestConnection(), frame_handler)

        dispatcher.handle_read(TestDispatcher(frame.encode()))

        self.assertTrue(frame_handler.has_outgoing_messages)
        recv_frame = frame_handler.pop_message()
        self.assertEquals(Command.MESSAGE, recv_frame.command)
        self.assertEquals(body, recv_frame.body)

    def test_heartbeat_calc(self):
        dispatcher = AsyncDispatcher(
            TestConnection(), TestFrameHandler(),
            clock=FakeTimeGen([4000000.0, 4000002.0]).get_fake_time
        )
        dispatcher.setHeartBeat(8000, 0)

        self.assertEquals(6, dispatcher.next_check_interval())

    def test_heartbeat_exceeded(self):
        frame_handler = TestFrameHandler()
        dispatcher = AsyncDispatcher(
            TestConnection(), frame_handler,
            clock=FakeTimeGen([4000000.0, 4000012.0]).get_fake_time
        )
        dispatcher.setHeartBeat(8000, 0)

        self.assertTrue(dispatcher.writable(None))
        self.assertTrue(frame_handler.has_outgoing_messages)

    def test_no_incoming_heartbeat(self):
        dispatcher = AsyncDispatcher(TestConnection(), TestFrameHandler())

        with self.assertRaises(ValueError):
            dispatcher.setHeartBeat(8000, 8000)

    def test_no_heartbeat(self):
        dispatcher = AsyncDispatcher(TestConnection(), TestFrameHandler())
        dispatcher.setHeartBeat(0, 0)

        self.assertIsNone(dispatcher.next_check_interval())

    def test_handle_write(self):
        headers = {Headers.CONTENT_LENGTH: '78',
                   Headers.DESTINATION: 'jms.topic.vdsm_responses',
                   Headers.CONTENT_TYPE: 'application/json',
                   Headers.SUBSCRIPTION: 'ad052acb-a934-4e10-8ec3-00c7417ef8d'}
        body = ('{"jsonrpc": "2.0", "id": "e8a936a6-d886-4cfa-97b9-2d54209053f'
                'f", "result": []}')
        frame = Frame(command=Command.MESSAGE, headers=headers, body=body)
        frame_handler = TestFrameHandler()
        frame_handler.handle_frame(None, frame)

        dispatcher = AsyncDispatcher(TestConnection(), frame_handler)
        self.assertTrue(dispatcher.writable(None))

        dispatcher.handle_write(TestDispatcher(''))
        self.assertFalse(frame_handler.has_outgoing_messages)

    def test_handle_close(self):
        connection = TestConnection()
        dispatcher = AsyncDispatcher(connection, TestFrameHandler())

        dispatcher.handle_close(None)

        self.assertTrue(connection.closed)
