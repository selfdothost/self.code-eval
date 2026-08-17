"""session-gateway transport — the live path to theseus's Piston (T-019).

`self.code-eval#5` and `selfai/gitlab-profile#22`'s opening post both specify
Piston's native ``POST /api/v2/execute``. That is no longer how callers reach it:
#22's later comments move everyone behind **session-gateway**, which fronts both
Piston and Kata with app-key auth, per-app quota and an admission queue. This
module is that transport; :mod:`.execute` picks between it and the native one on
``PISTON_BACKEND``.

Contract (verified against session-gateway's own README and
``src/piston-backend/pistonHttpBackend.ts``, not inferred from the issue thread):

- ``POST /submit`` with ``x-app-key``, ``type: "piston"``, a required
  ``priority`` and ``resources``, and **top-level** ``language`` / ``code`` /
  ``stdin``. Piston's own nested ``{language, version, files: [...]}`` shape is
  rejected outright — it used to fail confusingly and now fails clearly.
- ``resources`` are small **quota units** (``cpu: 2, ram: 2, appDisk: 4`` is the
  default per-app quota), not the byte/MB scale Piston's native
  ``run_memory_limit`` uses. See :mod:`.sizing`.
- A piston submit completes inline: the response is ``dispatched`` with
  ``detail.state`` already ``completed`` / ``failed``. It can still come back
  ``queued`` when the gateway is at capacity, which is what
  :func:`poll_until_settled` handles.

Per-request **version pinning is available** as of
``self.theseus/gitlab-profile#8`` (shipped 2026-07-27): ``POST /submit`` takes an
optional top-level ``version``, and the *resolved* version comes back in
``detail.result.version`` whether or not one was pinned — so a run records what
actually executed either way. :func:`submit` passes it through; the resolved
value lands on ``ExecutionResult.version``, falling back to ``"unpinned"`` only
when the gateway reports nothing.

Two things the gateway path still costs us, neither of them silently:

1. **No ``signal`` field.** The gateway flattens Piston's response to
   ``{stdout, stderr, exitCode}``. It does synthesize exit **137** for a
   signal kill, so :func:`.taxonomy.classify`'s SIGKILL rule still fires on the
   exit code — but ``ExecutionResult.signal`` is always ``None`` here.
2. **No separate compile stage.** On a compile failure the gateway returns the
   *compile* stage's streams and exit code with no marker saying so. A compile
   error is therefore indistinguishable from a runtime failure on this path and
   classifies as FAIL rather than COMPILE_ERROR. The stderr text still carries
   the compiler's own message, so it is diagnosable by a human, just not by the
   taxonomy.

Also worth knowing: the gateway runs its **own** transient retry around each
backend call. Our single cold-start retry (client kit R5) sits on top of that,
so a cold runtime can see more than two Piston-level attempts in total. That is
harmless — both layers bound themselves — but it means our call-count contract
describes *our* attempts, not Piston's.
"""

import time
from typing import Any, Dict, Mapping, Optional

import requests

from .config import (
    gateway_app_key,
    gateway_priority,
    gateway_url,
    piston_enabled,
)
from .errors import (
    GatewayExecutionFailedError,
    GatewayRejectedError,
    PistonConnectionError,
    PistonNotEnabledError,
    PistonTimeoutError,
)
from .execute import ExecutionResult, PistonStage

SUBMIT_PATH = "/submit"
STATUS_PATH = "/status"
# App-key authenticated passthrough of Piston's own /api/v2/runtimes
# (self.theseus/gitlab-profile#9).
GATEWAY_RUNTIMES_PATH = "/runtimes"

# Transport timeout (seconds) for a single HTTP call. Separate from the
# gateway's own `maxRuntimeMs` execution ceiling.
DEFAULT_HTTP_TIMEOUT_SECONDS = 30

# How long to wait on a `queued` submission before giving up, and how often to
# poll. A piston submit normally completes inline; queueing means the gateway is
# at capacity, which an eval run's burst of submissions can genuinely cause.
DEFAULT_POLL_TIMEOUT_SECONDS = 120
DEFAULT_POLL_INTERVAL_SECONDS = 1.0

# Terminal states from GET /status/:id.
#
# `not-found` is deliberately NOT in here (#8). The gateway answers a status
# lookup for an id it has just issued with HTTP 404 + `{"state":"not-found"}`
# for a short window after `POST /submit` returns — measured at ~0.5% of
# executes at concurrency 6, never once at concurrency 1. Treating that as
# settled would map a record with no `result` into an ExecutionResult with no
# exit code, which the taxonomy scores as a failing test: the same silent-0
# shape as the `failed`-record bug above, and invisible in aggregate. It is a
# registration race, so it is a reason to keep polling; a `not-found` that
# survives to `poll_timeout` is a real loss and raises there.
NOT_FOUND_STATE = "not-found"
SETTLED_STATES = frozenset({"completed", "failed", "cancelled", "expired"})

# The gateway's quota-scale ceiling on a single session, in ms. Our per-language
# timeouts are clamped to this rather than to Piston's raw 3000 ms run_timeout —
# the two ceilings are different things and only the gateway's is ours to set.
GATEWAY_MAX_RUNTIME_MS_CEILING = 60000


def _headers() -> Dict[str, str]:
    """Build request headers, requiring an app key.

    A missing key is a deployment error, not an execution outcome, so it raises
    with an actionable message rather than producing a mystery 401.
    """
    key = gateway_app_key()
    if not key:
        raise GatewayRejectedError(
            "missing-key",
            "No session-gateway application key configured. Set "
            "SESSION_GATEWAY_APP_KEY (issued by self.theseus via POST /admin/keys "
            "— see self.theseus/gitlab-profile#10).",
        )
    return {"x-app-key": key, "content-type": "application/json"}


def fetch_gateway_runtimes(
    *, http_timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS, require_enabled: bool = True
):
    """Fetch the gateway's authenticated ``GET /runtimes`` passthrough.

    Added by self.theseus/gitlab-profile#9 and app-key authenticated like
    ``/submit``. It proxies Piston's own ``/api/v2/runtimes`` verbatim, so the
    same pure resolver works against either backend — which restores
    language/version resolution (client kit R3) on the live path, where
    previously the token was passed through unchecked.
    """
    if require_enabled and not piston_enabled():
        raise PistonNotEnabledError(
            "Piston execution is disabled; refusing to query session-gateway."
        )
    return _get(GATEWAY_RUNTIMES_PATH, timeout=http_timeout, allow_list=True)


def build_submit_payload(
    source: str,
    language: str,
    *,
    stdin: str = "",
    resources: Optional[Mapping[str, Any]] = None,
    max_runtime_ms: Optional[int] = None,
    priority: Optional[str] = None,
    version: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``POST /submit`` body for a one-shot Piston eval.

    Note the shape: ``language``, ``code`` and ``version`` are **top level**.
    Nesting them under a ``"piston"`` key, or sending a ``files`` array, is
    rejected by the gateway with ``invalid-piston-request``.

    ``version`` pins the runtime (self.theseus/gitlab-profile#8, shipped
    2026-07-27). Omitting it falls back to the gateway's deployment default;
    either way the resolved version comes back in ``detail.result.version``.
    """
    payload: Dict[str, Any] = {
        "type": "piston",
        "priority": priority or gateway_priority(),
        "language": language,
        "code": source,
    }
    if version:
        payload["version"] = version
    if resources is not None:
        payload["resources"] = dict(resources)
    if stdin:
        payload["stdin"] = stdin
    if max_runtime_ms is not None:
        payload["maxRuntimeMs"] = min(int(max_runtime_ms), GATEWAY_MAX_RUNTIME_MS_CEILING)
    return payload


def _post(path: str, payload: Mapping[str, Any], *, timeout: int) -> Mapping[str, Any]:
    """POST to the gateway and return the parsed body, mapping transport failures."""
    url = f"{gateway_url()}{path}"
    try:
        response = requests.post(
            url, json=dict(payload), headers=_headers(), timeout=timeout
        )
    except requests.Timeout as exc:
        raise PistonTimeoutError(f"session-gateway {url} timed out: {exc}") from exc
    except requests.RequestException as exc:
        raise PistonConnectionError(
            f"Failed to reach session-gateway at {url}: {exc}"
        ) from exc
    return _parse_body(response, url)


def _get(path: str, *, timeout: int, allow_list: bool = False, allow_not_found: bool = False):
    """GET from the gateway and return the parsed body.

    ``allow_list`` because ``/runtimes`` proxies Piston verbatim and so returns a
    bare JSON array, while every other endpoint returns an object.
    """
    url = f"{gateway_url()}{path}"
    try:
        response = requests.get(url, headers=_headers(), timeout=timeout)
    except requests.Timeout as exc:
        raise PistonTimeoutError(f"session-gateway {url} timed out: {exc}") from exc
    except requests.RequestException as exc:
        raise PistonConnectionError(
            f"Failed to reach session-gateway at {url}: {exc}"
        ) from exc
    return _parse_body(response, url, allow_list=allow_list, allow_not_found=allow_not_found)


def _parse_body(response, url: str, *, allow_list: bool = False, allow_not_found: bool = False):
    """Validate status and decode JSON, with Piston-named errors on failure.

    ``allow_not_found`` lets a status lookup see the body behind a 404 (#8).
    The gateway signals a missing id with BOTH a 404 and `{"state":"not-found"}`,
    and the status-code check wins, so `not-found` never actually reached the
    poll loop that already knew the word. Scoped to the status path and to a
    404 whose body really does say `not-found`: every other 4xx/5xx, and a 404
    with any other body, still raises. Otherwise this would quietly swallow a
    revoked app key.
    """
    if response.status_code == 404 and allow_not_found:
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, Mapping) and str(body.get("state") or "") == NOT_FOUND_STATE:
            return body
    if response.status_code >= 400:
        raise PistonConnectionError(
            f"session-gateway {url} returned HTTP {response.status_code}: "
            f"{getattr(response, 'text', '')[:500]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise PistonConnectionError(
            f"session-gateway {url} returned a non-JSON body: {exc}"
        ) from exc
    if allow_list:
        if not isinstance(data, list):
            raise PistonConnectionError(
                f"session-gateway {url} returned {type(data).__name__}, expected an array"
            )
        return data
    if not isinstance(data, Mapping):
        raise PistonConnectionError(
            f"session-gateway {url} returned {type(data).__name__}, expected an object"
        )
    return data


def _raise_if_execution_failed(detail: Mapping[str, Any]) -> None:
    """Raise when a settled record is ``failed`` — the program never ran.

    session-gateway distinguishes these two precisely (``src/orchestration/
    pistonExecutor.ts``): a program that ran and exited non-zero is a
    **success** carrying a ``result``, while ``state: "failed"`` means every
    attempt failed transiently or hit a non-retryable error, so no verdict was
    ever produced. Scoring that as a failing test would be exactly the silent-0
    this client exists to prevent — a genuine case being an unknown runtime,
    which reports ``"<lang>-* runtime is unknown"`` and would otherwise look
    identical to a candidate whose tests failed.

    The real reason lives at ``detail.failure.message``; ``error``/``reason`` are
    checked as fallbacks so a shape change degrades to a vaguer message rather
    than an empty one.
    """
    if str(detail.get("state") or "") != "failed":
        return
    failure = detail.get("failure") or {}
    message = (
        (failure.get("message") if isinstance(failure, Mapping) else None)
        or (failure.get("reason") if isinstance(failure, Mapping) else None)
        or detail.get("error")
        or detail.get("reason")
        or "session-gateway reported a failed execution with no detail"
    )
    kind = failure.get("kind") if isinstance(failure, Mapping) else None
    raise GatewayExecutionFailedError(str(kind or "unknown"), str(message))


def _raise_if_rejected(body: Mapping[str, Any]) -> None:
    """Turn a ``rejected`` submission into a named error.

    A rejection never reaches a backend, so it must not be scored — it is an
    admission/validation failure (bad key, quota exhausted, malformed request),
    which the taxonomy classifies as ERROR rather than a failing test.
    """
    if body.get("status") == "rejected":
        raise GatewayRejectedError(
            str(body.get("reason") or "unknown"),
            str(body.get("message") or "session-gateway rejected the submission"),
        )


def poll_until_settled(
    request_id: str,
    *,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    http_timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    sleep=time.sleep,
) -> Mapping[str, Any]:
    """Poll ``GET /status/:id`` until the request reaches a terminal state.

    Only needed when a submission comes back ``queued`` (the gateway at
    capacity). ``sleep`` is injectable so tests do not spend real time.

    A ``not-found`` answer is treated as not-yet-settled rather than terminal
    (#8) — see :data:`SETTLED_STATES`. The deadline is unchanged and is still
    the ceiling: an id that is *genuinely* unknown polls until ``poll_timeout``
    and then raises naming it, so "the record had not registered yet" and "this
    id does not exist" stay distinguishable instead of both becoming a resultless
    pass.
    """
    elapsed = 0.0
    while True:
        body = _get(
            f"{STATUS_PATH}/{request_id}", timeout=http_timeout, allow_not_found=True
        )
        state = str(body.get("state") or "")
        if state in SETTLED_STATES:
            return body
        if elapsed >= poll_timeout:
            if state == NOT_FOUND_STATE:
                raise PistonTimeoutError(
                    f"session-gateway never registered request {request_id}: still "
                    f"{NOT_FOUND_STATE!r} after {poll_timeout}s. The submission was "
                    "accepted but no record ever appeared, so the program did not run."
                )
            raise PistonTimeoutError(
                f"session-gateway request {request_id} still {state!r} after "
                f"{poll_timeout}s — gave up waiting for the admission queue."
            )
        sleep(poll_interval)
        elapsed += poll_interval


def _result_from_detail(
    detail: Mapping[str, Any], language: str
) -> ExecutionResult:
    """Map a settled gateway record onto an :class:`ExecutionResult`.

    The gateway hands back ``{stdout, stderr, exitCode}`` only. ``signal`` is
    always ``None`` on this path and there is no separate compile stage — see the
    module docstring for what that costs the taxonomy. Exit 137 is preserved
    (the gateway synthesizes it for signal kills), so the SIGKILL rule still
    fires on the exit code.
    """
    result = detail.get("result") or {}
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    exit_code = result.get("exitCode")
    if exit_code is not None:
        exit_code = int(exit_code)

    run = PistonStage(stdout=stdout, stderr=stderr, code=exit_code, signal=None)
    return ExecutionResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        signal=None,
        language=language,
        # The resolved runtime version, when the gateway reports it. It lives at
        # `detail.result.version` (verified live 2026-07-27: a python submit
        # comes back with "3.12.0"), which lands half of client kit R6 —
        # we can record what actually ran even though we still cannot *pin* it
        # per request (self.theseus/gitlab-profile#8). "unpinned" is the honest
        # fallback when the field is absent, e.g. on a failed record that never
        # reached a runtime.
        version=str(result.get("version") or detail.get("version") or "unpinned"),
        run=run,
        compile=None,
        raw=dict(detail),
    )


def gateway_execute(
    source: str,
    language: str,
    *,
    stdin: str = "",
    resources: Optional[Mapping[str, Any]] = None,
    max_runtime_ms: Optional[int] = None,
    priority: Optional[str] = None,
    version: Optional[str] = None,
    http_timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep=time.sleep,
) -> ExecutionResult:
    """Run one program through session-gateway and return its parsed result.

    ``language`` is passed straight through — the gateway maintains no language
    list of its own, so validity is Piston's to decide. Resolution against
    ``GET /runtimes`` (the gateway's own authenticated passthrough, shipped in
    ``self.theseus/gitlab-profile#9``) happens a level up in
    :func:`.execute.execute_program`, which is where both backends share it.
    """
    if not piston_enabled():
        raise PistonNotEnabledError(
            "Piston execution is disabled; refusing to submit to session-gateway."
        )

    payload = build_submit_payload(
        source,
        language,
        stdin=stdin,
        resources=resources,
        max_runtime_ms=max_runtime_ms,
        priority=priority,
        version=version,
    )
    body = _post(SUBMIT_PATH, payload, timeout=http_timeout)
    _raise_if_rejected(body)

    status = str(body.get("status") or "")
    request_id = str(body.get("requestId") or "")
    detail = body.get("detail") or {}

    # Poll whenever the record we were handed is not already settled. That
    # covers the documented `queued` case AND the one observed under real burst
    # load: a `dispatched` response whose detail has not reached a terminal
    # state yet. Mapping that half-formed record directly would produce an
    # ExecutionResult with no exit code, which the taxonomy would score as a
    # failing test — the same silent-0 shape as the `failed`-record bug.
    needs_poll = status == "queued" or str(detail.get("state") or "") not in SETTLED_STATES

    if needs_poll:
        if not request_id:
            raise PistonConnectionError(
                f"session-gateway returned status {status!r} with neither a settled "
                "record nor a requestId to poll."
            )
        detail = poll_until_settled(
            request_id,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
            http_timeout=http_timeout,
            sleep=sleep,
        )
    elif not detail:
        raise PistonConnectionError(
            f"session-gateway returned status {status!r} with no detail payload."
        )

    _raise_if_execution_failed(detail)
    return _result_from_detail(detail, language)
