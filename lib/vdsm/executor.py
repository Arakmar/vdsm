#
# Copyright 2014 Red Hat, Inc.
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

"""Threaded based executor.
Blocked tasks may be discarded, and the worker pool is automatically
replenished."""

import collections
import logging
import threading

from . import utils


class NotRunning(Exception):
    """Executor not yet started or shutting down."""


class AlreadyStarted(Exception):
    """Executor started multiple times."""


class TooManyTasks(Exception):
    """Too many tasks for this Executor."""


class Executor(object):
    """
    Executes potentially blocking task into background
    threads. Can replace stuck threads with fresh ones.
    """

    _log = logging.getLogger('Executor')

    def __init__(self, name, workers_count, max_tasks, scheduler):
        self._name = name
        self._workers_count = workers_count
        self._tasks = _TaskQueue(max_tasks)
        self._scheduler = scheduler
        self._workers = set()
        self._lock = threading.Lock()
        self._running = False

    @property
    def name(self):
        return self._name

    def start(self):
        self._log.debug('Starting executor')
        with self._lock:
            if self._running:
                raise AlreadyStarted()
            self._running = True
            for _ in xrange(self._workers_count):
                self._add_worker()

    def stop(self, wait=True):
        self._log.debug('Stopping executor')
        with self._lock:
            self._running = False
            self._tasks.clear()
            for _ in xrange(self._workers_count):
                self._tasks.put((_STOP, None))
            workers = tuple(self._workers) if wait else ()
        for worker in workers:
            worker.join()

    def dispatch(self, callable, timeout=None):
        """
        dispatches a new task to the executor.

        The task may be any callable.
        The task will be executed as soon as possible
        in one of the active workers of the executor.

        The timeout is measured from the time the callable
        is called.
        """
        if not self._running:
            raise NotRunning()
        self._tasks.put((callable, timeout))

    # Serving workers

    def _worker_discarded(self, worker):
        """
        Called from scheduler thread when worker was discarded. The worker
        thread is blocked on a task, and will exit when the task finish.
        """
        with self._lock:
            if self._running:
                self._add_worker()

    def _worker_stopped(self, worker):
        """
        Called from worker thread before it exit.
        """
        with self._lock:
            self._workers.remove(worker)

    def _next_task(self):
        """
        Called from worker thread to get the next task from the taks queue.
        Raises NotRunning exception if executor was stopped.
        """
        task, timeout = self._tasks.get()
        if task is _STOP:
            raise NotRunning()
        return task, timeout

    # Private

    def _add_worker(self):
        worker = _Worker(self, self._scheduler)
        self._workers.add(worker)


_STOP = object()


class _WorkerDiscarded(Exception):
    """ Raised if worker was discarded during execution of a task """


class _Worker(object):

    _log = logging.getLogger('Executor')
    _id = 0

    def __init__(self, executor, scheduler):
        self._executor = executor
        self._scheduler = scheduler
        self._discarded = False
        _Worker._id += 1
        name = "%s-worker-%d" % (self._executor.name, _Worker._id)
        self._thread = threading.Thread(target=self._run, name=name)
        self._thread.daemon = True
        self._log.debug('Starting worker %s' % name)
        self._thread.start()

    @property
    def name(self):
        return self._thread.name

    def join(self):
        self._log.debug('Waiting for worker %s', self.name)
        self._thread.join()

    @utils.traceback(on=_log.name)
    def _run(self):
        self._log.debug('Worker started')
        try:
            while True:
                self._execute_task()
        except NotRunning:
            self._log.debug('Worker stopped')
        except _WorkerDiscarded:
            self._log.debug('Worker was discarded')
        finally:
            self._executor._worker_stopped(self)

    def _execute_task(self):
        callable, timeout = self._executor._next_task()
        discard = self._discard_after(timeout)
        try:
            callable()
        except Exception:
            self._log.exception("Unhandled exception in %s", callable)
        finally:
            # We want to discard workers that were too slow to disarm
            # the timer. It does not matter if the thread was still
            # blocked on callable when we discard it or it just finished.
            # However, we expect that most of times only blocked threads
            # will be discarded.
            if discard is not None:
                discard.cancel()
            if self._discarded:
                raise _WorkerDiscarded()

    def _discard_after(self, timeout):
        if timeout is not None:
            return self._scheduler.schedule(timeout, self._discard)
        return None

    def _discard(self):
        if self._discarded:
            raise AssertionError("Attempt to discard worker twice")
        self._discarded = True
        self._log.debug("Worker %s discarded", self.name)
        self._executor._worker_discarded(self)


class _TaskQueue(object):
    """
    Replacement for Queue.Queue, with two important changes:

    * Queue.Queue blocks when full. We want to raise TooManyTasks instead.
    * Queue.Queue lacks the clear() operation, which is needed to implement
      the 'poison pill' pattern (described for example in
      http://pymotw.com/2/multiprocessing/communication.html )
    """

    def __init__(self, max_tasks):
        self._max_tasks = max_tasks
        self._tasks = collections.deque()
        # deque is thread safe - we can append and pop from both ends without
        # additional locking. We need this condition only for waiting. See:
        # https://docs.python.org/2.6/library/queue.html#Queue.Full
        self._cond = threading.Condition(threading.Lock())

    def put(self, task):
        """
        Put a new task in the queue.
        Do not block when full, raises TooManyTasks instead.
        """
        # There is a race here but we don't really mind. Worst case scenario,
        # we will have a bit more task then the configured maximum, but we
        # just want to avoid to have indefinite amount of tasks.
        if len(self._tasks) == self._max_tasks:
            raise TooManyTasks()
        self._tasks.append(task)
        with self._cond:
            self._cond.notify()

    def get(self):
        """
        Get a new task. Blocks if empty.
        """
        while True:
            try:
                return self._tasks.popleft()
            except IndexError:
                with self._cond:
                    if not self._tasks:
                        self._cond.wait()

    def clear(self):
        self._tasks.clear()
