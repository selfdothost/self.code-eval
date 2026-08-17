"""Opt-in Piston routing for the MultiPL-E seam (multiple-seam kit R1/R3/R4,
T-011 / T-012 / T-013).

``containerized_eval.eval_string_script`` is the single per-problem entry point
for every ``multiple-*`` task: it takes a language token plus one assembled
program (candidate + tests) and returns a result dict the harness scores on. That
is exactly Piston's one-shot execute contract, so this module is the whole seam.

Design rules this module holds to:

- **Flag off is untouched.** Nothing here runs unless
  ``ENABLE_PISTON_EXECUTION`` is on; ``containerized_eval`` calls
  :func:`should_route_to_piston` first and otherwise takes the unmodified local
  ``eval_*.py`` / ``safe_subprocess`` path.
- **Same result shape.** :func:`eval_string_script_piston` returns the same
  ``{program, stdout, stderr, exit_code, status}`` dict, with ``status`` drawn
  from the harness's own vocabulary (``OK`` / ``Exception`` / ``Timeout`` /
  ``SyntaxError``), so downstream scoring
  (``single_experiment_pass_k``/``multiple.py``: ``status == "OK" and
  exit_code == 0``) needs no change.
- **Never a silent 0.** An execution that never produced a verdict (unreachable
  endpoint, disabled flag, unresolvable runtime) gets the distinct
  :data:`STATUS_PISTON_ERROR` status, and a resource kill gets
  :data:`STATUS_SIGKILL` — both score as non-pass (correct) while staying
  visibly different from a candidate that ran and failed its tests.
- **Executor gate (R3).** A language with no ``eval_*.py`` executor cannot be
  scored by this harness at all; :func:`require_executor` raises
  :class:`~.errors.MissingExecutorError` rather than recording a 0. The gate is
  derived from the live ``EVALUATORS`` table, not a hard-coded list — see
  :func:`executor_backed_tokens`.

.. warning::
   **Timeout parity is not exact, by design.** The local path gives a program up
   to 15 s (``safe_subprocess.run``) or 5 s (``libeval.run_without_exn``);
   Piston enforces a hard 3000 ms ``run_timeout`` cap unless theseus raises
   ``PISTON_RUN_TIMEOUT`` globally. :func:`run_timeout_ms_for` therefore clamps,
   which means a slow-but-correct generation can pass locally and time out on
   Piston. This is the known live constraint tracked on ``selfai/gitlab-profile#22``
   — it is *named* here, not silently absorbed.
"""

from typing import Any, Dict, FrozenSet, Mapping, Optional, Sequence

from .config import piston_enabled
from .errors import MissingExecutorError, PistonError
from .execute import ExecutionResult, execute_with_cold_start_retry
from .sizing import PISTON_RUN_TIMEOUT_CAP_MS
from .taxonomy import ResultClass, classify, classify_exception

# ─── Result status vocabulary ───────────────────────────────────────────────
#
# The first four are the harness's own statuses (see multiple_metrics/libeval.py
# and the eval_*.py executors); scoring counts only "OK" + exit 0 as a pass. The
# last two are Piston-only additions: they score as non-pass like any other
# non-"OK" status, but keep an execution failure visibly distinct from a
# candidate that genuinely failed its tests.
STATUS_OK = "OK"
STATUS_EXCEPTION = "Exception"
STATUS_TIMEOUT = "Timeout"
STATUS_SYNTAX_ERROR = "SyntaxError"
STATUS_SIGKILL = "SIGKILL"
STATUS_PISTON_ERROR = "PistonError"

# Truncation the local seam applies to captured output; mirrored so the two
# paths produce comparably sized results.
MAX_OUTPUT_BYTES = 2048

# ─── Harness per-problem timeouts (R4 pass-through) ─────────────────────────
#
# The harness has no single configurable per-problem timeout: each executor
# hard-codes its own. These are the values actually in the source —
# `safe_subprocess.run(timeout_seconds=15)` for the executors built on it,
# `libeval.run_without_exn`'s `communicate(timeout=5)` for the rest, and the
# handful of executors with an explicit `subprocess.run(timeout=...)`.
# `run_timeout_ms_for` converts to ms and clamps to Piston's cap.
DEFAULT_HARNESS_TIMEOUT_SECONDS = 15
HARNESS_TIMEOUT_SECONDS: Dict[str, int] = {
    "cs": 5,
    "java": 5,
    "scala": 5,
    "clojure": 5,
    "dart": 5,
    "elixir": 5,
    "haskell": 5,
    "ocaml": 5,
}


def should_route_to_piston() -> bool:
    """True when this seam should execute via Piston instead of locally.

    Reads the code-eval service's own ``ENABLE_PISTON_EXECUTION`` (client kit
    R1) — never the FastAPI backend's Tools-scoped pair.
    """
    return piston_enabled()


def executor_backed_tokens(evaluators: Optional[Mapping[str, Any]] = None) -> FrozenSet[str]:
    """Return every language token the harness has an ``eval_*.py`` executor for.

    Derived from ``containerized_eval.EVALUATORS`` (imported lazily to avoid an
    import cycle — ``containerized_eval`` imports this module). Deriving it beats
    hard-coding: the executor set has already changed once (the five that landed
    with the multiple-runtimes merge took it from 19 to 24 files), and a
    hard-coded gate would have silently blocked languages that now work.

    Pass ``evaluators`` to gate against an explicit table instead (tests do).
    """
    if evaluators is None:
        from ..tasks.custom_metrics.multiple_metrics.containerized_eval import (
            EVALUATORS,
        )

        evaluators = EVALUATORS
    return frozenset(str(token).strip().lower() for token in evaluators)


def require_executor(
    language: str, evaluators: Optional[Mapping[str, Any]] = None
) -> None:
    """Raise :class:`~.errors.MissingExecutorError` if ``language`` has no executor.

    The R3 gate. A language the harness registers but cannot score must fail
    loudly — recording a 0 would be indistinguishable from a candidate that ran
    and failed every test.
    """
    token = (language or "").strip().lower()
    backed = executor_backed_tokens(evaluators)
    if token not in backed:
        raise MissingExecutorError(
            f"MultiPL-E language {language!r} has no eval_*.py executor in the "
            f"harness, so it cannot be scored through this seam (with or without "
            f"Piston). Known executor-backed tokens: {', '.join(sorted(backed))}."
        )


def run_timeout_ms_for(language: str, timeout_seconds: Optional[int] = None) -> int:
    """Map the harness's per-problem timeout to a Piston ``run_timeout`` in ms.

    ``timeout_seconds`` overrides the per-language harness default. The result is
    clamped to :data:`~.sizing.PISTON_RUN_TIMEOUT_CAP_MS` — the client never
    assumes a per-request raise above Piston's global cap. See this module's
    warning: the clamp is a real behavioural difference from the local path, not
    an equivalence.
    """
    if timeout_seconds is None:
        timeout_seconds = HARNESS_TIMEOUT_SECONDS.get(
            (language or "").strip().lower(), DEFAULT_HARNESS_TIMEOUT_SECONDS
        )
    return min(int(timeout_seconds) * 1000, PISTON_RUN_TIMEOUT_CAP_MS)


def status_for(classification: ResultClass) -> str:
    """Map a taxonomy class to the harness's result ``status`` string."""
    return {
        ResultClass.PASS: STATUS_OK,
        ResultClass.FAIL: STATUS_EXCEPTION,
        ResultClass.TIMEOUT: STATUS_TIMEOUT,
        ResultClass.COMPILE_ERROR: STATUS_SYNTAX_ERROR,
        ResultClass.SIGKILL: STATUS_SIGKILL,
        ResultClass.ERROR: STATUS_PISTON_ERROR,
    }[classification]


def _piston_metadata(
    language: Optional[str] = None, version: Optional[str] = None
) -> Dict[str, Any]:
    """Reproducibility metadata for a Piston-backed result (client kit R6 / T-008).

    ``execution_backend`` is the marker distinguishing a Piston-backed execution
    from an in-container one; ``piston_runtime`` / ``piston_runtime_version``
    record the runtime that actually ran the program, so a score is reproducible
    against a known package version. These keys are added ONLY on the Piston
    path — with the flag off the local result dict is untouched, which is what
    keeps the off-path byte-identical.
    """
    metadata: Dict[str, Any] = {"execution_backend": "piston"}
    if language:
        metadata["piston_runtime"] = language
    if version:
        metadata["piston_runtime_version"] = version
    return metadata


def _retry_metadata(result: Optional[ExecutionResult]) -> Dict[str, Any]:
    """Retry provenance, recorded only when a retry actually happened.

    A cold-start retry can turn a scored 0 into a pass, so a result that
    depended on one must say so — otherwise the retry policy is an invisible
    thumb on the scale. Absent keys mean "first attempt, no retry".
    """
    if result is None or result.attempts <= 1:
        return {}
    return {
        "piston_attempts": result.attempts,
        "piston_retry_reasons": list(result.retry_reasons),
    }


def _result_dict(
    program: str,
    *,
    stdout: str,
    stderr: str,
    exit_code: Optional[int],
    status: str,
    language: Optional[str] = None,
    version: Optional[str] = None,
    retry: Optional[ExecutionResult] = None,
) -> Dict[str, Any]:
    """Build the seam's result dict in the harness's existing shape."""
    result = {
        "program": program,
        "stdout": (stdout or "").replace("!!int", "")[:MAX_OUTPUT_BYTES],
        "stderr": (stderr or "")[:MAX_OUTPUT_BYTES],
        "exit_code": exit_code,
        "status": status,
    }
    result.update(_piston_metadata(language, version))
    result.update(_retry_metadata(retry))
    return result


def result_to_harness_dict(program: str, result: ExecutionResult) -> Dict[str, Any]:
    """Convert a parsed :class:`ExecutionResult` into the seam's result dict."""
    status = status_for(classify(result))

    # Match the local executors' own convention for a program that never got off
    # the ground. `eval_python.py` does exactly this — `"SyntaxError" in stderr`
    # -> status "SyntaxError" — and several other executors report a failed
    # build the same way. Without it, the gateway path (which cannot see a
    # separate compile stage) reports a plain "Exception" where the local path
    # says "SyntaxError". Scoring is unaffected either way, but keeping the
    # status strings aligned means the two paths' results read identically.
    if status == STATUS_EXCEPTION and "SyntaxError" in (result.stderr or ""):
        status = STATUS_SYNTAX_ERROR

    return _result_dict(
        program,
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        status=status,
        language=result.language,
        version=result.version,
        retry=result,
    )


def error_to_harness_dict(program: str, exc: BaseException) -> Dict[str, Any]:
    """Convert a raised Piston failure into a non-pass result dict.

    Carries the error text in ``stderr`` and a distinct ``status`` so the failure
    is legible in the results JSON instead of vanishing into a 0.
    """
    classification = classify_exception(exc)
    return _result_dict(
        program,
        stdout="",
        stderr=f"{type(exc).__name__}: {exc}",
        exit_code=None,
        status=status_for(classification),
    )


def eval_string_script_piston(
    language: str,
    program: str,
    *,
    timeout_seconds: Optional[int] = None,
    runtimes: Optional[Sequence[Mapping]] = None,
    evaluators: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one MultiPL-E program in Piston, returning the harness's result dict.

    Steps: executor gate (R3) -> per-language ``run_timeout`` (R4) -> execute with
    the single cold-start retry (client R5) -> classify -> harness dict. Per-language
    resource sizing (R4) is applied inside the client, off the resolved invocable
    name, so a compiled language gets its larger budget end-to-end.

    Any :class:`~.errors.PistonError` becomes a non-pass result dict with a
    distinct status rather than propagating — a single unreachable execute must
    not abort a whole eval run — except :class:`~.errors.MissingExecutorError`,
    which propagates: it is a harness-coverage bug, not an execution outcome.
    """
    require_executor(language, evaluators)

    run_timeout = run_timeout_ms_for(language, timeout_seconds)
    try:
        result = execute_with_cold_start_retry(
            program,
            language,
            runtimes=runtimes,
            run_timeout=run_timeout,
        )
    except PistonError as exc:
        return error_to_harness_dict(program, exc)
    return result_to_harness_dict(program, result)
