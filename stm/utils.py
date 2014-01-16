 
import stm
import stm.datatypes
import functools
import operator
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
    stm.watch to filter out duplicate invocations with the same result value.
    
    Note that the resulting callback will keep a reference around to the last
    value with which it was called, so make sure you're not counting on this
    value's being garbage collected immediately after the callback is invoked.
    
    By default, the "is" operator is used to compare each value with the one
    passed in previously. If you need another operator (for example,
    operator.eq) to be used, have a look at changes_only_according_to().
    """
    return changes_only_according_to(operator.is_)(callback)


def changes_only_according_to(predicate):
    """
    A variant of changes_only that allows specifying the function used to test
    values for equality.
    
    By default, changes_only uses identity-wise comparison (i.e. the "is"
    operator) to compare values. This function can be used to indicate an
    alternative comparison predicate; operator.eq (or lambda a, b: a == b)
    could be used to test for equality with Python's == operator.
    """
    def decorator(callback):
        last = stm.atomically(lambda: stm.TVar((False, None)))
        @functools.wraps(callback)
        def actual_callback(result):
            has_run, last_value = last.value
            if not has_run or not predicate(last_value, result):
                last.value = True, result
                callback(result)
        return actual_callback
    return decorator


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









