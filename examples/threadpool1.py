
from stm.threadutils import ThreadPool
from stm.utils import atomically_watch, changes_only_according_to
from stm.eventloop import scheduled_function
import random
import operator
import time

pool = ThreadPool(5, 3)

@atomically_watch(lambda: (pool.tasks_scheduled, pool.tasks_finished, pool._live_threads, pool._free_threads))
@changes_only_according_to(operator.eq)
@scheduled_function
def _((scheduled, finished, live, free)):
    print "{0} scheduled, {1} finished, {2} live, {3} free".format(scheduled, finished, live, free)

def task():
    time.sleep(random.random() + 0.5)

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
