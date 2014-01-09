"""
Transactional threads and thread pools.

More documentation to come.
"""

import stm
import stm.datatypes
import stm.eventloop

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
        if target:
            self.run = target
        self._state = NEW
    
    def start(self):
        @stm.atomically
        def _():
            stm.eventloop.schedule(self._start_thread)
            self._state = STARTED
    
    def run(self):
        raise Exception("No target specified and run wasn't overridden.")
    
    def _start_thread(self):
        Thread(target=self._run_thread).start()
    
    def _run_thread(self):
        @stm.atomically
        def target():
            self._state = RUNNING
            return self.run
        try:
            target()
        finally:
            @stm.atomically
            def _():
                self._state = FINISHED
    
    @property
    def state(self):
        return self._state










