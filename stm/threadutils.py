"""
Transactional threads and thread pools.

More documentation to come.
"""

import threading
import stm
import stm.datatypes
import stm.eventloop
import stm.utils
import traceback

# Thread has been created but start() hasn't been called
NEW = "stm.threadutils.NEW"
# start() has been called but the event scheduler hasn't actually spun up the
# thread yet (threads started inside a transaction will stay in STARTED until
# shortly after the transaction commits)
STARTED = "stm.threadutils.STARTED"
# Thread is actually running
RUNNING = "stm.threadutils.RUNNING"
# Thread has died
FINISHED = "stm.threadutils.FINISHED"

class Thread(stm.datatypes.TObject):
    def __init__(self, target=None):
        @stm.atomically
        def _():
            stm.datatypes.TObject.__init__(self)
            self._target = target
            self._state = NEW
    
    def start(self):
        @stm.atomically
        def _():
            stm.eventloop.schedule(self._start_thread)
            self._state = STARTED
    
    def run(self):
        target = stm.atomically(lambda: self._target)
        target()
    
    def _start_thread(self):
        threading.Thread(target=self._run_thread).start()
    
    def _run_thread(self):
        @stm.atomically
        def _():
            self._state = RUNNING
        try:
            self.run()
        finally:
            @stm.atomically
            def _():
                self._state = FINISHED
    
    @property
    def state(self):
        return stm.atomically(lambda: self._state)


class ThreadPool(stm.datatypes.TObject):
    def __init__(self, threads, keep_alive):
        @stm.atomically
        def _():
            stm.datatypes.TObject.__init__(self)
            self._tasks = stm.datatypes.TList()
            self._max_threads = threads
            self._keep_alive = keep_alive
            self._live_threads = 0
            self._free_threads = 0
            self._tasks_scheduled = 0
            self._tasks_finished = 0
    
    def schedule(self, function):
        @stm.atomically
        def need_new_thread():
            # Schedule the task to be run
            self._tasks.insert(0, function)
            self._tasks_scheduled += 1
            # See if we should start a new thread
            if self._free_threads == 0 and self._live_threads < self._max_threads:
                # No free threads to handle this task and we haven't maxed out
                # the number of threads we can start, so increment our live
                # and free thread counters and start a new thread.
                self._live_threads += 1
                self._free_threads += 1
                return True
            else:
                return False
        # Do the actual thread starting if we're supposed to start a new thread
        if need_new_thread:
            Thread(target=self._thread_run).start()
    
    def join(self, timeout_after=None, timeout_at=None):
        stm.utils.wait_until(lambda: self._tasks_scheduled == self._tasks_finished, timeout_after, timeout_at)
    
    def _thread_run(self):
        while True:
            @stm.atomically
            def task():
                # See if we have any tasks to run
                if self._tasks:
                    # We do. Mark this thread as no longer free, then return
                    # the task.
                    self._free_threads -= 1
                    return self._tasks.pop()
                # No tasks yet, so wait the maximum amount of time we're
                # supposed to stay alive without any tasks to run.
                stm.retry(self._keep_alive)
                # It's been self._keep_alive seconds and we haven't had any
                # tasks to run, so decrement the number of live and free
                # threads and then die.
                self._free_threads -= 1
                self._live_threads -= 1
                return None
            # Do the actual dying if we've been idle too long
            if not task:
                return
            # We got a task to run. Run it.
            try:
                task()
            except:
                traceback.print_exc()
            # We're done running the task, so mark ourselves as free again.
            @stm.atomically
            def _():
                self._free_threads += 1
                self._tasks_finished += 1












