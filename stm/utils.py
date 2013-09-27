 
import stm
import functools

def atomic_function(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return stm.atomically(lambda: function(*args, **kwargs))
    return wrapper
