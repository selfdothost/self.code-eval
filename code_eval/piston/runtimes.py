"""Fetch + cache of ``GET /api/v2/runtimes`` (cavekit R2/R3 / T-004).

The Tier 0 resolver (:func:`.languages.resolve_language`) is deliberately pure —
it operates on a *passed-in* runtimes list so it can be unit-tested without a
network. This module is the thin fetch that produces that list from the live
endpoint and caches it, so the invocable-name/version table is resolved against
what the theseus deployment actually exposes (never a static ``packages.txt``).

The cache is a simple process-lifetime memo: the runtimes set is effectively
static for a deployment, and one eval run may resolve many languages. Callers
that need to invalidate it (tests, a redeploy) use :func:`clear_runtimes_cache`
or pass ``force_refresh=True``.
"""

from typing import List, Mapping, Optional, Sequence

from .client import piston_get, piston_json
from .config import BACKEND_GATEWAY, piston_backend
from .errors import PistonConnectionError

# Process-lifetime memo of the parsed runtimes payload. ``None`` = not fetched.
_RUNTIMES_CACHE: Optional[Sequence[Mapping]] = None

RUNTIMES_PATH = "/api/v2/runtimes"

# session-gateway's authenticated passthrough of the same payload
# (self.theseus/gitlab-profile#9, shipped 2026-07-27). Before it existed the
# gateway path had no way to discover languages at all and skipped resolution
# entirely; now both backends resolve against what the deployment really
# exposes, which is what R3 asked for.
GATEWAY_RUNTIMES_PATH = "/runtimes"


def get_runtimes(
    *, force_refresh: bool = False, require_enabled: bool = False
) -> Sequence[Mapping]:
    """Return the parsed ``GET /api/v2/runtimes`` list, fetching once and caching.

    A transport or bad-response failure raises
    :class:`.errors.PistonConnectionError` rather than yielding an
    empty/guessed runtimes list — that contract is unchanged.

    **The execution gate does not apply by default** (self.code-eval#6).
    Listing runtimes runs nothing; it is how a caller finds out what the
    deployment *could* run. Keeping it behind ENABLE_PISTON_EXECUTION conflated
    two different things — "do not execute untrusted code" and "do not read a
    list" — and made language registration impossible to gate on live
    capability while execution was still being throughput-tested
    (selfai/gitlab-profile#22).

    Execution itself is untouched: :func:`.client.piston_post` still gates, and
    so does every path that actually submits a program. Pass
    ``require_enabled=True`` to restore the old behaviour for a caller that
    genuinely should not proceed while Piston is off.
    """
    global _RUNTIMES_CACHE
    if _RUNTIMES_CACHE is None or force_refresh:
        if piston_backend() == BACKEND_GATEWAY:
            # Imported here rather than at module scope: gateway.py imports
            # execute.py, which imports this module.
            from .gateway import fetch_gateway_runtimes

            data = fetch_gateway_runtimes(require_enabled=require_enabled)
        else:
            data = piston_json(piston_get(RUNTIMES_PATH, require_enabled=require_enabled))
        _RUNTIMES_CACHE = _normalize_runtimes(data)
    return _RUNTIMES_CACHE


def clear_runtimes_cache() -> None:
    """Drop the cached runtimes payload (next :func:`get_runtimes` re-fetches)."""
    global _RUNTIMES_CACHE
    _RUNTIMES_CACHE = None


def _normalize_runtimes(data: object) -> List[Mapping]:
    """Coerce a runtimes payload into the ``List[Mapping]`` the resolver expects.

    Piston returns a bare JSON array of runtime objects; we only keep the mapping
    entries so a malformed element can't poison :func:`.languages.resolve_language`.
    """
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        raise PistonConnectionError(
            f"Expected GET {RUNTIMES_PATH} to return a JSON array, got {type(data)!r}."
        )
    return [rt for rt in data if isinstance(rt, Mapping)]
