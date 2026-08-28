"""Shared helpers for the app's outbound HTTP calls (the inspiration
server, the YouTube Data API)."""

from __future__ import annotations


def terse_source_note(exc: Exception) -> str:
    """A trailing clause to append to an error message that wraps a
    low-level network failure.

    Python's ``urllib`` collapses DNS-lookup, connection-refused, TLS, and
    socket-timeout failures alike into a single short ``URLError`` reason
    (e.g. ``[Errno 8] nodename nor servname provided, or not known``) with
    nothing else attached — and the services we call this way tend to
    answer failures just as tersely. So whenever one of those errors is
    surfaced, say so explicitly: the short reason it carries is the full
    explanation available, not a truncated one, and it came from a layer
    known for being terse. Without that note a one-line reason reads like
    something upstream ate the useful part."""
    return (
        f" (this came through {type(exc).__name__} from a network layer/service "
        "notoriously terse with its error detail — the reason above is the full "
        "extent of what it reports)"
    )
