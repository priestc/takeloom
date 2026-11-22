"""Keeping every PortAudio stream's Python callback alive until the stream
is really closed.

sounddevice hands PortAudio a cffi callback that lives exactly as long as
the Python Stream object holding it (Stream._callback); sounddevice has no
__del__ that closes the stream first. So if a running stream's object is
ever dropped (a teardown that raised partway, two threads tearing the same
streams down at once), CoreAudio keeps calling a freed callback on its
realtime IO thread — and the process dies there with nothing pointing back
at the Python code. That's what the server's 2026-11-22 13:37 crash looked
like: SIGBUS in ffi_closure_SYSV_inner, called from PortAudio's
AudioIOProc, trying to allocate ~520 KB of stack (a garbage argument count
read from freed memory).

So every stream is registered here when it's opened (keep_alive) and only
released once close() has actually returned (close_stream), which is also
once-only and safe to call from any thread, any number of times. A stream
whose owner forgets it now leaks (it keeps running, harmlessly) rather than
crashing the process.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_live: set = set()  # opened, not yet closed
_closing: set = set()  # close_stream() in progress on some thread


def keep_alive(stream):
    """Register a just-opened sounddevice stream; returns it."""
    with _lock:
        _live.add(stream)
    return stream


def close_stream(stream) -> None:
    """Stop and close `stream` exactly once, releasing it only after the
    close has returned. Anything else with .stop()/.close() (an engine
    tap, a MidiInput) that was never registered is just stopped and
    closed, as before."""
    with _lock:
        tracked = stream in _live
        if tracked:
            if stream in _closing:
                return  # another thread is already closing it
            _closing.add(stream)
    if not tracked:
        stream.stop()
        stream.close()
        return
    stop_error: BaseException | None = None
    try:
        stream.stop()
    except BaseException as e:  # noqa: BLE001 — still close it; re-raised below
        stop_error = e
    try:
        stream.close()
    except BaseException:
        # Not known to be closed: keep the callback alive for good.
        with _lock:
            _closing.discard(stream)
        raise
    with _lock:
        _closing.discard(stream)
        _live.discard(stream)
    if stop_error is not None:
        raise stop_error


def open_stream_count() -> int:
    with _lock:
        return len(_live)
