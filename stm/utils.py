 
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
    """
    A decorator that can be used to decorate callbacks that are to be passed to
    stm.watch. It wraps the callback with a function that only invokes the
    callback if the value passed to it is different from that passed to the
    last invocation of the callback.
    
    Note that the wrapper function attempts to store a weak reference to the
    last result passed into the callback but falls back to holding a strong
    reference if the value in question is of a type that does not support
    weak references (strings, for example).
    """
    last = stm.atomically(lambda: stm.TVar(None))
    @functools.wraps(callback)
    def actual_callback(result):
        if last.value is None or last.value.value is not result:
            last.value = stm.datatypes.TPossiblyWeakRef(result)
            callback(result)
    return actual_callback


def atomically_watch(function, callback=None):
    """
    A wrapper around stm.watch that automatically runs the call inside a
    transaction. This is essentially equivalent to::
    
        stm.atomically(lambda: stm.watch(function, callback))
    
    but, as with stm.watch, atomically_watch can be used as a decorator by
    omitting the callback parameter. For example, the following could be used
    outside of a transaction to place a new watch::
    
        @atomically_watch(some_tvar.get)
        def _(result):
            ...do something...
    
    This would be equivalent to:
    
        @stm.atomically
        def _():
            @stm.watch(some_tvar.get)
            def _(result):
                ..do something..
    
    Note that the callback will (as callbacks always are) still be run inside
    a transaction. If you need to perform I/O in the callback, use
    stm.eventloop.scheduled_function to decorate the callback such that it will
    be run by the event loop outside of the scope of STM::
    
        @atomically_watch(some_tvar.get)
        @stm.eventloop.scheduled_function
        def _(result):
            print "Changed to " + str(result) # Or any other I/O
        
    """
    if callback is None:
        def decorator(actual_callback):
            atomically_watch(function, actual_callback)
        return decorator
    stm.atomically(lambda: stm.watch(function, callback))









