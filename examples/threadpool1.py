
from stm.threadutils import ThreadPool
from stm.utils import atomically_watch, changes_only
from stm.eventloop import scheduled_function
import stm
import random
import operator
import time

pool = ThreadPool(300, 1)

@atomically_watch(lambda: (pool.tasks_scheduled, pool.tasks_finished, pool._live_threads, len(pool._free_threads), len(pool._tasks)))
@changes_only(according_to=operator.eq)
@scheduled_function
def _((scheduled, finished, live, free, tasks)):
    print "{0} scheduled, {1} finished, {2} threads live, {3} free, {4} tasks scheduled".format(scheduled, finished, live, free, tasks)

def task():
    time.sleep(1)

for _ in range(1000):
    pool.schedule(task)

pool.join()

time.sleep(15)

# TODO: Figure out why ThreadPool's keep_alive isn't keeping threads
# (and thus this script) alive for 3 seconds after we join...
