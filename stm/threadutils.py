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
import operator

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
            self._tasks_scheduled = 0
            self._tasks_finished = 0
            # Hack until I write a proper TSet datatype
            self._free_threads = stm.datatypes.TDict()
            stm.watch(self._calculate_threads_short, self._spin_up_threads)
            stm.watch(self._check_tasks_ready, self._allocate_tasks)
    
    def _calculate_threads_short(self):
        """
        Calculate and return the number of threads that we would need to spin
        up given our current circumstances in order to have one free thread for
        every scheduled task without going over the limit.
        """
        return min(len(self._tasks) - len(self._free_threads), self._max_threads - self._live_threads)
    
    @stm.utils.changes_only(according_to=operator.eq)
    def _spin_up_threads(self, number_of_threads):
        """
        Spin up the specified number of threads.
        """
        for _ in range(number_of_threads):
            self._live_threads += 1
            thread = _ThreadPoolWorker(self)
            thread.start()
            self._free_threads[thread] = None
    
    def _check_tasks_ready(self):
        """
        See if we have both tasks to run and free threads to run them.
        """
        return bool(self._tasks) and bool(self._free_threads)
    
    @stm.utils.changes_only(according_to=operator.eq)
    def _allocate_tasks(self, _):
        """
        Pop pairs of tasks to be allocated and free threads to allocate them to
        and tell each of the threads to run each of the tasks.
        """
        while self._check_tasks_ready():
            task = self._tasks.pop()
            thread = self._free_threads.popitem()[0]
            thread.current_task = task
    
    @stm.utils.atomic_function
    def schedule(self, function):
        """
        Schedule a function to be run by this thread pool. The function will
        be executed as soon as one of this pool's threads is free.
        """
        # Schedule the task to be run
        self._tasks.insert(0, function)
        self._tasks_scheduled += 1

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


class _ThreadPoolWorker(Thread):
    # NOTE: We depend on the fact that Thread subclasses TObject. Might be a
    # better idea to explicitly subclass TObject ourselves, init it, and then
    # have TObject watch for the case where its __init__ is called twice.
    # (Or I could use super(), but I'm on an anti-super kick right now due to
    # the fact that object.__init__ gets mad if you give it any arguments...)
    def __init__(self, pool):
        Thread.__init__(self)
        self.pool = pool
        self.current_task = None
    
    def run(self):
        while True:
            @stm.atomically
            def task():
                # See if we have any tasks to run.
                if self.current_task:
                    # We do. Mark ourselves as no longer free.
                    return self.current_task
                # No tasks yet, so see if we've been idle for more than
                # self._keep_alive seconds.
                if stm.elapsed(self.pool._keep_alive):
                    # We have, so decrement the number of free and live threads
                    # and then die.
                    self.pool._live_threads -= 1
                    # Note that we don't access _free_threads until when we're
                    # actually going to die, so we won't ever resume from
                    # retrying just because another thread became free.
                    del self.pool._free_threads[self]
                    return None
                # We haven't, so retry.
                stm.retry()
            # Do the actual dying if we've been idle too long
            if not task:
                return
            # We got a task to run. Run it.
            try:
                task()
            except:
                traceback.print_exc()
            # We're done running the task, so increment the number of tasks
            # that have been completed and mark ourselves as free.
            @stm.atomically
            def _():
                self.pool._tasks_finished += 1
                self.pool._free_threads[self] = None
                self.current_task = None












