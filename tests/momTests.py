#
# Copyright (C) 2015 Red Hat, Inc.
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
# Refer to the README and COPYING files for full details of the license.

from unittest import TestCase
import logging
import shutil
import tempfile
import threading
from vdsm.define import Mbytes
from vdsm.momIF import MomClient
from mom import unixrpc
from six.moves import configparser
import os.path
import monkeypatch

from vdsm import cpuarch

MOM_CONF = "/dev/null"
MOM_SOCK = "test_mom_vdsm.sock"


class DummyMomApi(object):
    def ping(self):
        return True

    def setNamedPolicy(self, policy_name, content):
        self.last_policy_name = policy_name
        self.last_policy_content = content

    def setPolicy(self, content):
        self.last_policy_name = None
        self.last_policy_content = content

    def getStatistics(self):
        return {
            "host": {
                "ksm_run": 0,
                "ksm_merge_across_nodes": 1,
                "ksm_pages_to_scan": 5,
                "ksm_pages_sharing": 100,
                "ksmd_cpu_usage": 15
            }
        }


class BrokenMomApi(object):
    def ping(self):
        return False


# Each time mom server or client is created, a new logging.StreamHanlder is
# added to the "mom" logger. This monkey-patching remove loggers and handlers
# added during the tests.
@monkeypatch.MonkeyClass(logging.getLogger().manager, "loggerDict", {})
class MomPolicyTests(TestCase):

    _TMP_DIR = tempfile.gettempdir()

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp(dir=self._TMP_DIR)
        self.config_overrides = configparser.SafeConfigParser()
        self.config_overrides.add_section("logging")
        self.config_overrides.set("logging", "log", "stdio")
        self.config_overrides.add_section("main")
        self.config_overrides.set("main", "rpc-port",
                                  os.path.join(self._tmp_dir, MOM_SOCK))

    def tearDown(self):
        shutil.rmtree(self._tmp_dir)

    def _getMomClient(self):
        return MomClient(MOM_CONF, self.config_overrides)

    def _getMomPort(self):
        return self.config_overrides.get('main', 'rpc-port')

    def _getMomServer(self, api_class=DummyMomApi):
        port = self._getMomPort()
        server = unixrpc.UnixXmlRpcServer(port)
        api = api_class()
        server.register_instance(api)
        t = threading.Thread(target=server.serve_forever)
        return server, t, api

    def _stopMomServer(self, server, t):
        server.shutdown()
        t.join()

    def testSetPolicyParameters(self):
        server, thread, api = self._getMomServer()

        try:
            client = self._getMomClient()
            thread.start()
            client.setPolicyParameters({"a": 5, "b": True, "c": "test"})
        finally:
            self._stopMomServer(server, thread)

        expected = "(set a 5)\n(set c 'test')\n(set b True)"

        self.assertEqual(api.last_policy_name, "01-parameters")
        self.assertEqual(api.last_policy_content, expected)

    def testSetPolicy(self):
        server, thread, api = self._getMomServer()

        try:
            client = self._getMomClient()
            thread.start()
            expected = "(set a 5)\n(set c 'test')\n(set b True)"
            client.setPolicy(expected)
        finally:
            self._stopMomServer(server, thread)

        self.assertEqual(api.last_policy_name, None)
        self.assertEqual(api.last_policy_content, expected)

    def testGetStatus(self):
        server, thread, api = self._getMomServer()

        try:
            client = self._getMomClient()
            thread.start()
            self.assertEqual("active", client.getStatus())
        finally:
            self._stopMomServer(server, thread)

    def testGetStatusFailing(self):
        server, thread, api = self._getMomServer(BrokenMomApi)

        try:
            client = self._getMomClient()
            thread.start()
            self.assertEqual("inactive", client.getStatus())
        finally:
            self._stopMomServer(server, thread)

    def testGetConnectionRefused(self):
        client = self._getMomClient()
        # Server is not running
        client.setPolicy("")

    def testGetKsmStats(self):
        server, thread, api = self._getMomServer()

        try:
            client = self._getMomClient()
            thread.start()
            stats = client.getKsmStats()
        finally:
            self._stopMomServer(server, thread)

        expected = {
            "ksmCpu": 15,
            "ksmMergeAcrossNodes": True,
            "ksmState": False,
            "ksmPages": 5,
            "memShared": 100 * cpuarch.PAGE_SIZE_BYTES / Mbytes
        }

        self.assertEqual(stats, expected)
