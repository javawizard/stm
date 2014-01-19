 
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
    
        if function():
            return
        elif elapsed(timeout_after, timeout_at):
            raise stm.timeout.Timeout
        else:
            retry()
    
    but the entire thing is automatically wrapped in a call to stm.atomically,
    so wait_until can be called outside of a transaction.
    
    timeout_after and timeout_at specify (in the same format as elapsed()'s
    seconds and time parameters) a timeout after which wait_until will give up
    and raise stm.timeout.Timeout.
    """
    @stm.atomically
    def _():
        if function():
            return
        elif stm.elapsed(timeout_after, timeout_at):
            raise Timeout
        else:
            stm.retry()


def would_block(function):
    """
    Run the specified function in a nested transaction, abort the nested
    transaction to avoid any side effects being persisted, then return True if
    the function attempted to retry.
    """
    try:
        # Create a function to pass to or_else that runs the function, then
        # raises an exception to abort the nested transaction.
        def run_and_raise():
            function()
            # We need to abort this nested transaction to avoid the function's
            # side effects being persisted, and we don't differentiate between
            # a function that runs successfully and a function that raises an
            # exception (neither would retry), so just raise Exception here.
            raise Exception()
        # The call to stm.atomically isn't strictly necessary right now, but
        # I'm considering changing or_else to not revert the effects of a
        # function that ends up throwing an exception, in which case it would
        # be necessary.
        stm.atomically(lambda: stm.or_else(run_and_raise, lambda: None))
        # Function tried to retry
        return True
    except Exception:
        # Function didn't retry (either it threw an exception itself or it
        # succeeded, in which case we threw the exception for it)
        return False


def changes_only(callback=None, according_to=None):
    """
    A decorator that can be used to decorate callbacks that are to be passed to
    stm.watch to filter out duplicate invocations with the same result value.
    It can be used either as::
    
        @changes_only
        def callback(result):
            ...
    
    or as::
    
        @changes_only(according_to=some_predicate)
        def callback(result):
            ...
    
    with the latter allowing a custom two-argument function to be used to
    compare the equality of the value passed to a given invocation with the
    value passed to the previous invocation; the former compares values using
    the "is" operator.
    
    Note that the resulting callback will keep a reference around to the last
    value with which it was called, so make sure you're not counting on this
    value's being garbage collected immediately after the callback is invoked.
    """
    if callback and according_to:
        last = stm.atomically(lambda: stm.TVar((False, None)))
        @functools.wraps(callback)
        def actual_callback(*args):
            # We use *args here to permit @changes_only to be used to decorate
            # methods on objects; in such cases, we'll be passed two arguments,
            # self and the actual result.
            result = args[-1]
            has_run, last_value = last.value
            if not has_run or not according_to(last_value, result):
                last.value = True, result
                callback(*args)
        return actual_callback
    elif callback:
        return changes_only(callback, operator.is_)
    elif according_to:
        def decorator(callback):
            return changes_only(callback, according_to)
        return decorator
    else:
        raise ValueError("Either callback or according_to must be specified")


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










