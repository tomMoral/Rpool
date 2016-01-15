from multiprocessing.pool import Pool, RUN, TERMINATE
import multiprocessing as mp
import threading
import os
import sys
from time import sleep


# Additional state to flag the pool as unusable
# with unsafe terminate function
BROKEN = 3

#
CRASH_WORKER = ("A process was killed during the execution "
                "a multiprocessing job.")
CRASH_RESULT_HANDLER = ("The result handler crashed. This is probably"
                        "due to a result unpickling error.")
CRASH_TASK_HANDLER = ("The task handler crashed. This is probably"
                      "due to a result pickling error.")

# Protect the queue fro being reused
_local = threading.local()


# Check if a thread has been started
def _is_started(thread):
    if hasattr(thread, "_started"):
        return thread._started.is_set()
    # Backward compat for python 2.7
    return thread._Thread__started.is_set()


def get_reusable_pool(*args, **kwargs):
    _pool = getattr(_local, '_pool', None)
    processes = kwargs.get('processes')
    if _pool is None:
        _local._pool = _pool = _ReusablePool(*args, **kwargs)
    else:
        _pool._maintain_pool()
        if _pool._state != RUN:
            mp.util.debug("Create a new pool with {} processes as the "
                          "previous one was in state {}"
                          "".format(processes, _pool._state))
            with _pool.maintain_lock:
                _pool.terminate()
            _local._pool = None
            return get_reusable_pool(*args, **kwargs)
        else:
            _pool._resize(processes)
    return _pool


class _ReusablePool(Pool):
    """A Pool, not tolerant to fault and reusable"""
    def __init__(self, processes=None, initializer=None, initargs=(),
                 maxtasksperchild=None, context=None):
        self.maintain_lock = mp.Lock()
        if sys.version_info[:2] >= (3, 4):
            super(_ReusablePool, self).__init__(
                processes, initializer, initargs, maxtasksperchild, context)
        else:
            super(_ReusablePool, self).__init__(
                processes, initializer, initargs, maxtasksperchild)

    def _has_started_thread(self, thread_name):
        thread = getattr(self, thread_name, None)
        if thread is not None:
            return _is_started(thread)
        return False

    def _join_exited_workers(self):
        """Cleanup after any worker processes which have exited due to reaching
        their specified lifetime.  Returns True if any workers were cleaned up.
        """
        cleaned = False
        for worker in self._pool[:]:
            if worker.exitcode is not None:
                # worker exited
                mp.util.debug('Cleaning up worker %r' % worker)
                worker.join()
                cleaned = True
                if worker.exitcode != 0:
                    mp.util.debug('A worker have failed in some ways, we will '
                                  'flag all current jobs as failed')
                    self._clean_up_crash(cause_msg=CRASH_WORKER,
                                         exitcode=worker.exitcode)
                    mp.util.sub_warning(
                        "Pool might be corrupted. Worker exited with "
                        "error code {}".format(worker.exitcode))
                    raise _BrokenPoolError(worker.exitcode)
                self._pool.remove(worker)
        if (self._has_started_thread("_result_handler") and
                not self._result_handler.is_alive()):
            self._clean_up_crash(cause_msg=CRASH_RESULT_HANDLER)
            print('\n\nPool kill by result_handler')
            raise _BrokenPoolError(worker.exitcode)
        if (self._has_started_thread("_task_handler") and
                not self._task_handler.is_alive()):
            self._clean_up_crash(cause_msg=CRASH_TASK_HANDLER)
            print('\n\nPool kill by taskhandler')
            raise _BrokenPoolError(worker.exitcode)
        return cleaned

    def terminate(self):
        if self._state != BROKEN:
            super(_ReusablePool, self).terminate()
        else:
            n_tries = 1000
            delay = .001
            for i in range(n_tries):
                alive = False
                for p in self._pool:
                    alive |= p.exitcode is None
                alive |= self._result_handler.is_alive()
                alive |= self._task_handler.is_alive()
                if not alive:
                    return
                sleep(delay)
            # TODO - kill -9 ?
            print('\n\n' + '=' * 79)
            mp.util.sub_warning("Terminate was called on a BROKEN pool but "
                                "some processes were still alive.")
            for p in self._pool:
                print(p.name, "alive: ", p.exitcode is None)
            print("Result handler {} alive: {}".format(
                hex(self._result_handler.ident),
                self._result_handler.is_alive()))
            print("Task handler {} alive: {}".format(
                hex(self._task_handler.ident), self._task_handler.is_alive()))
            print("Worker handler {} alive: {}".format(
                hex(self._worker_handler.ident),
                self._worker_handler.is_alive()))
            print('=' * 79)

    def _maintain_pool(self):
        """Clean up any exited workers and start replacements for them.
        """
        try:
            with self.maintain_lock:
                if (self._state != BROKEN and (
                        self._join_exited_workers() or
                        self._processes >= len(self._pool))):
                    self._repopulate_pool()
        except _BrokenPoolError:
            pass

    def _clean_up_crash(self, cause_msg, exitcode=None):
        if self._state == BROKEN:
            return
        # Terminate tasks handler by sentinel
        self._taskqueue.put(None)

        # Terminate the worker handler thread
        threading.current_thread()._state = TERMINATE
        for p in self._pool:
            p.terminate()

        # Flag all the _cached job as failed
        success, value = (False, AbortedWorkerError(cause_msg, exitcode))
        for k in list(self._cache.keys()):
            result = self._cache[k]
            if hasattr(result, '_index'):
                while result._index != result._length:
                    result._set(result._index, (success, value))
            else:
                result._set(0, (success, value))

        # Terminate result handler by sentinel
        self._result_handler._state = TERMINATE

        # This avoids deadlock caused by putting a sentinel in the outqueue
        # as it might be locked by a dead worker
        self._outqueue._wlock = None
        if sys.version_info[:2] < (3, 4):
            self._outqueue._make_methods()
        self._outqueue.put(None)

        # Flag the pool as broken
        self._state = BROKEN

    def _wait_complete(self):
        if len(self._cache) > 0:
            mp.util.sub_warning("You are trying to resize a working pool. "
                                "The pool will wait until the jobs are "
                                "finished and then resize it. This can "
                                "slow down your code.")
        while len(self._cache) > 0:
            sleep(.1)

    def _resize(self, processes=None):
        """Resize the pool to the desired number of processes
        """
        if processes is None:
            processes = os.cpu_count() or 1
        if processes < 1:
            raise ValueError("Number of processes must be at least 1")
        if self._processes == processes:
            return
        self._wait_complete()
        self._processes = processes
        if len(self._pool) > processes:
            # Sentinel excess workers, they will terminate and
            # be collected asynchronously
            for worker in self._pool[processes:]:
                self._inqueue.put(None)

        # Make sure that the pool as the expected number of workers
        # up and ready
        while processes != len(self._pool):
            self._maintain_pool()

        assert processes == len(self._pool), (
            "Resize pool failed. Got {} extra  processes"
            "".format(processes - len(self._pool)))

    @staticmethod
    def _help_stuff_finish(inqueue, outqueue, task_handler, size):
        # task_handler may be blocked trying to put items on inqueue
        mp.util.debug("removing tasks from inqueue until task "
                      "handler finished")
        # We use a timeout to detect inqueues that was locked by a dead
        # process and therefor will never be unlocked
        assert(_is_started(task_handler))
        if inqueue._rlock.acquire(timeout=.1):
            while task_handler.is_alive() and inqueue._reader.poll():
                inqueue._reader.recv_bytes()
                sleep(0)
        else:
            mp.util.debug("inqueue is locked when terminating. "
                          "The pool might have crashed")
            while task_handler.is_alive() and inqueue._reader.poll():
                inqueue._reader.recv_bytes()
                sleep(0)
        if outqueue._rlock.acquire(timeout=.1):
            while task_handler.is_alive() and outqueue._reader.poll():
                outqueue._reader.recv_bytes()
                sleep(0)
        else:
            mp.util.debug("outqueue is locked when terminating. "
                          "The pool might have crashed")
            while task_handler.is_alive() and outqueue._reader.poll():
                outqueue._reader.recv_bytes()
                sleep(0)

    @classmethod
    def _terminate_pool(cls, taskqueue, inqueue, outqueue, pool,
                        worker_handler, task_handler, result_handler, cache):
        # this is guaranteed to only be called once
        mp.util.debug('finalizing pool')

        worker_handler._state = TERMINATE
        task_handler._state = TERMINATE

        mp.util.debug('helping task handler/workers to finish')
        # Flush both inqueue and outqueue to ensure that the task_handler
        # does not get blocked.
        cls._help_stuff_finish(inqueue, outqueue, task_handler, len(pool))

        assert result_handler.is_alive() or len(cache) == 0

        result_handler._state = TERMINATE
        outqueue.put(None)                  # sentinel

        # We must wait for the worker handler to exit before terminating
        # workers because we don't want workers to be restarted behind our back.
        mp.util.debug('joining worker handler')
        if threading.current_thread() is not worker_handler:
            worker_handler.join()

        # Terminate workers which haven't already finished.
        if pool and hasattr(pool[0], 'terminate'):
            mp.util.debug('terminating workers')
            for p in pool:
                if p.exitcode is None:
                    p.terminate()

        mp.util.debug('joining task handler')
        if threading.current_thread() is not task_handler:
            task_handler.join()

        mp.util.debug('joining result handler')
        if threading.current_thread() is not result_handler:
            result_handler.join()
        # Flag all the _cached job as terminated
        success, value = (False, TerminatedPoolError())
        for k in list(cache.keys()):
            result = cache[k]
            if hasattr(result, '_index'):
                while result._index != result._length:
                    result._set(result._index, (success, value))
            else:
                result._set(0, (success, value))

        if pool and hasattr(pool[0], 'terminate'):
            mp.util.debug('joining pool workers')
            for p in pool:
                if p.is_alive():
                    # worker has not yet exited
                    mp.util.debug('cleaning up worker %d' % p.pid)
                    p.join()


class AbortedWorkerError(Exception):
    """A worker was aborted"""
    def __init__(self, msg, exitcode):
        super(AbortedWorkerError, self).__init__()
        self.msg = msg
        self.exitcode = exitcode
        self.args = [repr(self)]

    def __repr__(self):
        format_exitcode = ''
        if self.exitcode is not None and self.exitcode < 0:
            format_exitcode = ' with signal {}'.format(-self.exitcode)
        if self.exitcode is not None and self.exitcode > 0:
            format_exitcode = ' with exitcode {}'.format(self.exitcode)
        return self.msg + format_exitcode


class _BrokenPoolError(Exception):
    """A worker was aborted"""
    def __init__(self, exitcode):
        super(_BrokenPoolError, self).__init__()
        self.exitcode = exitcode
        self.args = [repr(self)]

    def __repr__(self):
        return ('The pool is rendered unusable by a procecss that exited'
                ' with exitcode {}'.format(self.exitcode))


class TerminatedPoolError(Exception):
    """A worker was aborted"""
    def __init__(self):
        super(TerminatedPoolError, self).__init__()
        self.args = [repr(self)]

    def __repr__(self):
        return ('The pool was terminated while the task could complete')

if __name__ == '__main__':
    # This will cause a deadlock
    pool = get_reusable_pool(1)
    pid = pool.apply(os.getpid, tuple())
    pool = get_reusable_pool(2)
    os.kill(pid, 15)

    pool.terminate()