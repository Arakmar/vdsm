#
# Copyright 2012-2013 Red Hat, Inc.
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
import contextlib
import errno
import logging

from testrunner import VdsmTestCase as TestCaseBase
from vdsm import utils
from storage import misc


class RetryTests(TestCaseBase):
    def testStopCallback(self):
        counter = [0]
        limit = 4

        def stopCallback():
            counter[0] += 1
            if counter[0] == limit:
                return True

            return False

        def foo():
            raise RuntimeError("If at first you don't succeed, try, try again."
                               "Then quit. There's no point in being a damn"
                               "fool about it.")
                               # W. C. Fields

        self.assertRaises(RuntimeError, utils.retry, foo, tries=(limit + 10),
                          sleep=0, stopCallback=stopCallback)
        # Make sure we had the proper amount of iterations before failing
        self.assertEquals(counter[0], limit)


class PidStatTests(TestCaseBase):
    def test(self):
        args = ["sleep", "3"]
        sproc = misc.execCmd(args, sync=False, sudo=False)
        stats = utils.pidStat(sproc.pid)
        pid = int(stats.pid)
        # procName comes in the format of (procname)
        name = stats.comm
        self.assertEquals(pid, sproc.pid)
        self.assertEquals(name, args[0])
        sproc.kill()
        sproc.wait()


class CommandPathTests(TestCaseBase):
    def testExisting(self):
        cp = utils.CommandPath('sh', 'utter nonsense', '/bin/sh')
        self.assertEquals(cp.cmd, '/bin/sh')

    def testMissing(self):
        NAME = 'nonsense'
        try:
            utils.CommandPath(NAME, 'utter nonsense').cmd
        except OSError as e:
            self.assertEquals(e.errno, errno.ENOENT)
            self.assertTrue(NAME in e.strerror,
                            msg='%s not in %s' % (NAME, e.strerror))


class GeneralUtilsTests(TestCaseBase):
    def testPanic(self):
        self.assertRaises(AssertionError, utils.panic, "panic test")


@contextlib.contextmanager
def loghandler(handler, logger=""):
    log = logging.getLogger(logger)
    log.addHandler(handler)
    try:
        yield {}
    finally:
        log.removeHandler(handler)


class TracebackTests(TestCaseBase):

    def __init__(self, *a, **kw):
        self.record = None
        super(TestCaseBase, self).__init__(*a, **kw)

    def testDefaults(self):
        @utils.traceback()
        def fail():
            raise Exception
        with loghandler(self):
            self.assertRaises(Exception, fail)
        self.assertEquals(self.record.name, "root")
        self.assertTrue(self.record.exc_text is not None)

    def testOn(self):
        logger = "test"

        @utils.traceback(on=logger)
        def fail():
            raise Exception
        with loghandler(self, logger=logger):
            self.assertRaises(Exception, fail)
        self.assertEquals(self.record.name, logger)

    def testMsg(self):
        @utils.traceback(msg="WAT")
        def fail():
            raise Exception
        with loghandler(self):
            self.assertRaises(Exception, fail)
        self.assertEquals(self.record.message, "WAT")

    # Logging handler interface

    level = logging.DEBUG

    def acquire(self):
        pass

    def release(self):
        pass

    def handle(self, record):
        assert self.record is None
        self.record = record
