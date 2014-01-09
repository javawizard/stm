
from stm.datatypes import BroadcastQueue
import stm
from threading import Thread
import traceback

class EventLoop(object):
    def __init__(self):
        self._queue = BroadcastQueue()
        self._endpoint = self._queue.new_endpoint()
    
    def schedule(self, function):
        # In or out of STM
        if function is None:
            raise ValueError("function cannot be None")
        stm.atomically(lambda: self._queue.put(function))
    
    def stop(self):
        # In or out of STM
        stm.atomically(lambda: self._queue.put(None))
    
    def start(self):
        # Outside of STM only
        if stm._stm_state.current:
            raise Exception("This must be called outside of a transaction.")
        thread = Thread(name="stm.eventloop.EventLoop", target=self.run)
        # TODO: Should we be daemonizing here?
        thread.setDaemon(True)
        thread.start()
    
    def run(self):
        # Outside of STM only
        if stm._stm_state.current:
            raise Exception("This must be called outside of a transaction.")
        while True:
            next_event = stm.atomically(self._endpoint.get)
            if next_event is None:
                print "Event loop exiting"
                return
            try:
                next_event()
            except:
                print "Event threw an exception, which will be ignored."
                print "For reference, the exception is:"
                traceback.print_exc()


default_event_loop = stm.atomically(EventLoop)
default_event_loop.start()

def schedule(function):
    """
    Schedule a function to be run later.
    
    This can be used from within a transaction to schedule a function to be
    called outside of the transaction, after it commits. The specified function
    will be run outside of the context of a transaction, so it can do things
    like I/O that transactions normally aren't allowed to do.
    
    Note that such functions are run synchronously and in the order they were
    scheduled. They should therefore complete quickly, or spawn a new thread
    (or make use of a stm.threadutils.ThreadPool) to do their work.
    """
    default_event_loop.schedule(function)





            
