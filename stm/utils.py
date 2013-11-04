 
import stm
import functools

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
