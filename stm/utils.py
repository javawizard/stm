 
import stm
import stm.datatypes
import functools
from stm.timeout import Timeout

def atomic_function(function):
    """
    A decorator that causes calls to functions decorated with it to be
    implicitly run inside a call to stm.atomically(). Thus the following::
    
        @atomic_function
        def something(foo, bar):
            ...
    
    is equivalent to::
    
        def something(foo, bar):
            def do_something():
                ...
            return stm.atomically(do_something)
    """
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return stm.atomically(lambda: function(*args, **kwargs))
    return wrapper


def wait_until(function, timeout_after=None, timeout_at=None):
    """
    Wait until the specified function returns true. This is just short for::
    
        if not function():
            retry(timeout_after, timeout_at)
            raise stm.timeout.Timeout
    
    but the entire thing is automatically wrapped in a call to stm.atomically,
    so wait_until can be called outside of a transaction.
    
    timeout_after and timeout_at specify (in the same format as retry()'s
    resume_after and resume_at parameters) a timeout after which wait_until
    will give up and raise stm.timeout.Timeout.
    """
    @stm.atomically
    def _():
        if not function():
            stm.retry(timeout_after, timeout_at)
            raise Timeout


def changes_only(callback):
    last = stm.atomically(lambda: stm.TVar(None))
    @functools.wraps(callback)
    def actual_callback(result):
        if last.value is None or last.value.value is not result:
            last.value = stm.datatypes.TPossiblyWeakRef(result)
            callback(result)
    return actual_callback


def watcher(function):
    """
    A decorator that decorates a callback and is passed a function to watch.
    It wraps the callback with a function that, when called, registers a watch
    with stm.watch(function, callback).
    
    This is mainly useful as part of a stack of decorators registering a watch
    from outside of a transaction. For example::
    
        @stm.atomically
        @watcher(some_tvar.get)
        def _(value):
            ...do something...
    
    is equivalent to::
    
        @stm.atomically
        def _():
            def callback(value):
                ...do something...
            stm.watch(some_tvar.get, callback)
    """
    def decorator(callback):
        @functools.wraps(function)
        def wrapper():
            stm.watch(function, callback)









