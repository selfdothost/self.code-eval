"""Result taxonomy / error-signal path (cavekit R1 / T-005).

A **separate classification layer** over :class:`.execute.ExecutionResult`. It is
deliberately NOT folded into the response parser: the parser (:mod:`.execute`)
hoists the raw distinct fields (``exit_code`` / ``signal`` / ``run`` /
``compile`` / ``timed_out``); this module turns those raw facts into a single
verdict a harness seam can score on.

The contract this backs (R1): a flag-on run against an unreachable/bad
``PISTON_BASE_URL``, or any execution failure, must surface a *clear signal* —
never a silent 0-score indistinguishable from a genuine failing test. That is
enforced two ways:

- :func:`classify` maps a parsed result to one of PASS / FAIL / TIMEOUT /
  SIGKILL / COMPILE_ERROR — so an infra-shaped failure (timeout, an
  undersized-build SIGKILL, a compile error) is never conflated with a genuine
  test FAIL, and only PASS scores as passing.
- :func:`classify_exception` maps the named Piston errors (connection / not
  enabled / unknown language / transport timeout) to ERROR / TIMEOUT, so a
  raised failure is a distinct verdict too, not a mute 0.

:func:`verdict_for` / :func:`safe_verdict` are the convenience entry points the
seams call.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .errors import (
    PistonError,
    PistonTimeoutError,
)
from .execute import ExecutionResult, PistonStage


class ResultClass(str, Enum):
    """The mutually-exclusive outcome classes for one execution.

    ``str``-valued so a class serializes as a stable token in results metadata.
    Only :attr:`PASS` counts as a passing score; every other class is a distinct
    non-pass signal, so an execution failure is never a silent 0 that looks like
    a genuine test failure.
    """

    PASS = "pass"
    FAIL = "fail"
    TIMEOUT = "timeout"
    SIGKILL = "sigkill"
    COMPILE_ERROR = "compile_error"
    # A raised failure (unreachable/bad URL, disabled flag, unknown language,
    # bad response) — never got a verdict from Piston at all.
    ERROR = "error"


# The classes that mean "we did not get a clean test verdict" — the harness must
# treat these as an infra/execution signal, NOT as a genuine failing test.
NON_TEST_FAILURE_CLASSES = frozenset(
    {
        ResultClass.TIMEOUT,
        ResultClass.SIGKILL,
        ResultClass.COMPILE_ERROR,
        ResultClass.ERROR,
    }
)


@dataclass(frozen=True)
class Verdict:
    """A scorable verdict: the class, whether it passed, and a human detail.

    ``passed`` is ``True`` only for :attr:`ResultClass.PASS`. ``is_execution_error``
    is ``True`` for the non-test-failure classes so a caller can tell "the test
    genuinely failed" (FAIL) apart from "execution broke" (TIMEOUT / SIGKILL /
    COMPILE_ERROR / ERROR).
    """

    classification: ResultClass
    passed: bool
    detail: str
    result: Optional[ExecutionResult] = None

    @property
    def is_execution_error(self) -> bool:
        return self.classification in NON_TEST_FAILURE_CLASSES


def _stage_is_sigkill(stage: Optional[PistonStage]) -> bool:
    """True if a stage was killed by SIGKILL / exited 137 (OOM / resource kill)."""
    if stage is None:
        return False
    if stage.signal == "SIGKILL":
        return True
    return stage.code == 137


def classify(result: ExecutionResult) -> ResultClass:
    """Classify a parsed :class:`ExecutionResult` into a :class:`ResultClass`.

    Order matters — the first matching rule wins:

    1. :attr:`~ResultClass.TIMEOUT` — the synthetic ``timed_out`` result from the
       T-007 retry policy (a persistent transport timeout).
    2. :attr:`~ResultClass.SIGKILL` — any stage killed by SIGKILL / exit 137.
       Checked before COMPILE_ERROR so an *undersized compiled build* (R4) that
       SIGKILLs at compile is diagnosable as a resource kill, not a plain
       compile error.
    3. :attr:`~ResultClass.COMPILE_ERROR` — a compile stage exited non-zero
       (build failed for a reason other than a resource kill).
    4. :attr:`~ResultClass.PASS` — exit code 0 with no compile/run failure.
    5. :attr:`~ResultClass.FAIL` — anything else (a genuine non-zero test exit).
    """
    if result.timed_out:
        return ResultClass.TIMEOUT

    # SIGKILL on either stage (checked before compile-error so an undersized
    # build's OOM kill is not mistaken for an ordinary compile failure).
    if _stage_is_sigkill(result.compile) or _stage_is_sigkill(result.run):
        return ResultClass.SIGKILL
    if result.signal == "SIGKILL" or result.exit_code == 137:
        return ResultClass.SIGKILL

    if result.compile_failed:
        return ResultClass.COMPILE_ERROR

    if result.exit_code == 0 and result.signal is None:
        return ResultClass.PASS

    return ResultClass.FAIL


def classify_exception(exc: BaseException) -> ResultClass:
    """Classify a raised Piston failure into a :class:`ResultClass`.

    A transport timeout maps to :attr:`~ResultClass.TIMEOUT`; every other Piston
    error (unreachable/bad URL, disabled flag, unknown language, bad response)
    maps to :attr:`~ResultClass.ERROR`. Either way it is a distinct, non-pass
    signal — never a silent 0.
    """
    if isinstance(exc, PistonTimeoutError):
        return ResultClass.TIMEOUT
    return ResultClass.ERROR


def verdict_for(result: ExecutionResult) -> Verdict:
    """Wrap :func:`classify` into a :class:`Verdict` (passed / detail / result)."""
    cls = classify(result)
    detail = _detail_for(cls, result)
    return Verdict(
        classification=cls,
        passed=cls is ResultClass.PASS,
        detail=detail,
        result=result,
    )


def verdict_for_exception(exc: BaseException) -> Verdict:
    """Wrap :func:`classify_exception` into a non-pass :class:`Verdict`."""
    cls = classify_exception(exc)
    return Verdict(
        classification=cls,
        passed=False,
        detail=f"{type(exc).__name__}: {exc}",
        result=None,
    )


def safe_verdict(fn, *args, **kwargs) -> Verdict:
    """Call ``fn(*args, **kwargs)`` and always return a :class:`Verdict`.

    A returned :class:`ExecutionResult` is classified via :func:`verdict_for`; a
    raised :class:`.errors.PistonError` becomes a non-pass verdict via
    :func:`verdict_for_exception`. This is the never-a-silent-0 entry point for a
    seam that wants a verdict object no matter how the execution ended. Non-Piston
    exceptions propagate (they are bugs, not execution outcomes).
    """
    try:
        result = fn(*args, **kwargs)
    except PistonError as exc:
        return verdict_for_exception(exc)
    return verdict_for(result)


def _detail_for(cls: ResultClass, result: ExecutionResult) -> str:
    """Build a short human-readable detail line for a verdict."""
    if cls is ResultClass.PASS:
        return f"{result.language} {result.version}: exit 0"
    if cls is ResultClass.TIMEOUT:
        return f"{result.language} {result.version}: timed out"
    if cls is ResultClass.SIGKILL:
        # Two different causes produce an identical exit 137, and on the
        # session-gateway path they are genuinely indistinguishable: an
        # undersized resource budget (OOM kill) and a run_timeout overrun, which
        # Piston also enforces with SIGKILL. The gateway drops the `signal`
        # field and reports no timing, so name both rather than assert one.
        return (
            f"{result.language} {result.version}: SIGKILL / exit 137 — "
            "either a run_timeout overrun or an undersized resource budget "
            "(indistinguishable on the gateway path; see R4 sizing)"
        )
    if cls is ResultClass.COMPILE_ERROR:
        stderr = result.compile.stderr if result.compile else ""
        return f"{result.language} {result.version}: compile error: {stderr[:200]}"
    return f"{result.language} {result.version}: exit {result.exit_code}"
