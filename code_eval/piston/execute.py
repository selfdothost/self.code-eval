"""The core execute contract: POST ``/api/v2/execute`` and parse the run result
(cavekit R2 / T-004).

This is the reusable adapter both harness seams (T-011 MultiPL-E, T-015 Python
``exec()``) will call. It builds on the Tier 0 seams and does **only** the R2
contract:

- resolve language + version via :func:`.languages.resolve_language` *before*
  building the request (an unknown token raises before any HTTP is issued);
- build a Piston v2 execute payload carrying source, stdin, language, version,
  ``resources`` and ``run_timeout``;
- POST it through :func:`.client.piston_post` (enable gate + connection-error
  contract already enforced there);
- parse the ``run`` (and, for compiled languages, ``compile``) stage of the
  Piston v2 response into an :class:`ExecutionResult` exposing stdout, stderr,
  exit code and signal as distinct fields.

Deliberate extension points left for later tasks (do not implement here):

- **T-005 (result taxonomy):** classify :class:`ExecutionResult` (pass / fail /
  timeout / SIGKILL / compile-error). The stage objects and distinct fields
  below are the raw material; add the taxonomy on top, don't fold it in.
- **T-006 (resource sizing):** replace :data:`DEFAULT_RESOURCES` with a
  per-language lookup and pass it in via the ``resources`` argument.
- **T-007 (run_timeout + cold-start retry):** replace :data:`DEFAULT_RUN_TIMEOUT_MS`
  with a per-language value and wrap :func:`execute` with the single-retry policy.
- **T-008 (reproducibility metadata):** ``language`` / ``version`` are already on
  the result; surface them into the eval results metadata.
"""

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Sequence

from .client import piston_json, piston_post
from .config import BACKEND_GATEWAY, piston_backend
from .errors import PistonTimeoutError
from .languages import resolve_language
from .runtimes import get_runtimes
from .sizing import (
    DEFAULT_RESOURCES,
    DEFAULT_RUN_TIMEOUT_MS,
    gateway_max_runtime_ms_for,
    gateway_resources_for,
    resources_for,
    run_timeout_for,
)

# ``DEFAULT_RESOURCES`` / ``DEFAULT_RUN_TIMEOUT_MS`` now live in :mod:`.sizing`
# as the interpreted-language / cap fallbacks; the per-language lookups
# (:func:`.sizing.resources_for`, :func:`.sizing.run_timeout_for`) are what
# :func:`execute` actually injects. They are re-exported here so the R2-era
# imports keep working.

EXECUTE_PATH = "/api/v2/execute"

# Retries allowed AFTER the first attempt, for cold-start signals only.
# Two, not one: the original R5 contract said "exactly one", written against a
# transport timeout. Measured on the live deployment, a cold julia is SIGKILLed
# TWICE before succeeding on the third attempt — one retry would still score it
# a hard 0. See execute_with_cold_start_retry.
MAX_COLD_START_RETRIES = 2


@dataclass(frozen=True)
class PistonStage:
    """One stage (``run`` or ``compile``) of a Piston v2 execute response.

    Mirrors the upstream shape 1:1 so a later task (T-005) can classify on any of
    these without re-fetching. ``code`` is the process exit code; ``signal`` is
    the terminating signal name (e.g. ``"SIGKILL"``) or ``None``.
    """

    stdout: str
    stderr: str
    code: Optional[int]
    signal: Optional[str]
    output: str = ""


@dataclass(frozen=True)
class ExecutionResult:
    """Parsed result of one execute call, with the four scorable fields distinct.

    ``stdout`` / ``stderr`` / ``exit_code`` / ``signal`` are hoisted from the
    ``run`` stage (what the harness scores on). The full ``run`` and (for compiled
    languages) ``compile`` stages are kept so T-005's taxonomy and T-008's
    metadata can read them; ``raw`` is the untouched response payload.
    """

    stdout: str
    stderr: str
    exit_code: Optional[int]
    signal: Optional[str]
    language: str
    version: str
    run: Optional[PistonStage]
    compile: Optional[PistonStage] = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    # True only for the synthetic result the T-007 retry policy returns when a
    # request times out (at the transport layer) on every attempt. Lets the
    # T-005 taxonomy report a persistent timeout as a distinct result — never a
    # silent 0 or a normal failure.
    timed_out: bool = False
    # How many execute attempts produced this result (1 = no retry), and why
    # each retry fired. Surfaced into results metadata so a score that depended
    # on retries is auditable rather than invisible.
    attempts: int = 1
    retry_reasons: tuple = ()

    @property
    def compile_failed(self) -> bool:
        """True if a compile stage ran and exited non-zero.

        Compiled languages (C#, Rust, Java) fail at the ``compile`` stage; the
        ``run`` stage then never executes. Exposed so T-005 can distinguish a
        compile error from a runtime failure — not a taxonomy itself.
        """
        return self.compile is not None and (self.compile.code or 0) != 0


def build_execute_payload(
    source: str,
    *,
    invocable: str,
    version: str,
    stdin: str = "",
    resources: Optional[Mapping[str, Any]] = None,
    run_timeout: int = DEFAULT_RUN_TIMEOUT_MS,
    files: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build the Piston v2 ``/api/v2/execute`` request body.

    The program source is carried in ``files[].content`` (the upstream Piston v2
    shape — there is no top-level ``source`` key in the API). ``resources`` and
    ``run_timeout`` are accepted as arguments so T-006 / T-007 can inject
    per-language values without touching this function.

    .. note::
       **``resources`` is not a Piston v2 field.** Reading session-gateway's
       ``src/piston-backend/pistonHttpBackend.ts`` settled this: native Piston
       accepts ``{language, version, files, stdin, args, compile_timeout,
       run_timeout, compile_memory_limit, run_memory_limit}`` and nothing named
       ``resources`` — that is a *session-gateway* concept, which is where the
       kit's example came from. The key is still emitted (harmless; Piston
       ignores unknown keys) because it documents the intended budget, but the
       budget is *also* translated into the real ``run_memory_limit`` /
       ``compile_memory_limit`` fields, which is what actually takes effect.
       On the gateway path the units differ entirely — see
       :func:`.sizing.gateway_resources_for`.
    """
    if resources is None:
        resources = dict(DEFAULT_RESOURCES)
    if files is None:
        files = [{"content": source}]

    body = {
        "language": invocable,
        "version": version,
        "files": [dict(f) for f in files],
        "stdin": stdin,
        "resources": dict(resources),
        "run_timeout": run_timeout,
    }

    # Translate the budget into the fields Piston actually reads. `ram` is in MB
    # in our table; Piston's memory limits are bytes. A non-positive value means
    # "unlimited" to Piston, so only send a real limit.
    ram_mb = resources.get("ram")
    if isinstance(ram_mb, int) and ram_mb > 0:
        body["run_memory_limit"] = ram_mb * 1024 * 1024
        body["compile_memory_limit"] = ram_mb * 1024 * 1024
    return body


def _parse_stage(raw: Optional[Mapping[str, Any]]) -> Optional[PistonStage]:
    """Parse one ``run``/``compile`` stage object; ``None`` if the stage is absent."""
    if not raw:
        return None
    return PistonStage(
        stdout=raw.get("stdout", "") or "",
        stderr=raw.get("stderr", "") or "",
        code=raw.get("code"),
        signal=raw.get("signal"),
        output=raw.get("output", "") or "",
    )


def parse_execute_response(
    data: Mapping[str, Any],
    *,
    language: str,
    version: str,
) -> ExecutionResult:
    """Parse a Piston v2 execute response into an :class:`ExecutionResult`.

    The top-level scorable fields come from the ``run`` stage. A ``compile`` stage
    (present only for compiled languages) is parsed and kept distinct so a
    compile failure is surfaced rather than masquerading as a passing run.
    """
    run = _parse_stage(data.get("run"))
    compile_stage = _parse_stage(data.get("compile"))

    if run is not None:
        stdout, stderr, exit_code, signal = (
            run.stdout,
            run.stderr,
            run.code,
            run.signal,
        )
    elif compile_stage is not None:
        # Compile failed hard enough that no run stage exists: surface the
        # compile stage's stream/exit so the failure is not read as an empty pass.
        stdout, stderr, exit_code, signal = (
            compile_stage.stdout,
            compile_stage.stderr,
            compile_stage.code,
            compile_stage.signal,
        )
    else:
        stdout, stderr, exit_code, signal = "", "", None, None

    return ExecutionResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        signal=signal,
        language=language,
        version=version,
        run=run,
        compile=compile_stage,
        raw=data,
    )


def execute(
    source: str,
    language_token: str,
    *,
    stdin: str = "",
    runtimes: Optional[Sequence[Mapping]] = None,
    resources: Optional[Mapping[str, Any]] = None,
    run_timeout: Optional[int] = None,
) -> ExecutionResult:
    """Run one program in Piston and return its parsed :class:`ExecutionResult`.

    Steps, in order:

    1. Resolve ``language_token`` -> ``(invocable, version)`` against the runtimes
       payload (fetched + cached via :func:`.runtimes.get_runtimes` when not
       supplied). An unknown token raises :class:`.errors.UnknownLanguageError`
       *before* any request is built.
    2. Build the execute payload (source / stdin / language / version /
       ``resources`` / ``run_timeout``).
    3. POST it through :func:`.client.piston_post` — the enable gate and
       connection-error contract already live there.
    4. Parse the response's ``run`` (and ``compile``) stages.

    ``resources`` / ``run_timeout`` default to the per-language values from
    :mod:`.sizing` (T-006 / T-007) resolved off the *invocable* name; pass them
    explicitly to override. A transport timeout surfaces as
    :class:`.errors.PistonTimeoutError` — :func:`execute_with_cold_start_retry`
    is the wrapper that applies the single-retry policy on top.

    **Backend routing.** With ``PISTON_BACKEND=gateway`` (the default, and the
    live path) this delegates to :func:`.gateway.gateway_execute` before any of
    the above: session-gateway takes the language token straight through, so
    there is no ``GET /api/v2/runtimes`` resolution step and no version to
    resolve. The steps below describe the ``direct`` backend only.
    """
    if piston_backend() == BACKEND_GATEWAY:
        from .gateway import gateway_execute

        # Resolve against the gateway's own /runtimes passthrough
        # (self.theseus/gitlab-profile#9). Before that endpoint existed the token
        # was passed through unchecked; now the gateway path gets the same R3
        # guarantee as the direct one — an unknown language raises
        # UnknownLanguageError before any submission, and the resolved version is
        # PINNED on the request (self.theseus/gitlab-profile#8) rather than left
        # to whatever the deployment happens to have installed.
        if runtimes is None:
            runtimes = get_runtimes()
        invocable, version = resolve_language(language_token, runtimes)

        return gateway_execute(
            source,
            invocable,
            stdin=stdin,
            version=version,
            resources=resources
            if resources is not None
            else gateway_resources_for(invocable),
            max_runtime_ms=run_timeout
            if run_timeout is not None
            else gateway_max_runtime_ms_for(invocable),
        )

    if runtimes is None:
        runtimes = get_runtimes()

    invocable, version = resolve_language(language_token, runtimes)

    if resources is None:
        resources = resources_for(invocable)
    if run_timeout is None:
        run_timeout = run_timeout_for(invocable)

    payload = build_execute_payload(
        source,
        invocable=invocable,
        version=version,
        stdin=stdin,
        resources=resources,
        run_timeout=run_timeout,
    )

    response = piston_post(EXECUTE_PATH, payload)
    data = piston_json(response)
    return parse_execute_response(data, language=invocable, version=version)


def _is_resource_kill(result: "ExecutionResult") -> bool:
    """True if a stage was killed by SIGKILL / exit 137.

    Through session-gateway this is the *only* visible form of a Piston
    ``run_timeout`` kill — the ``signal`` field is flattened away and exit 137 is
    synthesized — so it is indistinguishable from an OOM kill. That ambiguity is
    exactly why the retry is gated on us having chosen the budget.
    """
    for stage in (result.compile, result.run):
        if stage is not None and (stage.signal == "SIGKILL" or stage.code == 137):
            return True
    return result.signal == "SIGKILL" or result.exit_code == 137


def timeout_result(language: str, version: str, detail: str = "") -> ExecutionResult:
    """Build the synthetic :class:`ExecutionResult` for a persistent timeout.

    Returned by :func:`execute_with_cold_start_retry` when a request times out on
    both the first attempt and its single cold-start retry. ``timed_out`` is set
    so the T-005 taxonomy classifies it as a timeout — distinct from a success
    (exit 0) and from a normal failure (a non-zero ``exit_code``).
    """
    return ExecutionResult(
        stdout="",
        stderr=detail,
        exit_code=None,
        signal=None,
        language=language,
        version=version,
        run=None,
        compile=None,
        raw={},
        timed_out=True,
    )


def execute_with_cold_start_retry(
    source: str,
    language_token: str,
    *,
    stdin: str = "",
    runtimes: Optional[Sequence[Mapping]] = None,
    resources: Optional[Mapping[str, Any]] = None,
    run_timeout: Optional[int] = None,
) -> ExecutionResult:
    """Run :func:`execute`, retrying a cold start up to :data:`MAX_COLD_START_RETRIES`.

    **Two** cold-start signals are retried, because through session-gateway they
    are the same event wearing different clothes:

    - a :class:`.errors.PistonTimeoutError` — the transport-level timeout the
      original R5 contract was written against;
    - an **exit-137 / SIGKILL** result, which is what a Piston ``run_timeout``
      kill actually looks like once the gateway flattens the ``signal`` field
      away (see :mod:`.gateway`). Measured on the live deployment: a cold
      ``julia`` returns exit 137 twice and then succeeds on the third attempt,
      on a bare ``print(42)``. Retrying only the timeout would score a working
      runtime as a hard 0.

    **The SIGKILL retry is gated on the budget being the language's own default**
    (``resources is None``). An explicit, caller-chosen budget that gets killed
    is a genuine sizing problem and must surface rather than be retried into
    looking like a slow success — that is the "don't mask real undersizing"
    half of the trade-off. A default-budget kill is ambiguous (timeout vs. OOM,
    indistinguishable through the gateway) and worth one more try.

    Retries are visible, never silent: the returned result carries
    ``attempts`` and ``retry_reasons``, which the seams surface into results
    metadata. A run whose numbers depended on retries can be audited after the
    fact.

    Any other failure (connection error, unknown language, bad status,
    rejection) propagates unchanged — none of those are cold-start transients.
    """
    # Both backends resolve the same way now that the gateway exposes a
    # /runtimes passthrough (self.theseus/gitlab-profile#9) — so a timeout
    # result carries the real invocable name and version on either path.
    if runtimes is None:
        runtimes = get_runtimes()
    invocable, version = resolve_language(language_token, runtimes)

    # Only retry a resource kill when we chose the budget. See the docstring.
    retry_sigkill = resources is None

    last_exc: Optional[PistonTimeoutError] = None
    reasons: list = []

    for attempt in range(1, MAX_COLD_START_RETRIES + 2):
        try:
            result = execute(
                source,
                language_token,
                stdin=stdin,
                runtimes=runtimes,
                resources=resources,
                run_timeout=run_timeout,
            )
        except PistonTimeoutError as exc:
            last_exc = exc
            reasons.append(f"attempt {attempt}: transport timeout")
            continue

        if (
            retry_sigkill
            and attempt <= MAX_COLD_START_RETRIES
            and _is_resource_kill(result)
        ):
            reasons.append(f"attempt {attempt}: exit 137 / SIGKILL (cold start?)")
            continue

        return replace(result, attempts=attempt, retry_reasons=tuple(reasons))

    # Every attempt was a cold-start signal. A persistent transport timeout
    # becomes the timeout result; a persistent SIGKILL is returned as the real
    # (killed) result, so a genuinely undersized language still reads as a kill
    # rather than being relabelled a timeout.
    attempts = MAX_COLD_START_RETRIES + 1
    if last_exc is not None:
        return replace(
            timeout_result(invocable, version, detail=str(last_exc)),
            attempts=attempts,
            retry_reasons=tuple(reasons),
        )
    return replace(result, attempts=attempts, retry_reasons=tuple(reasons))
