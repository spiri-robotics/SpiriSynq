"""Deterministic shutdown of zenoh sessions.

THE PROBLEM
-----------
Every ``zenoh.open()``, and every ``declare_subscriber`` / ``declare_queryable``
call, spawns *non-daemon* OS threads inside zenoh's Rust/Tokio runtime (they
show up in ``threading.enumerate()`` with "pyo3" in their name). Non-daemon
threads block interpreter shutdown: CPython will not exit until every one of
them has been joined.

zenoh only stops those threads when the session is closed
(``zenoh_session.close()``), which we wrap in ``Session.close()``. So the rule
is simple: *every Session must be closed before the interpreter tries to join
threads*, or the process hangs forever on exit.

The hard part is making that happen automatically, without forcing every caller
to remember an explicit ``.close()``.

WHY THE OBVIOUS FIXES DON'T WORK
--------------------------------
``__del__`` / ``weakref.finalize``
    These only run when the object is garbage-collected. That is fine on the
    happy path, but under pytest a *failing* test stores its traceback, and the
    traceback keeps the test's frame locals (the Session, the objects) alive
    until well after the test has finished. They are never collected before
    shutdown, so ``__del__`` never fires and the threads never stop. We keep
    ``Session.__del__`` anyway -- it gives prompt cleanup when objects *are*
    collected normally -- but it cannot be the only mechanism.

``atexit.register(...)``
    Too late. In CPython 3.13 ``threading._shutdown()`` (which joins the
    non-daemon threads) runs *before* ``atexit`` handlers. By the time an atexit
    handler could close the sessions, the interpreter is already blocked on the
    join. Verified empirically.

context manager (``with Session() as s:``)
    Works for scripts and tests with a clear scope, but not for the case this
    library is built for: open a session, register long-lived subscriber /
    queryable callbacks, and hand control back to an event loop. There is no
    natural block to close at.

daemonizing the threads
    The textbook answer to "non-daemon threads block exit" is to make them
    daemon threads. We can daemonize the few threads spawned by ``zenoh.open()``
    (there is a brief window before they start), but the per-subscriber /
    per-queryable threads are already running by the time we can see them, and
    CPython 3.13 forbids ``thread.daemon = True`` after start
    (``RuntimeError: cannot set daemon status of active thread``). zenoh exposes
    no knob to spawn its runtime threads as daemons, so this can only ever cover
    a subset of the threads -- insufficient on its own.

monkey-patching ``threading._shutdown``
    Would work, but replacing a core-library function wholesale is a large,
    fragile blast radius. Rejected.

THE MECHANISM WE USE
--------------------
``threading._register_atexit(func)`` registers a callback that runs *inside*
``threading._shutdown()``, BEFORE the non-daemon threads are joined -- precisely
the window ``atexit`` misses. This is not a trick we invented: CPython's own
``concurrent.futures`` uses the exact same hook to stop its worker threads and
avoid this identical deadlock.

We keep a weak registry of every live Session and, at that pre-join moment,
close them all. It holds *weak* references on purpose: it must never keep a
Session alive by itself (that would defeat normal GC and resurrect sessions
pytest is pinning). It simply closes whatever is still alive at exit -- which is
exactly the set of leaked sessions that would otherwise hang the join.

A WeakValueDictionary keyed by id() is used rather than a WeakSet because
Session is a ``@dataclass`` (eq=True => __hash__ is None => unhashable), so it
cannot be a set member. This mirrors the existing ``Session.objects`` registry.

PORTABILITY  /  WHY THIS MAY RAISE AT IMPORT TIME
-------------------------------------------------
``threading._register_atexit`` is a CPython implementation detail (added in
CPython 3.9, bpo-39812). It is not part of the language spec, and other
implementations -- PyPy, GraalPy, etc. -- are not guaranteed to provide it, nor
to give it the same "before the non-daemon join" ordering even if they do.

Rather than silently fall back to behaviour that would hang the process on exit,
we fail loud: if the hook is unavailable we raise at import time with an
explanation, so the problem surfaces immediately instead of as a mysterious CI
hang. If you are porting SpiriSynq to such an interpreter, this is the file to
revisit.
"""

import platform
import threading
import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from SpiriSynq.session import Session


_live_sessions: "weakref.WeakValueDictionary[int, Session]" = (
    weakref.WeakValueDictionary()
)


def register_session(session: "Session") -> None:
    """Register a Session to be closed automatically at interpreter shutdown.

    Holds only a weak reference, so registering never keeps the session alive;
    it only lets :func:`_close_all_sessions` find and close it before the
    interpreter joins zenoh's non-daemon threads. Keyed by ``id()`` because a
    ``@dataclass`` Session is unhashable (see the module docstring).
    """
    _live_sessions[id(session)] = session


def _close_all_sessions() -> None:
    """Close every still-open Session before non-daemon threads are joined.

    Snapshots the registry to a list first so that sessions disappearing
    mid-iteration (their last strong ref dropping) cannot raise.
    """
    for session in list(_live_sessions.values()):
        # Session.close() already swallows its own errors; this loop must never
        # raise during interpreter shutdown, or it could leave the rest of the
        # sessions unclosed and re-introduce the hang.
        session.close()


def _install_shutdown_hook() -> None:
    """Wire :func:`_close_all_sessions` into the interpreter's thread-shutdown.

    Raises:
        RuntimeError: if the running interpreter does not provide
            ``threading._register_atexit``. See the module docstring for the
            full rationale; the short version is that without this hook zenoh's
            non-daemon threads hang the process on exit, so we refuse to run
            rather than hang silently.
    """
    register = getattr(threading, "_register_atexit", None)
    if register is None:
        raise RuntimeError(
            "SpiriSynq needs threading._register_atexit to shut down zenoh's "
            "non-daemon worker threads before the interpreter joins them. This is "
            "a CPython implementation detail (CPython 3.9+) and is missing on the "
            f"current interpreter ({platform.python_implementation()} "
            f"{platform.python_version()}).\n\n"
            "Without it, any process that opens a Session would hang forever on "
            "exit. See the module docstring in SpiriSynq/shutdown.py for the "
            "problem space and the alternatives that were investigated before "
            "settling on this hook."
        )
    register(_close_all_sessions)


_install_shutdown_hook()
