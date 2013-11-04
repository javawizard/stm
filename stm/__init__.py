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
    
     * This class waits significantly longer (0.5 seconds at present) between
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
            time_to_sleep = min(0.5, self.resume_at - time.time())
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
        self.vars = {}
    
    def get_real_value(self, var):
        """
        Returns the real value of the specified variable, possibly throwing
        _Restart if the variable has been modified since this transaction
        started. This will only be called once for any given var in any given
        transaction; the value will thereafter be stored in self.vars.
        This must be overridden by the subclass.
        """
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
        Looks up the value of the specified variable in self.vars and returns
        it, or calls self.get_real_value(var) (and then stores it in self.vars)
        if the specified variable is not in self.vars. This is a concrete
        function; subclasses need not override it.
        """
        try:
            return self.vars[var]
        except KeyError:
            value = self.get_real_value(var)
            self.vars[var] = value
            return value
    
    def set_value(self, var, value):
        """
        Sets the entry in self.vars for the specified variable to the specified
        value.
        """
        # The logic for loading threatened invariants is in self.get_real_value,
        # so call self.get_value to load invariants if needed before setting
        # the var's value.
        self.get_value(var)
        self.vars[var] = value
    
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
        self.check_values = set()
        self.retry_values = set()
        self.created_weakrefs = set()
        self.live_weakrefs = set()
        self.proposed_invariants = []
        self.threatened_invariants = set()
        self.resume_at = None
        # Store off the transaction id we're starting at, so that we know if
        # things have changed since we started.
        if not start:
            with _global_lock:
                start = _last_transaction
        self.start = start
    
    def get_base_transaction(self):
        return self
    
    def get_real_value(self, var):
        # Just check to make sure the variable hasn't been modified since we
        # started (and raise _Restart if it has), then return its real value.
        # TODO: Update this comment to mention invariants
        with _global_lock:
            var._check_clean()
            self.check_values.add(var)
            self.retry_values.add(var)
            self.threatened_invariants.update(var._invariants)
            return var._real_value
    
    def run(self, function):
        global _last_transaction
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
        # First, we need to check invariants proposed during this
        # transaction and invariants threatened by variable accesses during
        # this transaction for consistency. We'll handle the former first.
        invariant_accesses = {}
        # Some of the invariants we run might access vars that cause
        # additional invariants to be loaded; we don't need to check these,
        # so iterate over a copy of self.threatened_invariants.
        for invariant in self.threatened_invariants.copy():
            # Create a nested transaction in which to run this invariant.
            # This will ensure that side effects of running the invariant
            # (including registering other invariants) aren't persisted.
            invariant_transaction = _NestedTransaction(self)
            with _stm_state.with_current(invariant_transaction):
                # If the invariant aborts, requests a restart, or retries, the
                # request should be passed along, so we don't need to catch any
                # exceptions here.
                invariant.check_invariant()
            # Now store off the list of variables that the invariant
            # accessed.
            invariant_accesses[invariant] = set(invariant_transaction.vars.keys())
        # Now we check proposed invariants for consistency. Invariants
        # proposed in the invariants themselves won't be persisted, so we
        # don't need to copy self.proposed_invariants like we did with
        # self.threatened_invariants.
        for function in self.proposed_invariants:
            invariant_transaction = _NestedTransaction(self)
            with _stm_state.with_current(invariant_transaction):
                new_invariant = _Invariant(function)
                new_invariant.check_invariant()
            invariant_accesses[new_invariant] = set(invariant_transaction.vars.keys())
        # All invariants were consistent. Now we acquire the global lock.
        with _global_lock:
            # Now we make sure nothing we read or modified changed since this
            # transaction started.
            for item in self.check_values:
                item._check_clean()
            # We also check our invariants to make sure none of them changed
            # since this transaction started.
            for invariant in invariant_accesses:
                if invariant.modified > self.start:
                    raise _Restart
            # Nothing changed, so we're good to commit. First we make
            # ourselves a new id.
            _last_transaction += 1
            modified = _last_transaction
            # Then we update the real values of all of the TVars. Note that
            # TVar._update_real_value takes care of notifying the TVar's
            # events for us.
            for var, value in self.vars.items():
                var._update_real_value(value, modified)
            # Then we update all of the invariants we ran.
            for invariant, var_set in invariant_accesses.iteritems():
                new_dependencies = var_set - invariant.dependencies
                old_dependencies = invariant.dependencies - var_set
                for var in old_dependencies:
                    var._invariants.discard(invariant)
                for var in new_dependencies:
                    var._invariants.add(invariant)
                invariant.dependencies = var_set
                invariant.modified = modified
            # And then we tell all TWeakRefs created during this
            # transaction to mature
            for ref in self.created_weakrefs:
                ref._make_mature()
    
    def retry_block(self):
        # Received a retry request that made it all the way up to the top.
        # First, check to see if any of the variables we've accessed have
        # been modified since we started; if they have, we need to restart
        # instead.
        with _global_lock:
            for item in self.check_values:
                item._check_clean()
            # Nope, none of them have changed. So now we create an event,
            # then add it to all of the vars we need to watch.
            e = _Event()
            for item in self.retry_values:
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
            for item in self.retry_values:
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
    
    def get_real_value(self, var):
        # Just get the value from our parent.
        return self.parent.get_value(var)
    
    def run(self, function):
        # Run the function, then (if it didn't throw any exceptions; _Restart,
        # _Retry, or otherwise) copy our values into our parent.
        result = function()
        self.commit()
        return result
    
    def commit(self):
        for var, value in self.vars.items():
            self.parent.set_value(var, value)
    
    def make_previously(self):
        return _NestedTransaction(self.parent)


class _Invariant(object):
    """
    A transactional invariant.
    
    These are objects created during commit time for every invariant proposed
    with stm.invariant. They store the invariant's actual function and a list
    of TVars that the invariant accessed during its last successful run.
    
    (They don't currently store references to TWeakRefs accessed during the
    last run. I'll be changing this soon.)
    """
    def __init__(self, function):
        self.function = function
        self.modified = 0
        # We don't need to keep around vars that can't be referenced by
        # anything else: if they're garbage collected, then the invariant
        # function itself wouldn't have been able to access them during its
        # next run, so we don't care about them.
        self.dependencies = weakref_module.WeakSet()
    
    def check_invariant(self):
        result = self.function()
        if result is None or result is True:
            return
        if result is False:
            raise Exception("Invariant %r was violated" % self.function)
        else:
            raise Exception("Invariant %r returned an unexpected value: %r"
                            % (self.function, result))


class TVar(object):
    """
    A transactional variable.
    
    TVars are the main primitives used within the STM system. They hold a
    reference to a single value. They can only be read or written from within a
    call to atomically().
    
    More complex datatypes (such as TList, TDict, and TObject) are available in
    stm.datatypes.
    """
    __slots__ = ["_events", "_real_value", "_modified", "_invariants",
                 "__weakref__"]
    
    def __init__(self, value=None):
        """
        Create a TVar with the specified initial value.
        """
        self._events = set()
        self._real_value = value
        self._modified = 0
        self._invariants = set()
    
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
    
    def _check_clean(self):
        # Check to see if our underlying value has been modified since the
        # start of this transaction, which should be a BaseTransaction
        if self._modified > _stm_state.get_base().start:
            # It has, so restart the transaction.
            raise _Restart
    
    def _add_retry_event(self, e):
        self._events.add(e)
    
    def _remove_retry_event(self, e):
        self._events.remove(e)
    
    def _update_real_value(self, value, modified):
        # NOTE: This is always called while the global lock is acquired
        # Update our real value and modified transaction
        self._real_value = value
        self._modified = modified
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
        self._events = set()
        self._mature = False
        self._ref = value
        # Use the TVar hack we previously mentioned in the docstring for
        # ensuring that the callback is only run if we commit. TODO: Double
        # check to make sure this is even necessary, as now that I think about
        # it we only create the underlying weakref when we commit, so we might
        # already be good to go.
        callback_check = TVar(False)
        callback_check.set(True)
        def actual_callback():
            if callback_check.get():
                callback()
        self._callback = actual_callback
        _stm_state.get_base().created_weakrefs.add(self)
    
    def get(self):
        """
        Return the value that this weak reference refers to, or None if its
        value has been garbage collected.
        
        This will always return the same value over the course of a given
        transaction.
        """
        if self._mature:
            value = self._ref()
            if value is None and self in _stm_state.get_base().live_weakrefs:
                # Ref was live at some point during the past transaction but
                # isn't anymore
                raise _Restart
            # Value isn't inconsistent. Add it to the retry list (so that we'll
            # retry if we get garbage collected) and the check list (so that
            # we'll be checked for consistency again at the end of the
            # transaction).
            _stm_state.get_base().check_values.add(self)
            _stm_state.get_base().retry_values.add(self)
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
            return self._ref
    
    value = property(get, doc="""A property wrapper around self.get.
    
    Note that this is a read-only property.""")
    
    def __call__(self):
        """
        An alias for self.get() provided for API compatibility with Python's
        weakref.ref class.
        """
        return self.get()
    
    def _check_clean(self):
        """
        Raises _Restart if we're mature, our referent has been garbage
        collected, and we're in our base transaction's live_weakrefs list
        (which indicates that we previously read our referent as live during
        this transaction).
        """
        if self._mature and self._ref() is None and self in _stm_state.get_base().live_weakrefs:
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
        self._ref = weakref_module.ref(self._ref, self._on_value_dead)
    
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
            current.check_values.update(transaction.check_values)
            current.retry_values.update(transaction.retry_values)
            if transaction.resume_at is not None:
                current.update_resume_at(transaction.resume_at)
        # If it's a nested transaction, it will have already modified our base
        # by virtue of using our base as its parent, so we don't need to do
        # anything else.


def invariant(function):
    """
    (This function is highly experimental and is not yet complete: invariants
    cannot be proposed in a nested transaction yet.)
    
    Provides support for transactional invariants.
    
    This function is called to propose a new invariant. The passed-in function
    must succeed now, at the end of the current transaction, and at the end of
    every subsequent transaction. If it fails at the end of any transaction,
    that transaction will be immediately aborted, and the exception raised by
    the invariant propagated.
    
    To succeed, a function must return either None or True. It can indicate
    failure either by returning False or by raising an exception. This allows
    both invariants that signal failure by raising an exception and invariants
    that signal success/failure by returning the value of a simple boolean
    expression.
    """
    # FIXME: Run the invariant first to make sure that it passes right now
    _stm_state.get_base().proposed_invariants.append(function)












































