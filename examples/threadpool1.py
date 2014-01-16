
from stm.threadutils import ThreadPool
from stm.utils import atomically_watch, changes_only_according_to
from stm.eventloop import scheduled_function
import random
import operator
import time

pool = ThreadPool(5, 3)

@atomically_watch(lambda: (pool.tasks_scheduled, pool.tasks_finished))
@changes_only_according_to(operator.eq)
@scheduled_function
def _((scheduled, finished)):
    print "{0} scheduled, {1} finished".format(scheduled, finished)

def task():
    time.sleep(1)

for _ in range(40):
    pool.schedule(task)

pool.join()

# Sleep a bit to let the eventloop process our final progress message. This
# won't be necessary once I add an atexit hook to stm.eventloop to wait until
# all events have been processed before shutting down.
# 
# Also, TODO: Figure out why ThreadPool's keep_alive isn't keeping threads
# (and thus this script) alive for 3 seconds after we join...
time.sleep(0.5)
