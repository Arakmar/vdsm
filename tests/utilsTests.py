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

import collections
import os.path
import contextlib
import errno
import logging
import os
import sys
import threading

from testrunner import forked, online_cpus
from testrunner import VdsmTestCase as TestCaseBase
from testrunner import permutations, expandPermutations

from vdsm import constants
from vdsm import taskset
from vdsm import utils

from monkeypatch import MonkeyPatch
from testValidation import checkSudo
from testValidation import stresstest
from vmTestsData import VM_STATUS_DUMP
import copy
import time
import timeit

EXT_SLEEP = "sleep"


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
        sproc = utils.execCmd(args, sync=False)
        stats = utils.pidStat(sproc.pid)
        pid = int(stats.pid)
        # procName comes in the format of (procname)
        name = stats.comm
        self.assertEquals(pid, sproc.pid)
        self.assertEquals(name, args[0])
        sproc.kill()
        sproc.wait()


class PgrepTests(TestCaseBase):
    def test(self):
        sleepProcs = []
        for i in range(3):
            sleepProcs.append(utils.execCmd([EXT_SLEEP, "3"], sync=False,
                              sudo=False))

        pids = utils.pgrep(EXT_SLEEP)
        for proc in sleepProcs:
            self.assertTrue(proc.pid in pids, "pid %d was not located by pgrep"
                            % proc.pid)

        for proc in sleepProcs:
            proc.kill()
            proc.wait()


class GetCmdArgsTests(TestCaseBase):
    def test(self):
        args = [EXT_SLEEP, "4"]
        sproc = utils.execCmd(args, sync=False)
        try:
            self.assertEquals(utils.getCmdArgs(sproc.pid), tuple(args))
        finally:
            sproc.kill()
            sproc.wait()

    def testZombie(self):
        args = [EXT_SLEEP, "0"]
        sproc = utils.execCmd(args, sync=False)
        sproc.kill()
        try:
            test = lambda: self.assertEquals(utils.getCmdArgs(sproc.pid),
                                             tuple())
            utils.retry(AssertionError, test, tries=10, sleep=0.1)
        finally:
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
            self.assertIn(NAME, e.strerror)


class GeneralUtilsTests(TestCaseBase):
    def testPanic(self):
        self.assertRaises(AssertionError, utils.panic, "panic test")

    def testAnyFnmatch(self):
        self.assertTrue(utils.anyFnmatch('test1', ['test0', 'test1']))

    def testReadMemInfo(self):
        meminfo = utils.readMemInfo()
        # most common fields as per man 5 proc
        # add your own here
        fields = ('MemTotal', 'MemFree', 'Buffers', 'Cached', 'SwapCached',
                  'Active', 'Inactive', 'SwapTotal', 'SwapFree', 'Dirty',
                  'Writeback', 'Mapped', 'Slab', 'VmallocTotal',
                  'VmallocUsed', 'VmallocChunk')
        for field in fields:
            self.assertIn(field, meminfo)
            self.assertTrue(isinstance(meminfo[field], int))

    def testParseMemInfo(self):
        testPath = os.path.realpath(__file__)
        dirName = os.path.dirname(testPath)
        path = os.path.join(dirName, "mem_info.out")
        with open(path) as f:
            meminfo = utils._parseMemInfo(f.readlines())
        # testing some random fields
        self.assertEquals(meminfo['NFS_Unstable'], 0)
        self.assertEquals(meminfo['KernelStack'], 2760)
        self.assertEquals(meminfo['Inactive'], 1432748)

    def testGrouper(self):
        iterable = '1234567890'
        grouped = [('1', '2'), ('3', '4'), ('5', '6'), ('7', '8'), ('9', '0')]
        self.assertEquals(list(utils.grouper(iterable, 2)), grouped)

        iterable += 'a'
        grouped.append(('a', None))
        self.assertEquals(list(utils.grouper(iterable, 2)), grouped)

        iterable += 'bcde'
        grouped = [('1', '2', '3'), ('4', '5', '6'), ('7', '8', '9'),
                   ('0', 'a', 'b'), ('c', 'd', 'e')]
        self.assertEquals(list(utils.grouper(iterable, 3)), grouped)

        grouped = [('1', '2', '3', '4', '5'), ('6', '7', '8', '9', '0'),
                   ('a', 'b', 'c', 'd', 'e')]
        self.assertEquals(list(utils.grouper(iterable, 5)), grouped)


class AsyncProcessOperationTests(TestCaseBase):
    def _echo(self, text):
        proc = utils.execCmd(["echo", "-n", "test"], sync=False)

        def parse(rc, out, err):
            return out

        return utils.AsyncProcessOperation(proc, parse)

    def _sleep(self, t):
        proc = utils.execCmd(["sleep", str(t)], sync=False)
        return utils.AsyncProcessOperation(proc)

    def _fail(self, t):
        proc = utils.execCmd(["sleep", str(t)], sync=False)

        def parse(rc, out, err):
            raise Exception("TEST!!!")

        return utils.AsyncProcessOperation(proc, parse)

    def test(self):
        aop = self._sleep(1)
        self.assertEquals(aop.result(), ((0, "", ""), None))

    def testAlreadyExitedSuccess(self):
        aop = self._sleep(0)
        time.sleep(1)
        self.assertEquals(aop.result(), ((0, "", ""), None))

    def testAlreadyExitedFail(self):
        aop = self._sleep("hello")
        time.sleep(1)
        ((rc, out, err), err) = aop.result()
        self.assertEquals(err, None)
        self.assertEquals(rc, 1)

    def testWait(self):
        aop = self._sleep(1)
        aop.wait(timeout=2)

    def testParser(self):
        aop = self._echo("test")
        self.assertEquals(aop.result(), ("test", None))

    def testStop(self):
        aop = self._sleep(10)
        aop.stop()

        start = time.time()
        aop.result()
        end = time.time()
        duration = end - start
        self.assertTrue(duration < 2)

    def testException(self):
        aop = self._fail(1)
        res, err = aop.result()
        self.assertEquals(res, None)
        self.assertNotEquals(err, None)


class CallbackChainTests(TestCaseBase):
    def testCanPassIterableOfCallbacks(self):
        f = lambda: False
        callbacks = [f] * 10
        chain = utils.CallbackChain(callbacks)
        self.assertEqual(list(chain.callbacks), callbacks)

    def testEmptyChainIsNoop(self):
        chain = utils.CallbackChain()
        self.assertFalse(chain.callbacks)
        chain.start()
        chain.join()
        # assert exception isn't thrown in start on empty chain

    def testAllCallbacksAreInvokedIfTheyReturnFalse(self):
        n = 10
        counter = [n]

        def callback():
            counter[0] -= 1
            return False

        chain = utils.CallbackChain([callback] * n)
        chain.start()
        chain.join()
        self.assertEqual(counter[0], 0)

    def testChainStopsAfterSuccessfulCallback(self):
        n = 10
        counter = [n]

        def callback():
            counter[0] -= 1
            return counter[0] == 5

        chain = utils.CallbackChain([callback] * n)
        chain.start()
        chain.join()
        self.assertEquals(counter[0], 5)

    def testArgsPassedToCallback(self):
        callbackArgs = ('arg', 42, 'and another')
        callbackKwargs = {'some': 42, 'kwargs': []}

        def callback(*args, **kwargs):
            self.assertEqual(args, callbackArgs)
            self.assertEqual(kwargs, callbackKwargs)

        chain = utils.CallbackChain()
        chain.addCallback(callback, *callbackArgs, **callbackKwargs)
        chain.start()
        chain.join()


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


class RollbackContextTests(TestCaseBase):

    class UndoException(Exception):
        """A special exception for testing exceptions during undo functions"""

    class OriginalException(Exception):
        """A special exception for testing exceptions in the with statement"""

    def setUp(self):
        self._called = 0

    def _callDef(self):
        self._called += 1
        self.log.info("Incremented call count (%d)", self._called)

    def _raiseDef(self, ex=Exception()):
        self.log.info("Raised exception (%s)", ex.__class__.__name__)
        raise ex

    def test(self):
        with utils.RollbackContext() as rollback:
            rollback.prependDefer(self._callDef)

        self.assertEquals(self._called, 1)

    def testRaise(self):
        """
        Test that raising an exception in a deferred action does
        not block all subsequent actions from running
        """
        try:
            with utils.RollbackContext() as rollback:
                rollback.prependDefer(self._callDef)
                rollback.prependDefer(self._raiseDef)
                rollback.prependDefer(self._callDef)
        except Exception:
            self.assertEquals(self._called, 2)
            return

        self.fail("Exception was not raised")

    def testFirstUndoException(self):
        """
        Test that if multiple actions raise an exception only the first one is
        raised. When performing a batch rollback operations, probably the first
        exception is the root cause.
        """
        try:
            with utils.RollbackContext() as rollback:
                rollback.prependDefer(self._callDef)
                rollback.prependDefer(self._raiseDef)
                rollback.prependDefer(self._callDef)
                rollback.prependDefer(self._raiseDef, RuntimeError())
                rollback.prependDefer(self._callDef)
        except RuntimeError:
            self.assertEquals(self._called, 3)
            return
        except Exception:
            self.fail("Wrong exception was raised")

        self.fail("Exception was not raised")

    def testKeyError(self):
        """
        KeyError is raised as a tuple and not expection. Re-raising it
        should be aware of this fact and handled carfully.
        """
        try:
            with utils.RollbackContext():
                {}['aKey']
        except KeyError:
            return
        except Exception:
            self.fail("Wrong exception was raised")

        self.fail("Exception was not raised")

    def testPreferOriginalException(self):
        """
        Test that if an exception is raised both from the with
        statement and from the finally clause, the one from the with
        statement is the one that's actually raised.
        More info in: http://docs.python.org/
        2.6/library/stdtypes.html#contextmanager.__exit__
        """
        try:
            with utils.RollbackContext() as rollback:
                rollback.prependDefer(self._raiseDef, self.UndoException())
                raise self.OriginalException()
        except self.OriginalException:
            return
        except self.UndoException:
            self.fail("Wrong exception was raised - from undo function. \
                        should have re-raised OriginalException")
        except Exception:
            self.fail("Wrong exception was raised")

        self.fail("Exception was not raised")


@expandPermutations
class ExecCmdTest(TestCaseBase):
    CMD_TYPES = ((tuple,), (list,), (iter,))

    @permutations(CMD_TYPES)
    def testNormal(self, cmd):
        rc, out, _ = utils.execCmd(cmd(('echo', 'hello world')))
        self.assertEquals(rc, 0)
        self.assertEquals(out[0], 'hello world')

    @permutations(CMD_TYPES)
    def testIoClass(self, cmd):
        rc, out, _ = utils.execCmd(cmd(('ionice',)), ioclass=2,
                                   ioclassdata=3)
        self.assertEquals(rc, 0)
        self.assertEquals(out[0].strip(), 'best-effort: prio 3')

    @permutations(CMD_TYPES)
    def testNice(self, cmd):
        rc, out, _ = utils.execCmd(cmd(('cat', '/proc/self/stat')), nice=7)
        self.assertEquals(rc, 0)
        self.assertEquals(int(out[0].split()[18]), 7)

    @permutations(CMD_TYPES)
    def testSetSid(self, cmd):
        cmd_args = (constants.EXT_PYTHON, '-c',
                    'import os; print os.getsid(os.getpid())')
        rc, out, _ = utils.execCmd(cmd(cmd_args), setsid=True)
        self.assertNotEquals(int(out[0]), os.getsid(os.getpid()))

    @permutations(CMD_TYPES)
    def testSudo(self, cmd):
        checkSudo(['echo'])
        rc, out, _ = utils.execCmd(cmd(('grep', 'Uid', '/proc/self/status')),
                                   sudo=True)
        self.assertEquals(rc, 0)
        self.assertEquals(int(out[0].split()[2]), 0)


class ExecCmdAffinityTests(TestCaseBase):

    CPU_SET = frozenset([0])

    @forked
    @MonkeyPatch(utils, '_USING_CPU_AFFINITY', False)
    def testResetAffinityByDefault(self):
        try:
            proc = utils.execCmd((EXT_SLEEP, '30s'), sync=False)

            self.assertEquals(taskset.get(proc.pid),
                              taskset.get(os.getpid()))
        finally:
            proc.kill()

    @forked
    @MonkeyPatch(utils, '_USING_CPU_AFFINITY', True)
    def testResetAffinityWhenConfigured(self):
        taskset.set(os.getpid(), self.CPU_SET)
        self.assertEquals(taskset.get(os.getpid()), self.CPU_SET)

        try:
            proc = utils.execCmd((EXT_SLEEP, '30s'), sync=False)

            self.assertEquals(taskset.get(proc.pid), online_cpus())
        finally:
            proc.kill()

    @forked
    @MonkeyPatch(utils, '_USING_CPU_AFFINITY', True)
    def testKeepAffinity(self):
        taskset.set(os.getpid(), self.CPU_SET)
        self.assertEquals(taskset.get(os.getpid()), self.CPU_SET)

        try:
            proc = utils.execCmd((EXT_SLEEP, '30s'),
                                 sync=False,
                                 resetCpuAffinity=False)

            self.assertEquals(taskset.get(proc.pid), self.CPU_SET)
        finally:
            proc.kill()


class ExecCmdStressTest(TestCaseBase):

    CONCURRENCY = 50
    FUNC_DELAY = 0.01
    FUNC_CALLS = 40
    BLOCK_SIZE = 4096
    BLOCK_COUNT = 256

    def setUp(self):
        self.data = None  # Written to process stdin
        self.workers = []
        self.resume = threading.Event()

    @stresstest
    def test_read_stderr(self):
        self.check(self.read_stderr)

    @stresstest
    def test_read_stdout_stderr(self):
        self.check(self.read_stdout_stderr)

    @stresstest
    def test_write_stdin_read_stderr(self):
        self.data = 'x' * self.BLOCK_SIZE * self.BLOCK_COUNT
        self.check(self.write_stdin_read_stderr)

    def check(self, func):
        for i in xrange(self.CONCURRENCY):
            worker = Worker(self.resume, func, self.FUNC_CALLS,
                            self.FUNC_DELAY)
            self.workers.append(worker)
            worker.start()
        for worker in self.workers:
            worker.wait()
        self.resume.set()
        for worker in self.workers:
            worker.join()
        for worker in self.workers:
            if worker.exc_info:
                t, v, tb = worker.exc_info
                raise t, v, tb

    def read_stderr(self):
        args = ['if=/dev/zero',
                'of=/dev/null',
                'bs=%d' % self.BLOCK_SIZE,
                'count=%d' % self.BLOCK_COUNT]
        self.run_dd(args)

    def read_stdout_stderr(self):
        args = ['if=/dev/zero',
                'bs=%d' % self.BLOCK_SIZE,
                'count=%d' % self.BLOCK_COUNT]
        out = self.run_dd(args)
        size = self.BLOCK_SIZE * self.BLOCK_COUNT
        if len(out) < size:
            raise self.failureException("Partial read: %d/%d" % (
                                        len(out), size))

    def write_stdin_read_stderr(self):
        args = ['of=/dev/null',
                'bs=%d' % self.BLOCK_SIZE,
                'count=%d' % self.BLOCK_COUNT]
        self.run_dd(args)

    def run_dd(self, args):
        cmd = [constants.EXT_DD]
        cmd.extend(args)
        rc, out, err = utils.execCmd(cmd, raw=True, data=self.data)
        if rc != 0:
            raise self.failureException("Process failed: rc=%d err=%r" %
                                        (rc, err))
        if err == '':
            raise self.failureException("No data from stderr")
        return out


class Worker(object):

    def __init__(self, resume, func, func_calls, func_delay):
        self.exc_info = None
        self._resume = resume
        self._func = func
        self._func_calls = func_calls
        self._func_delay = func_delay
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True

    def start(self):
        self._thread.start()

    def wait(self):
        self._ready.wait()

    def join(self):
        self._thread.join()

    def _run(self):
        try:
            self._ready.set()
            self._resume.wait()
            for n in range(self._func_calls):
                self._func()
                time.sleep(self._func_delay)
        except Exception:
            self.exc_info = sys.exc_info()


class List2CmdlineeTests(TestCaseBase):

    def test_simple(self):
        args = ['/usr/bin/dd', 'iflag=direct',
                'if=/dev/a70a4106-24f2-4599-be3e-934fee6e4499/metadata',
                'bs=4096', 'count=1']
        line = ' '.join(args)
        self.assertEquals(utils._list2cmdline(args), line)

    def test_whitespace(self):
        args = ['a b', ' c ', 'd\t', '\ne']
        line = "'a b' ' c ' 'd\t' '\ne'"
        self.assertEquals(utils._list2cmdline(args), line)

    def test_unsafe(self):
        args = [c for c in '><*?[]`$|;&()#$\\"']
        line = ' '.join("'" + c + "'" for c in args)
        self.assertEquals(utils._list2cmdline(args), line)

    def test_safe(self):
        # Stolen from pipes._safechars
        line = ' '.join('%+,-./0123456789:=@ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdef'
                        'ghijklmnopqrstuvwxyz')
        args = line.split()
        self.assertEquals(utils._list2cmdline(args), line)

    def test_single_quote(self):
        args = ["don't", "try 'this'", "'at home'"]
        line = r"'don'\''t' 'try '\''this'\''' ''\''at home'\'''"
        self.assertEquals(utils._list2cmdline(args), line)

    def test_empty_arg(self):
        self.assertEquals(utils._list2cmdline(['a', '', 'b']), "a '' b")

    def test_empty(self):
        self.assertEquals(utils._list2cmdline([]), "")


class PickleCopyTests(TestCaseBase):
    def test_picklecopy_exact(self):
        self.assertEqual(utils.picklecopy(VM_STATUS_DUMP),
                         copy.deepcopy(VM_STATUS_DUMP))

    def test_picklecopy_faster(self):
        setup = """
import copy
from vdsm import utils
import vmTestsData
"""
        base = timeit.timeit('copy.deepcopy(vmTestsData.VM_STATUS_DUMP)',
                             setup=setup,
                             number=1000)
        hack = timeit.timeit('utils.picklecopy(vmTestsData.VM_STATUS_DUMP)',
                             setup=setup,
                             number=1000)
        # to justify this hack, it needs to be significantly faster, not
        # just a bit faster, hence the divisor
        # assertLess* requires python 2.7
        self.assertTrue(
            hack < base/2,
            "picklecopy [%f] not faster than deepcopy [%f]" % (hack, base))


@expandPermutations
class MemoizedTests(TestCaseBase):

    def setUp(self):
        self.values = {}
        self.accessed = collections.defaultdict(int)

    @permutations([[()], [("a",)], [("a", "b")]])
    def test_memoized_method(self, args):
        self.values[args] = 42
        self.assertEqual(self.accessed[args], 0)
        self.assertEqual(self.memoized_method(*args), 42)
        self.assertEqual(self.accessed[args], 1)
        self.assertEqual(self.memoized_method(*args), 42)
        self.assertEqual(self.accessed[args], 1)

    @permutations([[()], [("a",)], [("a", "b")]])
    def test_memoized_function(self, args):
        self.values[args] = 42
        self.assertEqual(self.accessed[args], 0)
        self.assertEqual(memoized_function(self, *args), 42)
        self.assertEqual(self.accessed[args], 1)
        self.assertEqual(memoized_function(self, *args), 42)
        self.assertEqual(self.accessed[args], 1)

    def test_key_error(self):
        self.assertRaises(KeyError, self.memoized_method)
        self.assertRaises(KeyError, self.memoized_method, "a")
        self.assertRaises(KeyError, self.memoized_method, "a", "b")

    def test_invalidate_method(self):
        args = ("a",)
        self.values[args] = 42
        self.assertEqual(self.memoized_method(*args), 42)
        self.memoized_method.invalidate()
        self.assertEqual(self.memoized_method(*args), 42)
        self.assertEqual(self.accessed[args], 2)

    def test_invalidate_function(self):
        args = ("a",)
        self.values[args] = 42
        self.assertEqual(memoized_function(self, *args), 42)
        memoized_function.invalidate()
        self.assertEqual(memoized_function(self, *args), 42)
        self.assertEqual(self.accessed[args], 2)

    @utils.memoized
    def memoized_method(self, *args):
        return self.get(args)

    def get(self, key):
        self.accessed[key] += 1
        return self.values[key]


@utils.memoized
def memoized_function(test, *args):
    return test.get(args)


class FakeLogger(object):

    def __init__(self, level):
        self.level = level
        self.messages = []

    def debug(self, fmt, *args):
        # Will fail bad logging call with mismatched fmt and args
        self.messages.append(fmt % args)

    def isEnabledFor(self, level):
        return self.level <= level


class StopwatchTests(TestCaseBase):

    def test_notset(self):
        log = FakeLogger(logging.NOTSET)
        with utils.stopwatch("message", log=log):
            pass
        self.assertNotEqual(log.messages, [])

    def test_debug(self):
        log = FakeLogger(logging.DEBUG)
        with utils.stopwatch("message", log=log):
            pass
        self.assertNotEqual(log.messages, [])

    def test_info(self):
        log = FakeLogger(logging.INFO)
        with utils.stopwatch("message", log=log):
            pass
        self.assertEqual(log.messages, [])
