"""Wait-and-retry around urllib for transient DNS / connection blips.

A single urllib call can fail on a sporadic name-resolution blip ("Name
or service not known" -- socket.gaierror errno -2; sometimes "Temporary
failure in name resolution") even though the network recovers within
seconds. A bare urlopen turns such a blip into a hard failure -- e.g.
seed.py's Cloudflare zone lookup returning None and falling through to an
interactive prompt that EOFErrors under --no-confirm.

This is the single place transient-network retry lives so the per-module
urllib wrappers (seed.fetch_cloudflare_account_id, ...) don't each
reinvent it.

Retries every `delay_s` seconds for up to `attempts` tries (default
15 x 60s ~= 14 min of tolerance, matching how long these blips last).
HTTPError is NOT retried -- a 4xx/5xx is a real server response, not a
network blip.
"""
from __future__ import annotations

import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Callable

DEFAULT_ATTEMPTS = 15
DEFAULT_DELAY_S = 60.0

# Substrings seen in URLError.reason for name-resolution outages, matched
# as a fallback when the reason is a plain string/OSError rather than a
# socket.gaierror instance.
_TRANSIENT_REASON_SUBSTRINGS = (
    "Name or service not known",
    "Temporary failure in name resolution",
    "nodename nor servname provided",
)


def _default_log(msg: str) -> None:
    print(f"[net-retry] {msg}", file=sys.stderr, flush=True)


def is_transient(exc: BaseException) -> bool:
    """True for DNS / connection blips worth waiting out; False for a real
    HTTP response (HTTPError) or any non-network error.

    HTTPError is a subclass of URLError, so it must be checked first."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, (socket.gaierror, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (socket.gaierror, TimeoutError, ConnectionError)):
            return True
        return any(s in str(reason) for s in _TRANSIENT_REASON_SUBSTRINGS)
    return False


def urlopen_retry(
    req: urllib.request.Request | str,
    *,
    timeout: float,
    attempts: int = DEFAULT_ATTEMPTS,
    delay_s: float = DEFAULT_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = _default_log,
    opener: Callable[..., object] | None = None,
):
    """urllib.request.urlopen with wait-and-retry on transient DNS /
    connection blips.

    Returns the opener result (a urllib response, usable as a context
    manager) from the first successful attempt. Re-raises immediately on a
    non-transient error (e.g. HTTPError) or after the final attempt, so the
    caller's existing error handling is unchanged once retries are spent.

    `sleep` and `opener` are injectable for tests. `opener` defaults to
    urllib.request.urlopen resolved AT CALL TIME (not as a def-default):
    a caller's unit test that patches `<module>.urllib.request.urlopen`
    must be honoured, but a def-bound default would capture the real
    urlopen at import time and silently bypass the patch."""
    if opener is None:
        opener = urllib.request.urlopen
    for attempt in range(1, attempts + 1):
        try:
            return opener(req, timeout=timeout)  # noqa: S310
        except Exception as exc:  # noqa: BLE001
            if not is_transient(exc) or attempt >= attempts:
                raise
            log(
                f"transient network error ({exc}); attempt "
                f"{attempt}/{attempts}, retrying in {delay_s:.0f}s"
            )
            sleep(delay_s)
    raise AssertionError("urlopen_retry: unreachable")  # pragma: no cover
