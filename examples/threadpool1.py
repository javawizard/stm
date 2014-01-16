
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
    time.sleep((random.random()) + 0.3)

for _ in range(40):
    pool.schedule(task)
    time.sleep(0.03)

pool.join()
