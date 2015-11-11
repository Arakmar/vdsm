#
# Copyright 2012 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
import logging
import time
from clientIF import clientIF
from contextlib import contextmanager
from monkeypatch import MonkeyPatch
from testValidation import slowtest
from vdsm import executor

from testlib import VdsmTestCase as TestCaseBase, \
    expandPermutations, \
    permutations, \
    dummyTextGenerator

from jsonRpcHelper import \
    PERMUTATIONS, \
    constructClient, \
    FakeClientIf

from yajsonrpc import \
    JsonRpcError, \
    JsonRpcMethodNotFoundError, \
    JsonRpcNoResponseError, \
    JsonRpcInternalError, \
    JsonRpcRequest


CALL_TIMEOUT = 3
CALL_ID = '2c8134fd-7dd4-4cfc-b7f8-6b7549399cb6'


class _DummyBridge(object):
    log = logging.getLogger("tests.DummyBridge")
    cif = None

    def getBridgeMethods(self):
        return ((self.echo, 'echo'),
                (self.ping, 'ping'),
                (self.slow_response, 'slow_response'))

    def echo(self, text):
        self.log.info("ECHO: '%s'", text)
        return text

    def ping(self):
        return None

    def slow_response(self):
        time.sleep(CALL_TIMEOUT + 2)

    def double_response(self):
        self.cif.notify('vdsm.double_response', content=True)
        return 'sent'

    def register_server_address(self, server_address):
        self.server_address = server_address

    def unregister_server_address(self):
        self.server_address = None


def getInstance():
    return FakeClientIf()


def dispatch(callable, timeout=None):
    raise executor.TooManyTasks


@expandPermutations
class JsonRpcServerTests(TestCaseBase):
    def _callTimeout(self, client, methodName, params=None, rid=None,
                     timeout=None):
        responses = client.call(JsonRpcRequest(methodName, params, rid),
                                timeout=CALL_TIMEOUT)
        if not responses:
            raise JsonRpcNoResponseError(methodName)
        resp = responses[0]
        if resp.error is not None:
            raise JsonRpcError(resp.error['code'], resp.error['message'])

        return resp.result

    @contextmanager
    def _client(self, clientFactory):
            client = clientFactory()
            try:
                yield client
            finally:
                client.close()

    @MonkeyPatch(clientIF, 'getInstance', getInstance)
    @permutations(PERMUTATIONS)
    def testMethodCallArgList(self, ssl, type):
        data = dummyTextGenerator(1024)

        bridge = _DummyBridge()
        with constructClient(self.log, bridge, ssl, type) as clientFactory:
            with self._client(clientFactory) as client:
                self.log.info("Calling 'echo'")
                if type == "xml":
                    response = client.send("echo", (data,))
                    self.assertEquals(response, data)
                else:
                    self.assertEquals(self._callTimeout(client, "echo",
                                      (data,), CALL_ID), data)

    @MonkeyPatch(clientIF, 'getInstance', getInstance)
    @permutations(PERMUTATIONS)
    def testMethodCallArgDict(self, ssl, type):
        data = dummyTextGenerator(1024)

        bridge = _DummyBridge()
        with constructClient(self.log, bridge, ssl, type) as clientFactory:
            with self._client(clientFactory) as client:
                if type == "xml":
                        response = client.send("echo", (data,))
                        self.assertEquals(response, data)
                else:
                    self.assertEquals(self._callTimeout(client, "echo",
                                      {'text': data}, CALL_ID), data)

    @MonkeyPatch(clientIF, 'getInstance', getInstance)
    @permutations(PERMUTATIONS)
    def testMethodMissingMethod(self, ssl, type):
        bridge = _DummyBridge()
        with constructClient(self.log, bridge, ssl, type) as clientFactory:
            with self._client(clientFactory) as client:
                if type == "xml":
                    response = client.send("I.DO.NOT.EXIST :(", ())
                    self.assertTrue("\"I.DO.NOT.EXIST :(\" is not supported"
                                    in response)
                else:
                    with self.assertRaises(JsonRpcError) as cm:
                        self._callTimeout(client, "I.DO.NOT.EXIST :(", [],
                                          CALL_ID)

                    self.assertEquals(cm.exception.code,
                                      JsonRpcMethodNotFoundError().code)

    @MonkeyPatch(clientIF, 'getInstance', getInstance)
    @permutations(PERMUTATIONS)
    def testMethodBadParameters(self, ssl, type):
        # Without a schema the server returns an internal error

        bridge = _DummyBridge()
        with constructClient(self.log, bridge, ssl, type) as clientFactory:
            with self._client(clientFactory) as client:
                if type == "xml":
                    response = client.send("echo", ())
                    self.assertTrue("echo() takes exactly 2 arguments"
                                    in response)
                else:
                    with self.assertRaises(JsonRpcError) as cm:
                        self._callTimeout(client, "echo", [],
                                          CALL_ID)

                    self.assertEquals(cm.exception.code,
                                      JsonRpcInternalError().code)

    @MonkeyPatch(clientIF, 'getInstance', getInstance)
    @permutations(PERMUTATIONS)
    def testMethodReturnsNullAndServerReturnsTrue(self, ssl, type):
        bridge = _DummyBridge()
        with constructClient(self.log, bridge, ssl, type) as clientFactory:
            with self._client(clientFactory) as client:
                if type == "xml":
                    response = client.send("ping", ())
                    # for xml empty response is not allowed by design
                    self.assertTrue("None unless allow_none is enabled"
                                    in response)
                else:
                    res = self._callTimeout(client, "ping", [],
                                            CALL_ID)
                    self.assertEquals(res, True)

    @MonkeyPatch(clientIF, 'getInstance', getInstance)
    @permutations(PERMUTATIONS)
    def testDoubleResponse(self, ssl, type):
        bridge = _DummyBridge()
        with constructClient(self.log, bridge, ssl, type) as clientFactory:
            with self._client(clientFactory) as client:
                if type == "xml":
                    # ignore notifications for xmlrpc
                    pass
                else:
                    def callback(client, event, params):
                        self.assertEquals(event, 'vdsm.double_response')
                        self.assertEquals(params['content'], True)

                    client.registerEventCallback(callback)
                    res = self._callTimeout(client, "double_response", [],
                                            CALL_ID)
                    self.assertEquals(res, 'sent')

    @slowtest
    @MonkeyPatch(clientIF, 'getInstance', getInstance)
    @permutations(PERMUTATIONS)
    def testSlowMethod(self, ssl, type):
        bridge = _DummyBridge()
        with constructClient(self.log, bridge, ssl, type) as clientFactory:
            with self._client(clientFactory) as client:
                if type == "xml":
                    # we do not provide timeout for xmlrpc
                    pass
                else:
                    with self.assertRaises(JsonRpcError) as cm:
                        self._callTimeout(client, "slow_response", [], CALL_ID)

                    self.assertEquals(cm.exception.code,
                                      JsonRpcNoResponseError().code)

    @MonkeyPatch(clientIF, 'getInstance', getInstance)
    @MonkeyPatch(executor.Executor, 'dispatch', dispatch)
    @permutations(PERMUTATIONS)
    def testFullExecutor(self, ssl, type):
        bridge = _DummyBridge()
        with constructClient(self.log, bridge, ssl, type) as clientFactory:
            with self._client(clientFactory) as client:
                if type == "xml":
                    # TODO start using executor for xmlrpc
                    pass
                else:
                    with self.assertRaises(JsonRpcError) as cm:
                        self._callTimeout(client, "no_method", [], CALL_ID)

                    self.assertEquals(cm.exception.code,
                                      JsonRpcInternalError().code)
