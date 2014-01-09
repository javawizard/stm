 
import stm
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










