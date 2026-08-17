"""Guarded HTTP plumbing for the Piston client (cavekit R1 / T-001).

This module owns the two invariants the config surface must guarantee *before*
the real execute contract (T-004) is built on top of it:

1. **No HTTP when off.** :func:`require_piston_enabled` is the single gate every
   Piston code path must pass through. If the flag is off it raises
   :class:`PistonNotEnabledError` *before* any request object is constructed —
   so no HTTP request is ever issued to ``PISTON_BASE_URL`` while the feature is
   disabled.
2. **Never a silent 0.** :func:`piston_post` wraps the transport and converts
   any connection/transport failure into a :class:`PistonConnectionError`. A
   dropped connection therefore surfaces as a distinct error, never a 0-score
   that looks like a genuinely failing test.

T-004 builds the real ``execute()`` (source / stdin / resources / run_timeout /
response parsing) on top of :func:`piston_post`; this tier only wires the gate
and the error contract.
"""

from typing import Any, Mapping

import requests

from .config import piston_base_url, piston_enabled
from .errors import (
    PistonConnectionError,
    PistonNotEnabledError,
    PistonTimeoutError,
)

# Conservative transport timeout (seconds) for the guarded POST. The per-language
# Piston ``run_timeout`` (T-007) is a separate, in-payload concern; this is only
# the socket/HTTP ceiling so a dead endpoint fails fast instead of hanging.
_DEFAULT_HTTP_TIMEOUT = 30.0


def require_piston_enabled() -> str:
    """Assert Piston execution is opted in and return the base URL.

    The gate every Piston path passes through. Raises
    :class:`PistonNotEnabledError` (loudly, before any request is built) when the
    flag is off, guaranteeing no HTTP request reaches ``PISTON_BASE_URL`` while
    disabled.
    """
    if not piston_enabled():
        raise PistonNotEnabledError(
            "Piston execution is disabled (ENABLE_PISTON_EXECUTION is off); "
            "refusing to issue a request to PISTON_BASE_URL."
        )
    return piston_base_url()


def piston_post(
    path: str,
    payload: Mapping[str, Any],
    timeout: float = _DEFAULT_HTTP_TIMEOUT,
) -> requests.Response:
    """POST ``payload`` to ``{PISTON_BASE_URL}{path}`` behind the enable gate.

    Returns the raw :class:`requests.Response` for T-004 to parse. Any transport
    failure (unreachable host, DNS error, timeout, connection reset) is
    re-raised as :class:`PistonConnectionError` so it can never be silently
    scored as a failing test.
    """
    base_url = require_piston_enabled()
    url = f"{base_url}{path}"
    try:
        return requests.post(url, json=dict(payload), timeout=timeout)
    except requests.Timeout as exc:
        # Transport-level timeout: the retriable cold-start signal (T-007). Kept
        # distinct from a plain unreachable endpoint so the retry policy fires on
        # a timeout only. ``requests.Timeout`` is a subclass of ``RequestException``
        # so this except MUST precede the generic one below.
        raise PistonTimeoutError(
            f"Piston execute request to {url} timed out: {exc}"
        ) from exc
    except requests.RequestException as exc:
        raise PistonConnectionError(
            f"Failed to reach Piston at {url}: {exc}"
        ) from exc


def piston_get(
    path: str,
    timeout: float = _DEFAULT_HTTP_TIMEOUT,
    *,
    require_enabled: bool = True,
) -> requests.Response:
    """GET ``{PISTON_BASE_URL}{path}``, by default behind the same enable gate
    as :func:`piston_post`.

    The read-side sibling of :func:`piston_post`, used to fetch
    ``GET /api/v2/runtimes`` (see :mod:`.runtimes`). A transport failure always
    surfaces as :class:`PistonConnectionError` — never a silent result.

    ``require_enabled=False`` skips *only* the execution gate, for the one
    read-only case that needs it: discovering which languages a deployment
    exposes (self.code-eval#6). ENABLE_PISTON_EXECUTION means "do not run
    untrusted code through Piston"; it was also, incidentally, preventing a
    plain list lookup, which made language registration impossible to gate on
    what the deployment can actually run. Nothing here executes anything, and
    :func:`piston_post` is untouched — the execution gate is exactly where it
    was.
    """
    base_url = require_piston_enabled() if require_enabled else piston_base_url()
    url = f"{base_url}{path}"
    try:
        return requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        raise PistonConnectionError(
            f"Failed to reach Piston at {url}: {exc}"
        ) from exc


def piston_json(response: requests.Response) -> Any:
    """Decode a Piston JSON response, honouring the never-a-silent-0 contract.

    A non-2xx status or an undecodable body means we never got a trustworthy
    verdict from Piston, so both raise :class:`PistonConnectionError` rather than
    being parsed into a (mis-scored) run result. A normal program that exits
    non-zero still comes back as HTTP 200 with a valid body, so this does not
    conflate a failing test with a transport/response error.
    """
    status = getattr(response, "status_code", None)
    if status is not None and not (200 <= int(status) < 300):
        body = ""
        try:
            body = response.text
        except Exception:  # pragma: no cover - defensive; body is best-effort
            body = "<unreadable body>"
        raise PistonConnectionError(
            f"Piston returned HTTP {status}: {body[:500]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise PistonConnectionError(
            f"Piston returned a non-JSON response: {exc}"
        ) from exc
