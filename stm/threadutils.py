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
    """
    A transactional wrapper around Python's threading.Thread.
    
    Instances of this class can be created and started from within an STM
    transaction. Threads started in such a way will begin running shortly
    after the transaction commits.
    """
    def __init__(self, target=None):
        """
        Create a new thread.
        """
        @stm.atomically
        def _():
            stm.datatypes.TObject.__init__(self)
            self._target = target
            self._state = NEW
    
    def start(self):
        """
        Start this thread.
        
        This can be called both inside and outside of a transaction. If it's
        called from inside a transaction, the thread will begin running shortly
        after the transaction commits.
        """
        @stm.atomically
        def _():
            if self._state != NEW:
                raise RuntimeError("Threads can only be started once")
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
        """
        The state this thread is currently in. This will be one of:
        
         * NEW: This thread has been created but start() has not yet been
           called
        
         * STARTED: start() has been called but the thread has not yet
           commenced execution. Threads started from within a transaction will
           remain in STARTED until shortly after the transaction commits.
        
         * RUNNING: The thread has commenced execution.
         
         * FINISHED: The thread has died.
        """
        return stm.atomically(lambda: self._state)


class ThreadPool(stm.datatypes.TObject):
    """
    An object that schedules execution of functions across a pool of threads.
    """
    def __init__(self, max_threads, keep_alive):
        """
        Create a new ThreadPool that will use up to the specified number of
        threads and that will keep idle threads alive for the specified number
        of seconds before killing them off.
        """
        @stm.atomically
        def _():
            stm.datatypes.TObject.__init__(self)
            self._tasks = stm.datatypes.TList()
            self._max_threads = max_threads
            self._keep_alive = keep_alive
            self._live_threads = 0
            self._free_threads = 0
            self._tasks_scheduled = 0
            self._tasks_finished = 0
    
    @stm.utils.atomic_function
    def schedule(self, function):
        """
        Schedule a function to be run by this thread pool. The function will
        be executed as soon as one of this pool's threads is free.
        """
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
            Thread(target=self._thread_run).start()
    
    def join(self, timeout_after=None, timeout_at=None):
        """
        Wait until this thread pool is idle, i.e. all scheduled tasks have
        completed. Note that a call to this function may never return if enough
        calls to schedule() are being made to keep the thread pool saturated
        with tasks to run.
        
        timeout_after and timeout_at specify a timeout (in the same format as
        that given to stm.utils.wait_until) after which join() will give up
        and raise stm.timeout.Timeout.
        """
        stm.utils.wait_until(lambda: self.tasks_remaining == 0, timeout_after, timeout_at)
    
    @property
    def tasks_scheduled(self):
        return self._tasks_scheduled
    
    @property
    def tasks_finished(self):
        return self._tasks_finished
    
    @property
    def tasks_remaining(self):
        return self._tasks_scheduled - self._tasks_finished
    
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












