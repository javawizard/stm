"""
A pure-Python software transactional memory system.

This module provides a software transactional memory system for Python. It
provides full support for isolated transactions, blocking, timed blocking, and
transactional invariants.

Have a look at the documentation for the atomically() function and the TVar
class. Those form the core building blocks of the STM system.
"""

from threading import local as _Local, Lock as _Lock, Thread as _Thread
from threading import Event as _Event
import weakref as weakref_module
from contextlib import contextmanager
import time

__all__ = ["TVar", "TWeakRef", "atomically", "retry", "or_else", "invariant",
           "previously"]


class _Restart(BaseException):
    """
    Raised when a transaction needs to restart. This happens when an attempt is
    made to read a variable that has been modified since the transaction
    started. It also happens just after the transaction has finished blocking
    in response to a _Retry.
    """
    pass

class _Retry(BaseException):
    """
    Raised when a transaction should retry at some later point, when at least
    one of the variables it accessed has been modified. This happens when
    retry() is called, and causes the toplevel transaction to block until one
    of the variables accessed in this transaction has been modified; the
    toplevel transaction then converts this into a _Restart.
    """
    pass


class _State(_Local):
    """
    A thread local holding the thread's current transaction
    """
    def __init__(self):
        self.current = None
    
    def get_current(self):
        """
        Returns the current transaction, or raises an exception if there is no
        current transaction.
        """
        if not self.current:
            raise Exception("No current transaction. The function you're "
                            "calling most likely needs to be wrapped in a "
                            "call to stm.atomically().")
        return self.current
    
    def get_base(self):
        return self.get_current().get_base_transaction()
    
    @contextmanager
    def with_current(self, transaction):
        old = self.current
        self.current = transaction
        try:
            yield
        finally:
            self.current = old

_stm_state = _State()
# Lock that we lock on while committing transactions
_global_lock = _Lock()
# Number of the last transaction to successfully commit. The first transaction
# run will change this to the number 1, the second transaction to the number
# 2, and so on.
_last_transaction = 0


class _Timer(_Thread):
    """
    A timer similar to threading.Timer but with a few differences:
    
     * This class waits significantly longer (0.3 seconds at present) between
       checks to see if we've been canceled before we're actually supposed to
       time out. This isn't visible to transactions themselves (they always
       respond instantly to any changes that could make them complete sooner),
       but it 1: saves us a decent bit of CPU time, but 2: means that if a
       transaction resumes for a reason other than because it timed out, the
       timeout thread will hang around for up to half a second before dying.
       
     * This class accepts timeouts specified in terms of the wall clock time
       (in terms of time.time()) at which the timer should go off rather than
       the number of seconds after which they should resume.
       
     * This class accepts a retry event to notify instead of a function to
       call, mainly to avoid one layer of unnecessary indirection.
    
    The whole reason we're busywaiting here is because Python [versions earlier
    than 3.n, where n is a number I don't remember at the moment] don't expose
    any platform independent way of genuinely waiting with a timeout on a
    condition. I'm considering rewriting this when I get time to use an
    anonymous pipe and a call to select(), which /would/ allow this sort of
    proper blocking timeout on all platforms except Windows (and hey, who
    cares about Windows anyway?).
    """
    def __init__(self, event, resume_at):
        _Thread.__init__(self, name="Timeout thread expiring at %s" % resume_at)
        self.event = event
        self.resume_at = resume_at
        self.cancel_event = _Event()
    
    def start(self):
        # Only start the thread if we've been given a non-None timeout
        # (allowing resume_at to be None and treating it as an infinite timeout
        #  makes the retry-related logic in _BaseTransaction simpler)
        if self.resume_at is not None:
            _Thread.start(self)
    
    def run(self):
        while True:
            # Check to see if we've been asked to cancel
            if self.cancel_event.is_set():
                # We got a cancel request, so return.
                return
            time_to_sleep = min(0.3, self.resume_at - time.time())
            if time_to_sleep <= 0:
                # Timeout's up! Notify the event, then return.
                self.event.set()
                return
            # Timeout's not up. Sleep for the specified amount of time.
            time.sleep(time_to_sleep)
    
    def cancel(self):
        self.cancel_event.set()


class _Transaction(object):
    """
    An abstract class for transactions that provides functionality to track the
    values of variables read and written during this transaction. The subclass
    is responsible for providing the values of variables which are not yet
    known and for running functions within the scope of this transaction.
    """
    def __init__(self):
        # Maps vars that we know about to their values
        self.var_cache = {}
        # Maps vars to watchers watching them
        self.watchers_cache = {}
        # Maps watchers to vars they read during their last run
        self.watched_vars_cache = {}
        # Vars that were read before they were written, or never written (i.e.
        # not vars that were written first). These are the vars for which
        # load_real_value was called. This set also includes mature TWeakRefs
        # that were read during this transaction.
        self.read_set = set()
        # Vars that have been written at any point during this transaction.
        # These are the vars for which load_threatened_invariants was called.
        self.write_set = set()
        # Sets of vars whose _watchers have been read/written
        self.watchers_changed_set = set()
        # Sets of watchers whose watched_vars have been read/written
        self.watched_vars_changed_set = set()
        self.proposed_watchers = []
    
    def values_to_check_for_cleanliness(self):
        return (self.read_set | self.write_set |
            self.watchers_changed_set | self.watched_vars_changed_set)
    
    def load_value(self, var):
        """
        Returns the real value of the specified variable, possibly throwing
        _Restart if the variable has been modified since this transaction
        started. This will only be called once for any given var in any given
        transaction; the value will thereafter be stored in self.var_cache.
        This must be overridden by the subclass.
        """
        raise NotImplementedError
    
    def load_watchers(self, var):
        # Note that var can be a TVar or a TWeakRefs
        raise NotImplementedError
    
    def load_watched_vars(self, watcher):
        raise NotImplementedError
    
    def run(self, function):
        """
        Runs the specified function in the context of this transaction,
        committing changes as needed once finished. This must be overridden by
        the subclass.
        """
        raise NotImplementedError
    
    def get_value(self, var):
        """
        TODO: Update documentation
        
        Looks up the value of the specified variable in self.var_cache and
        returns it, or calls self.get_real_value(var) (and then stores it in
        self.var_cache) if the specified variable is not in self.var_cache.
        This is a concrete function; subclasses need not override it.
        """
        # Note: We intentionally don't add the var to the read set if we wrote
        # it before we ever read it, as its value before the transaction
        # happened doesn't matter as far as retries etc. go. (Note that we
        # still validate its version clock when committing.)
        # Also note that we check for the var's presence in self.var_cache
        # instead of in read_set as there are situations where the var can end
        # up in read_set without having a corresponding entry in self.var_cache
        # (such as when it's read from within a function passed to
        # previously()).
        try:
            return self.var_cache[var]
        except KeyError:
            self.read_set.add(var)
            value = self.load_value(var)
            self.var_cache[var] = value
            return value
    
    def set_value(self, var, value):
        """
        Sets the entry in self.var_cache for the specified variable to the
        specified value.
        """
        self.var_cache[var] = value
        self.write_set.add(var)
    
    def get_watchers(self, var):
        try:
            return self.watchers_cache[var]
        except KeyError:
            # TODO: Do we need to add this var to watchers_changed_set if we're
            # just reading it? We might, if just for the sake of validating
            # that the var's watchers haven't changed since the transaction
            # started, although we might only need to worry about this for vars
            # in the write set...
            self.watchers_changed_set.add(var)
            w = self.load_watchers(var)
            self.watchers_cache[var] = w
            return w
    
    def set_watchers(self, var, watchers):
        self.watchers_changed_set.add(var)
        self.watchers_cache[var] = watchers
    
    def get_watched_vars(self, watcher):
        try:
            return self.watched_vars_cache[watcher]
        except KeyError:
            self.watched_vars_changed_set.add(watcher)
            v = self.load_watched_vars(watcher)
            self.watched_vars_cache[watcher] = v
            return v
    
    def set_watched_vars(self, watcher, vars):
        self.watched_vars_changed_set.add(watcher)
        self.watched_vars_cache[watcher] = vars
    
    def make_previously(self):
        """
        Returns a new transaction reflecting the state of this transaction
        just before it started.
        
        BaseTransaction will return another BaseTransaction with the same
        start attribute, which will cause it to throw _Restart if anything's
        changed. NestedTransaction will most likely just return another
        NestedTransaction with the same parent.
        
        (Note that this new transaction can only be used until self is
        committed, and the new transaction should not itself be committed.)
        
        This is used mainly for implementing the previously() function; it's
        implemented by calling this function to obtain a previous transaction,
        then running the function passed to it in the context of that
        transaction and then aborting the transaction.
        """
        raise NotImplementedError


class _BaseTransaction(_Transaction):
    """
    A toplevel transaction. This class takes care of committing values to the
    actual variables' values (and synchronizing on the global lock while doing
    so), blocking until vars are modified when a _Retry is caught, and so
    forth.
    """
    def __init__(self, overall_start_time, current_start_time, start=None):
        _Transaction.__init__(self)
        self.parent = None
        self.overall_start_time = overall_start_time
        self.current_start_time = current_start_time
        self.next_start_time = current_start_time
        self.created_weakrefs = set()
        self.live_weakrefs = set()
        self.resume_at = None
        # Store off the transaction id we're starting at, so that we know if
        # things have changed since we started.
        if not start:
            # TODO: Do we need to lock here? Probably, because if we're running
            # on, say, Jython and we don't, we might see a slightly outdated
            # value here... But I need to think about that and make sure that
            # that could really screw up the STM system that badly as opposed
            # to, say, just causing a transaction to be needlessly re-run.
            with _global_lock:
                start = _last_transaction
        self.start = start
    
    def get_base_transaction(self):
        return self
    
    def load_value(self, var):
        # Just check to make sure the variable hasn't been modified since we
        # started (and raise _Restart if it has), then return its real value.
        with _global_lock:
            var._check_clean(self)
            return var._real_value
    
    def load_watchers(self, var):
        # Note that var can be a TVar or a TWeakRef
        with _global_lock:
            var._check_clean(self)
            # Duplicate the set so that later modifications to the original
            # don't screw us up. TODO: Consider using a WeakSet here
            return set(var._watchers)
    
    def load_watched_vars(self, watcher):
        with _global_lock:
            watcher._check_clean(self)
            return set(watcher.watched_vars)
    
    def run(self, function):
        try:
            # First we actually run the transaction.
            result = function()
            # Transaction appears to have run successfully, so commit it.
            self.commit()
            # And we're done!
            return result
        except _Retry:
            # The transaction called retry(). Handle accordingly.
            self.retry_block()
    
    def commit(self):
        global _last_transaction
        
        watchers_to_run = set()
        for var in self.write_set:
            watchers_to_run.update(self.get_watchers(var))
        # Run all of the proposals here, and blank out the list of
        # proposals since we're turning them into entries on
        # watchers_changed and watched_vars_changed
        watchers_to_run.update(set(self.proposed_watchers))
        self.proposed_watchers = []

        new_watchers_to_run = set()
        while watchers_to_run:
            for watcher in watchers_to_run:
                formerly_watched_vars = self.get_watched_vars(watcher)
                watcher_transaction = _NestedTransaction(self)
                with _stm_state.with_current(watcher_transaction):
                    result = watcher.function()
                newly_watched_vars = watcher_transaction.read_set
                self.set_watched_vars(watcher, newly_watched_vars)
                for formerly_watched_var in formerly_watched_vars - newly_watched_vars:
                    # TODO: See if we can avoid the constant set cloning
                    self.set_watchers(formerly_watched_var, self.get_watchers(formerly_watched_var) - set([watcher]))
                for newly_watched_var in newly_watched_vars - formerly_watched_vars:
                    self.set_watchers(newly_watched_var, self.get_watchers(newly_watched_var) | set([watcher]))
                callback_transaction = _NestedTransaction(self)
                with _stm_state.with_current(callback_transaction):
                    watcher.callback(result)
                callback_transaction.commit()
                for var in callback_transaction.write_set:
                    new_watchers_to_run.update(self.get_watchers(var))
            watchers_to_run = new_watchers_to_run
            # Copy in any new proposals made during callback runs
            watchers_to_run.update(set(self.proposed_watchers))
            self.proposed_watchers = []
        
        with _global_lock:
            # Now we make sure nothing we read or modified changed since this
            # transaction started. This will take care of making sure that
            # none of our TWeakRefs that we read as alive have since been
            # garbage collected.
            for item in self.values_to_check_for_cleanliness():
                item._check_clean(self)
            # Nothing changed, so we're good to commit. First we make
            # ourselves a new id.
            _last_transaction += 1
            modified = _last_transaction
            # Then we update the real values of all of the TVars in the write
            # set.
            for var in self.write_set:
                var._update_real_value(self.get_value(var))
                var._modified = modified
            
            for var in self.watchers_changed_set:
                var._watchers = self.get_watchers(var)
                var._modified = modified
            
            for watcher in self.watched_vars_changed_set:
                # Make them equal without creating a new WeakSet
                watcher.watched_vars.intersection_update(self.get_watched_vars(watcher))
                watcher.watched_vars.update(self.get_watched_vars(watcher))
                watcher._modified = modified
            
            # And then we tell all TWeakRefs created during this
            # transaction to mature
            for ref in self.created_weakrefs:
                ref._make_mature()
    
    def retry_block(self):
        # Received a retry request that made it all the way up to the top.
        # First, check to see if any of the variables we've accessed have
        # been modified since we started; if they have, we need to restart
        # instead. TODO: Do we really need to check the write set here? We're
        # not modifying anything, just waiting for something in the read set to
        # change, so methinks we don't need to...
        # TODO: We also ought to consider wrapping the read set with a
        # WeakSet here (or maybe making read_set a WeakSet to begin with) to
        # avoid preventing vars from being garbage collected just because we're
        # waiting for them to change, although that might not be necessary as
        # (I think) we'll already be notified by whatever var or ref we
        # accessed the var in question through...
        with _global_lock:
            for item in self.values_to_check_for_cleanliness():
                item._check_clean(self)
            # Nope, none of them have changed. So now we create an event,
            # then add it to all of the vars we've read.
            e = _Event()
            for item in self.read_set:
                item._add_retry_event(e)
        # Then we create a timer to let us know when our retry timeout (if any
        # calls made during this transaction indicated one) is up. Note that
        # _Timer does nothing when given a resume time of None, so we don't
        # need to worry about that here.
        timer = _Timer(e, self.resume_at)
        timer.start()
        # Then we wait.
        e.wait()
        # One of the vars was modified or our timeout expired. Now we go cancel
        # the timer (in case it was a change to one of our watched vars that
        # woke us up instead of a timeout) and remove ourselves from the vars'
        # events.
        timer.cancel()
        with _global_lock:
            for item in self.read_set:
                item._remove_retry_event(e)
        # Then we compute the current_start_time the next transaction attempt
        # should see. If we didn't have a resume_at specified (i.e. we blocked
        # solely on changes to TVars etc. we'd read), we use the current time;
        # if we did, we use the lesser of the indicated resume_at and the
        # current time (in case we resumed earlier than requested due to TVar
        # etc. changes).
        if self.resume_at is None:
            self.next_start_time = time.time()
        else:
            self.next_start_time = min(time.time(), self.resume_at)
        # And then we restart.
        raise _Restart
    
    def make_previously(self):
        return _BaseTransaction(self.overall_start_time, self.current_start_time, self.start)
    
    def update_resume_at(self, resume_at):
        if self.resume_at is None:
            # First timed retry request of the transaction, so just store its
            # requested resume time.
            self.resume_at = resume_at
        else:
            # Second or later timed retry request of this transaction (the
            # previous ones were presumably intercepted by or_else), so see
            # which one wants us to resume sooner and resume then.
            self.resume_at = min(self.resume_at, resume_at)


class _NestedTransaction(_Transaction):
    """
    A nested transaction. This just wraps another transaction and persists
    changes to it upon committing unless the function to run throws an
    exception (of any sort, including _Retry and _Restart).
    """
    def __init__(self, parent):
        _Transaction.__init__(self)
        self.parent = parent
    
    def get_base_transaction(self):
        return self.parent.get_base_transaction()
    
    def load_value(self, var):
        # Just get the value from our parent.
        return self.parent.get_value(var)
    
    def load_watchers(self, var):
        return self.parent.get_watchers(var)
    
    def load_watched_vars(self, watcher):
        return self.parent.get_watched_vars(watcher)
    
    def run(self, function):
        # Run the function, then (if it didn't throw any exceptions; _Restart,
        # _Retry, or otherwise) copy our values into our parent.
        result = function()
        self.commit()
        return result
    
    def commit(self):
        for var in self.write_set:
            self.parent.set_value(var, self.var_cache[var])
        
        # TODO: Extract code that runs watchers into a separate function on
        # _Transaction, then call it from here. Then we won't need to copy
        # self.proposed_watchers to self.parent.
        self.parent.proposed_watchers.extend(self.proposed_watchers)
        for var in self.watchers_changed_set:
            self.parent.set_watchers(var, self.get_watchers(var))
        for watcher in self.watched_vars_changed_set:
            self.parent.set_watched_vars(watcher, self.get_watched_vars(watcher))
    
    def make_previously(self):
        return _NestedTransaction(self.parent)


class _Watcher(object):
    """
    A transactional invariant.
    
    These are objects created during commit time for every invariant proposed
    with stm.invariant. They store the invariant's actual function and a list
    of TVars that the invariant accessed during its last successful run.
    
    (They don't currently store references to TWeakRefs accessed during the
    last run. I'll be changing this soon.)
    """
    def __init__(self, function, callback):
        # The watcher function itself
        self.function = function
        # The callback to invoke with the function's result
        self.callback = callback
        self._modified = 0
        # The set of TVars and TWeakRefs that the watcher is watching, i.e. the
        # vars that it accessed on its last run in the last transaction in
        # which it was run.
        # We don't need to keep around vars that can't be referenced by
        # anything else: if they're garbage collected, then the tracked
        # function itself wouldn't have been able to access them during its
        # next run, so we don't care about them.
        self.watched_vars = weakref_module.WeakSet()

    def _check_clean(self, transaction):
        if self._modified > transaction.start:
            raise _Restart


class TVar(object):
    """
    A transactional variable.
    
    TVars are the main primitives used within the STM system. They hold a
    reference to a single value. They can only be read or written from within a
    call to atomically().
    
    More complex datatypes (such as TList, TDict, and TObject) are available in
    stm.datatypes.
    """
    __slots__ = ["_events", "_real_value", "_modified", "_watchers",
                 "__weakref__"]
    
    def __init__(self, value=None):
        """
        Create a TVar with the specified initial value.
        """
        # TODO: Sets are expensive things (~232 bytes), much more expensive
        # than TVars (~88 bytes). We should set self._events and self._watchers
        # to None whenever they don't contain anything to save on space.
        self._events = set()
        self._real_value = value
        self._modified = 0
        # Set of _Watcher objects that accessed this TVar during their last
        # run, and that therefore need to be re-run when this TVar is modified
        self._watchers = set()
    
    def get(self):
        """
        Return the current value of this TVar.
        
        This can only be called from within a call to atomically(). An
        exception will be thrown if this method is called elsewhere.
        """
        # Ask the current transaction for our value.
        return _stm_state.get_current().get_value(self)
    
    def set(self, value):
        """
        Set the value of this TVar to the specified value.
        
        This can only be called from within a call to atomically(). An
        exception will be thrown if this method is called elsewhere.
        """
        # Set the specified value into the current transaction.
        _stm_state.get_current().set_value(self, value)
    
    value = property(get, set, doc="A property wrapper around self.get and self.set.")
    
    def _check_clean(self, transaction):
        # Check to see if our underlying value has been modified since the
        # start of this transaction, which should be a BaseTransaction
        if self._modified > transaction.start:
            # It has, so restart the transaction.
            raise _Restart
    
    def _add_retry_event(self, e):
        self._events.add(e)
    
    def _remove_retry_event(self, e):
        self._events.remove(e)
    
    def _update_real_value(self, value):
        # NOTE: This is always called while the global lock is acquired
        # Update our real value.
        self._real_value = value
        # Then notify all of the events registered to us.
        for e in self._events:
            e.set()


class TWeakRef(object):
    """
    A transactional weak reference with a simple guarantee: the state of a
    given weak reference (i.e. whether or not it's been garbage collected yet)
    remains the same over the course of a given transaction. More specifically,
    if a TWeakRef's referent is garbage collected in the middle of a
    transaction that previously read the reference as alive, the transaction
    will be immediately restarted.
    
    A callback function may be specified when creating a TWeakRef; this
    function will be called in its own transaction when the value referred to
    by the TWeakRef is garbage collected, if the TWeakRef itself is still
    alive. Note that the callback function will only be called if the
    transaction in which this TWeakRef is created commits successfully.
    
    TWeakRefs are fully compatible with the retry() function; that is, a
    function such as the following works as expected, and blocks until the
    TWeakRef's referent has been garbage collected::
    
        def block_until_garbage_collected(some_weak_ref):
            if some_weak_ref.get() is not None:
                retry()
    
    TWeakRefs are not mutable. If mutable weak references are desired, see
    stm.datatypes.TMutableWeakRef.
    """
    def __init__(self, value, callback=None):
        """
        Create a new weak reference pointing to the specified value.
        """
        # TODO: Same note as on TVar._events about replacing the sets with None
        # when they don't contain anything. We should also consider setting a
        # __slots__ on TWeakRef.
        self._events = set()
        self._mature = False
        self._weak_ref = weakref_module.ref(value, self._on_value_dead)
        self._strong_ref = value
        self._watchers = set()
        # We used to use a hack involving a TVar and a wrapper callback to
        # ensure that the callback was never invoked if the transaction in
        # which this TWeakRef was created never committed, but with recent
        # changes to how TWeakRef tracks weak references, this is no longer
        # necessary.
        self.callback = callback
        _stm_state.get_base().created_weakrefs.add(self)
        # TODO: Note in TWeakRef's documentation that TWeakRefs don't become
        # weak until the transaction creating them commits, so if they're
        # allowed to escape their creating transactions, they'll act like
        # strong references. Also consider changing that behavior so that they
        # become weak even if the transaction creating them doesn't commit, but
        # make sure not to invoke their callback in such a case.
    
    def get(self):
        """
        Return the value that this weak reference refers to, or None if its
        value has been garbage collected.
        
        This will always return the same value over the course of a given
        transaction.
        """
        if self._mature:
            value = self._weak_ref()
            if value is None and self in _stm_state.get_base().live_weakrefs:
                # Ref was live at some point during the past transaction but
                # isn't anymore
                raise _Restart
            # Value isn't inconsistent. Add it to the retry list (so that we'll
            # retry if we get garbage collected) and the check list (so that
            # we'll be checked for consistency again at the end of the
            # transaction).
            _stm_state.get_base().read_set.add(self)
            # Then, if we're live, add ourselves to the live list, so that if
            # we later die in the transaction, we'll properly detect an
            # inconsistency
            if value is not None:
                _stm_state.get_base().live_weakrefs.add(self)
            # Then return our value.
            return value
        else:
            # We were just created during this transaction, so we haven't
            # matured (and had our ref wrapped in an actual weak reference), so
            # return our value.
            return self._strong_ref
    
    value = property(get, doc="""A property wrapper around self.get.
    
    Note that this is a read-only property.""")
    
    def __call__(self):
        """
        An alias for self.get() provided for API compatibility with Python's
        weakref.ref class.
        """
        return self.get()
    
    @property
    def is_alive(self):
        return self.get() is not None
    
    def _check_clean(self, transaction):
        """
        Raises _Restart if we're mature, our referent has been garbage
        collected, and we're in the specified transaction's live_weakrefs list
        (which indicates that we previously read our referent as live during
        this transaction).
        """
        if self._mature and self._weak_ref() is None and self in transaction.live_weakrefs:
            # Ref was live during the transaction but has since been
            # dereferenced
            raise _Restart
    
    def _make_mature(self):
        """
        Matures this weak reference, setting self._mature to True (which causes
        all future calls to self.get to add ourselves to the relevant
        transaction's retry and check lists) and replacing our referent with
        an actual weakref.ref wrapper around it. This is called right at the
        end of the transaction in which this TWeakRef was created (and
        therefore only if it commits successfully) to make it live.
        
        The reason we keep around a strong reference until the end of the
        transaction in which the TWeakRef was created is to prevent a TWeakRef
        created in a transaction from being collected mid-way through the
        transaction and causing a restart as a result, which would result in an
        infinite restart loop.
        """
        self._mature = True
        self._strong_ref = None
    
    def _on_value_dead(self, ref):
        """
        Function passed to the underlying weakref.ref object to be called when
        it's collected. It spawns a thread (to avoid locking up whatever thread
        garbage collection is happening on) that notifies all of this
        TWeakRef's retry events and then runs self._callback in a transaction.
        """
        def run():
            with _global_lock:
                for e in self._events:
                    e.set()
            # FIXME: Run tracked functions in self._tracked_functions on
            # their own transaction (but revert the transaction in which
            # function is run), probably before we invoke the callback
            if self._callback is not None:
                atomically(self._callback)
        _Thread(name="%r dead value notifier" % self, target=run).start()
    
    def _add_retry_event(self, e):
        self._events.add(e)
    
    def _remove_retry_event(self, e):
        self._events.remove(e)


def atomically(function):
    """
    Run the specified function in an STM transaction.
    
    Changes made to TVars from within a transaction will not be visible to
    other transactions until the transaction commits, and changes from other
    transactions started after this one started will not be seen by this one.
    The net effect is one of wrapping every transaction with a global lock, but
    without the loss of parallelism that would result.
    
    If the specified function throws an exception, the exception will be
    propagated out, and all of the changes made to TVars during the course of
    the transaction will be reverted.
    
    atomically() fully supports nested transactions. If a nested transaction
    throws an exception, the changes it made are reverted, and the exception
    propagated out of the call to atomically().
    
    The return value of atomically() is the return value of the function that
    was passed to it.
    """
    toplevel = not bool(_stm_state.current)
    # If we're the outermost transaction, store down the time we're starting
    if toplevel:
        overall_start_time = time.time()
        current_start_time = overall_start_time
    while True:
        # If we have no current transaction, create a _BaseTransaction.
        # Otherwise, create a _NestedTransaction with the current one as its
        # parent.
        if toplevel:
            transaction = _BaseTransaction(overall_start_time, current_start_time)
        else:
            transaction = _NestedTransaction(_stm_state.current)
        # Then set it as the current transaction
        with _stm_state.with_current(transaction):
            # Then run the transaction. _BaseTransaction's implementation takes care
            # of catching _Retry and blocking until one of the vars we read is
            # modified, then converting it into a _Restart exception.
            try:
                return transaction.run(function)
            # Note that we'll only get _Retry thrown here if we're in a nested
            # transaction, in which case we want it to propagate out, so we
            # don't catch it here.
            except _Restart:
                # We were asked to restart. If we're a toplevel transaction,
                # just continue. If we're a _NestedTransaction, propagate the
                # exception up. TODO: Figure out a way to move this logic into
                # individual methods on _Transaction that _BaseTransaction and
                # _NestedTransaction can override accordingly.
                if toplevel:
                    # Update our current_start_time in preparation for our next
                    # run before continuing.
                    current_start_time = transaction.next_start_time
                    continue
                else:
                    raise


def retry(resume_after=None, resume_at=None):
    """
    Provides support for transactions that block.
    
    This function, when called, indicates to the STM system that the caller has
    detected state with which it isn't yet ready to continue (for example, a
    queue from which an item is to be read is actually empty). The current
    transaction will be immediately aborted and automatically restarted once
    at least one of the TVars it read has been modified.
    
    This can be used to make, for example, a blocking queue from a list with a
    function like the following::
    
        def pop_or_block(some_list):
            if len(some_list) > 0:
                return some_list.pop()
            else:
                retry()
    
    Functions making use of retry() can be multiplexed, a la Unix's select
    system call, with the or_else function. See its documentation for more
    information.
    
    Either resume_at or resume_after may be specified, and serve to indicate a
    timeout after which the call to retry() will give up and just return
    instead of retrying. resume_after indicates a number of seconds from when
    this transaction was first attempted at which to time out, and resume_at
    indicates a wall clock time (a la time.time()) at which to time out.
    
    Timeouts are highly experimental and a feature shared with only one other
    STM system that I know of (scala-stm), so I'd greatly appreciate feedback
    on this feature.
    """
    # Make sure we're in a transaction
    _stm_state.get_current()
    if resume_after is not None and resume_at is not None:
        raise ValueError("Only one of resume_after and resume_at can be "
                         "specified")
    # If resume_after was specified, compute resume_at in terms of it
    if resume_after is not None:
        resume_at = _stm_state.get_base().overall_start_time + resume_after
    # If we're retrying with a timeout (either resume_after or resume_at),
    # check to see if it's elapsed yet
    if resume_at is not None:
        if _stm_state.get_base().current_start_time >= resume_at:
            # It's elapsed, so just return.
            return
        else:
            # It hasn't elapsed yet, so let our base transaction know when we
            # want it to resume.
            _stm_state.get_base().update_resume_at(resume_at)
    # Either we didn't have a timeout or our timeout hasn't elapsed yet, so
    # raise _Retry.
    raise _Retry


def or_else(*functions):
    """
    Run (and return the value produced by) the first function passed into this
    function that does not retry (see the documentation of the retry()
    function), or retry if all of the passed-in functions retry (or if no
    arguments are passed in). See the documentation for retry() for more
    information.
    
    This function could be considered the STM equivalent of Unix's select()
    system call. One could, for example, read an item from the first of two
    queues, q1 and q2, to actually produce an item with something like this::
    
        item = or_else(q1.get, q2.get)
    
    or_else can also be used to make non-blocking variants of blocking
    functions. For example, given one of our queues above, we can get the first
    value available from the queue or, if it does not currently have any values
    available, return None with::
    
        item = or_else(q1.get, lambda: None)
    
    Note that each function passed in is automatically run in its own nested
    transaction so that the effects of those that end up retrying are reverted
    and only the effects of the function that succeeds are persisted.
    """
    # Make sure we're in a transaction
    _stm_state.get_current()
    for function in functions:
        # Try to run each function in sequence, in its own transaction so that
        # if it raises _Retry (or any other exception) its effects will be
        # undone.
        try:
            return atomically(function)
        except _Retry:
            # Requested a retry, so move on to the next alternative
            pass
    # All of the alternatives retried, so retry ourselves.
    retry()


def previously(function, toplevel=False):
    """
    (This function is highly experimental. Use at your own risk.)
    
    Return the value that the specified function would have returned had it
    been run in a transaction just prior to the current one.
    
    If toplevel is False, the specified function will be run as if it were just
    before the start of the innermost nested transaction, if any. If toplevel
    is True, the specified function will be run as if it were just before the
    start of the outermost transaction.
    
    This function can be used to propose invariants that reason about changes
    made over the course of a transaction, like the following invariant that
    prevents a particular variable from ever being decremented::
    
        @invariant
        def _():
            old_value = previously(lambda: some_var.get())
            new_value = some_var.get()
            if new_value < old_value:
                raise Exception("This var cannot be decremented")
    """
    # We don't need any special retry handling in _BaseTransaction like I
    # thought we would because we're calling the function directly, not calling
    # transaction.run(function), so we'll get _Restart and _Retry passed back
    # out to us.
    if toplevel:
        current = _stm_state.get_base()
    else:
        current = _stm_state.get_current()
    transaction = current.make_previously()
    try:
        with _stm_state.with_current(transaction):
            return function()
    finally:
        if isinstance(transaction, _BaseTransaction):
            # Copy over the read set so that, if the transaction retries, it'll
            # resume if any of the variables read here change
            current.read_set.update(transaction.read_set)
            if transaction.resume_at is not None:
                current.update_resume_at(transaction.resume_at)
        # If it's a nested transaction, it will have already modified our base
        # by virtue of using our base as its parent, so we don't need to do
        # anything else.


def watch(function, callback=None):
    """
    (This function is highly experimental and should not yet be used.)
    
    A function that generalizes the previous behavior of invariant() to allow
    side effects in a separate callback function when necessary. More
    documentation to come soon. invariant() will shortly be rewritten as a thin
    wrapper around this function.
    
    If callback is None, then a function is returned such that
    watch(function)(callback) is equivalent to watch(function, callback). This
    allows watch to be used as a decorator.
    """
    if callback is None:
        def decorator(actual_callback):
            watch(function, actual_callback)
        return decorator
    # FIXME: Run the invariant first to make sure that it passes right now
    _stm_state.get_current().proposed_watchers.append(_Watcher(function, callback))












































