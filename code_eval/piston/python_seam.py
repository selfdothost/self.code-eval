"""Opt-in Piston routing for the Python ``exec()`` engines (python-seam kit
R1/R2/R3, T-015 / T-016 / T-018).

Three in-process engines share this shape — candidate + test are ``exec()``d
inside a ``multiprocessing.Process`` guarded by ``reliability_guard()`` and a
``SIGALRM`` time limit:

============================  =================================  ==============
Engine                        Entry point                        Routed here?
============================  =================================  ==============
HumanEval / MBPP              ``custom_metrics/execute.py``      yes
PAL                           ``pal_metric/python_executor.py``  yes
BigCodeBench "beyond"         ``custom_metrics/beyond_eval.py``  no — see below
============================  =================================  ==============

**Assembly (R2).** The kit flags candidate+test assembly as the design risk,
because a test that imports the candidate as a module cannot be reproduced by a
one-shot script. Reading the actual call sites settles it for two of the three
engines: ``compute_code_eval`` already builds ``candidate + "\\n" + test_case``
into ONE string before it ever reaches ``check_correctness``, and PAL's
``program`` is likewise a single string. So there is no module to import and no
assembly to invent — what is needed is a *wrapper* that turns "did this raise?"
into an exit code, which is what :func:`assemble_check_program` does.

The wrapper matters for one specific reason: run bare, a candidate that calls
``sys.exit(0)`` before its tests would exit 0 and read as a pass, whereas the
in-process path catches ``SystemExit`` (a ``BaseException``) and records a
failure. Executing the program inside a ``try/except BaseException`` restores
that semantic exactly, and emits a sentinel line so pass/fail is encoded in both
stdout and the exit code.

The program source is embedded base64-encoded rather than interpolated, so no
combination of quotes, backslashes or triple-quotes in a generated candidate can
break out of the wrapper.

**Why "beyond" is not routed.** ``beyond_eval.Sandbox.unsafe_execute`` is not a
one-shot program: it builds a namespace across ~25 ``exec`` calls, mutates it
per test case, times the loop, and needs ``lctk`` and ``sortedcontainers``
imported inside that namespace. Piston's stock python runtime has neither, and
adding Piston-side packages is explicitly out of scope for this work (it is a
``self.theseus/piston-repo`` ask). Rather than silently keep executing untrusted
code in-container while the operator believes Piston is on,
:func:`require_supported_engine` raises. See ``selfai/gitlab-profile#22``.
"""

import base64
from typing import Any, Dict, Mapping, Optional, Sequence

from .config import piston_enabled
from .errors import PistonError, UnsupportedEngineError
from .execute import ExecutionResult, execute_with_cold_start_retry
from .sizing import PISTON_RUN_TIMEOUT_CAP_MS
from .taxonomy import ResultClass, classify, classify_exception

# Sentinel lines the wrapper prints. Pass/fail is corroborated by BOTH the
# sentinel and the exit code, so a candidate cannot fake a pass by printing.
PASS_SENTINEL = "__PISTON_CHECK_PASSED__"
FAIL_SENTINEL = "__PISTON_CHECK_FAILED__"
ANSWER_SENTINEL = "__PISTON_ANSWER__"

# The harness's own result strings (execute.py / python_executor.py), preserved
# so downstream consumers that string-match keep working.
RESULT_PASSED = "passed"
RESULT_TIMED_OUT = "timed out"
RESULT_PAL_TIMED_OUT = "failed: timed out"

# Engines with an in-process shape this seam can reproduce one-shot.
SUPPORTED_ENGINES = frozenset({"check_correctness", "pal"})

BEYOND_UNSUPPORTED_REASON = (
    "The 'beyond' engine (beyond_eval.Sandbox) cannot be routed through Piston: "
    "it is not a one-shot program — it builds a namespace across ~25 exec calls, "
    "mutates it per test case, and requires the lctk and sortedcontainers "
    "packages inside that namespace, which Piston's stock python runtime does "
    "not provide. Adding Piston-side packages is a self.theseus/piston-repo ask "
    "(see selfai/gitlab-profile#22), not harness work. Run beyond evals with "
    "ENABLE_PISTON_EXECUTION off, accepting that they execute in-container."
)


def should_route_to_piston() -> bool:
    """True when the Python ``exec()`` engines should execute via Piston."""
    return piston_enabled()


def require_supported_engine(engine: str) -> None:
    """Raise :class:`~.errors.UnsupportedEngineError` for an unroutable engine.

    Failing loudly is deliberate: silently falling back to the in-process path
    would keep running untrusted generated code in-container while the operator
    believes the sandbox is on — the exact thing this work exists to fix.
    """
    if engine not in SUPPORTED_ENGINES:
        detail = BEYOND_UNSUPPORTED_REASON if engine == "beyond" else ""
        raise UnsupportedEngineError(
            f"Engine {engine!r} has no Piston-routable shape. {detail}".strip()
        )


def run_timeout_ms_for(timeout_seconds: float) -> int:
    """Map a per-problem timeout (seconds) to a Piston ``run_timeout`` (ms).

    Clamped to Piston's hard global cap. HumanEval/MBPP's default per-problem
    timeout is 3.0 s, which lands exactly on the 3000 ms cap, so the common case
    is genuine parity; a harness configured above 3 s is clamped down.
    """
    return min(int(float(timeout_seconds) * 1000), PISTON_RUN_TIMEOUT_CAP_MS)


def assemble_check_program(check_program: str) -> str:
    """Wrap an already-self-contained candidate+test program for one-shot execution.

    The result is a single Python script that:

    - decodes the original program from an embedded base64 blob (so nothing in
      the generated source can break out of the wrapper's quoting);
    - executes it with ``__name__ == "__main__"``, matching a bare script run;
    - catches ``BaseException`` — including ``SystemExit``, which the in-process
      path treats as a failure — and exits 1 with the failure sentinel;
    - exits 0 with the pass sentinel only if the program ran to completion.
    """
    encoded = base64.b64encode(check_program.encode("utf-8")).decode("ascii")
    return (
        "import base64, sys, traceback\n"
        f'_src = base64.b64decode("{encoded}").decode("utf-8")\n'
        "try:\n"
        '    _g = {"__name__": "__main__"}\n'
        '    exec(compile(_src, "<candidate>", "exec"), _g)\n'
        "except BaseException as _exc:\n"
        "    traceback.print_exc(file=sys.stderr)\n"
        f'    sys.stdout.write("{FAIL_SENTINEL} " + type(_exc).__name__ + ": " + str(_exc) + "\\n")\n'
        "    sys.stdout.flush()\n"
        "    sys.exit(1)\n"
        f'sys.stdout.write("{PASS_SENTINEL}\\n")\n'
        "sys.stdout.flush()\n"
        "sys.exit(0)\n"
    )


def assemble_pal_program(program: str, answer_symbol: Optional[str] = None) -> str:
    """Wrap a PAL program so its answer can be read back off stdout.

    PAL's in-process path reads the answer either from a named global
    (``answer_symbol``) or from the last line the program printed. Neither
    survives a one-shot execution as-is, so the wrapper prints the answer on a
    sentinel-prefixed final line: ``repr()`` when read from a global (so the
    value round-trips through :func:`ast.literal_eval`), or the program's own
    last stdout line otherwise.
    """
    encoded = base64.b64encode(program.encode("utf-8")).decode("ascii")
    symbol_literal = repr(answer_symbol) if answer_symbol else "None"
    return (
        "import base64, io, sys, traceback, contextlib\n"
        f'_src = base64.b64decode("{encoded}").decode("utf-8")\n'
        f"_symbol = {symbol_literal}\n"
        "_buf = io.StringIO()\n"
        "try:\n"
        "    _g = {}\n"
        "    with contextlib.redirect_stdout(_buf):\n"
        '        exec(compile(_src, "<pal>", "exec"), _g)\n'
        "except BaseException as _exc:\n"
        "    traceback.print_exc(file=sys.stderr)\n"
        f'    sys.stdout.write("{FAIL_SENTINEL} " + type(_exc).__name__ + ": " + str(_exc) + "\\n")\n'
        "    sys.stdout.flush()\n"
        "    sys.exit(1)\n"
        "if _symbol is not None:\n"
        "    _answer = repr(_g.get(_symbol))\n"
        "else:\n"
        "    _lines = [l for l in _buf.getvalue().splitlines() if l.strip()]\n"
        '    _answer = _lines[-1].strip() if _lines else ""\n'
        f'sys.stdout.write("{ANSWER_SENTINEL} " + _answer + "\\n")\n'
        "sys.stdout.flush()\n"
        "sys.exit(0)\n"
    )


def _last_sentinel_line(stdout: str, sentinel: str) -> Optional[str]:
    """Return the payload of the last line starting with ``sentinel``, if any."""
    for line in reversed((stdout or "").splitlines()):
        if line.startswith(sentinel):
            return line[len(sentinel) :].strip()
    return None


def _piston_metadata(result: Optional[ExecutionResult] = None) -> Dict[str, Any]:
    """Reproducibility metadata (client kit R6 / T-008), Piston path only."""
    metadata: Dict[str, Any] = {"execution_backend": "piston"}
    if result is not None:
        metadata["piston_runtime"] = result.language
        metadata["piston_runtime_version"] = result.version
        # Only when a retry actually fired — a result that depended on one must
        # say so rather than letting the policy silently move a score.
        if result.attempts > 1:
            metadata["piston_attempts"] = result.attempts
            metadata["piston_retry_reasons"] = list(result.retry_reasons)
    return metadata


def interpret_check_result(result: ExecutionResult) -> Dict[str, Any]:
    """Map an :class:`ExecutionResult` onto ``check_correctness``'s pass/fail.

    ``passed`` requires BOTH exit 0 (a clean run) and the pass sentinel, so a
    candidate that prints the sentinel and then fails is not scored as a pass.
    """
    classification = classify(result)

    if classification is ResultClass.TIMEOUT:
        return {"passed": False, "result": RESULT_TIMED_OUT}

    if classification is ResultClass.PASS:
        if _last_sentinel_line(result.stdout, PASS_SENTINEL) is not None:
            return {"passed": True, "result": RESULT_PASSED}
        # Exit 0 without the sentinel means the wrapper never finished — treat
        # it as a failure rather than a pass we cannot account for.
        return {
            "passed": False,
            "result": "failed: program exited 0 without completing the check wrapper",
        }

    if classification is ResultClass.SIGKILL:
        return {
            "passed": False,
            "result": "failed: killed by the sandbox (SIGKILL / exit 137) — "
            "a run_timeout overrun or an undersized resource budget, not a "
            "test failure",
        }

    detail = _last_sentinel_line(result.stdout, FAIL_SENTINEL)
    if detail is None:
        detail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        detail = detail[0]
    return {"passed": False, "result": f"failed: {detail}"}


def check_correctness_piston(
    check_program: str,
    timeout: float,
    task_id: Any,
    completion_id: Any,
    *,
    runtimes: Optional[Sequence[Mapping]] = None,
) -> Dict[str, Any]:
    """Piston-backed drop-in for ``custom_metrics.execute.check_correctness``.

    Returns the same ``{task_id, passed, result, completion_id}`` dict, plus the
    Piston reproducibility metadata. A Piston failure that never produced a
    verdict is reported with a ``piston error:`` result string — non-pass, but
    distinguishable from a candidate that ran and failed its tests.
    """
    program = assemble_check_program(check_program)
    try:
        result = execute_with_cold_start_retry(
            program,
            "python",
            runtimes=runtimes,
            run_timeout=run_timeout_ms_for(timeout),
        )
    except PistonError as exc:
        return dict(
            task_id=task_id,
            passed=False,
            result=f"piston error: {type(exc).__name__}: {exc}",
            completion_id=completion_id,
            piston_result_class=classify_exception(exc).value,
            **_piston_metadata(),
        )

    verdict = interpret_check_result(result)
    return dict(
        task_id=task_id,
        passed=verdict["passed"],
        result=verdict["result"],
        completion_id=completion_id,
        piston_result_class=classify(result).value,
        **_piston_metadata(result),
    )


def run_program_piston(
    program: str,
    timeout: float,
    task_id: Any,
    completion_id: Any,
    answer_symbol: Optional[str] = None,
    *,
    runtimes: Optional[Sequence[Mapping]] = None,
) -> Dict[str, Any]:
    """Piston-backed drop-in for ``pal_metric.python_executor.run_program``.

    Returns the same ``{task_id, result, completion_id}`` dict. ``result`` is the
    answer string on success and a ``failed: ...`` string otherwise, matching the
    in-process path's vocabulary (including its ``"failed: timed out"``).
    """
    assembled = assemble_pal_program(program, answer_symbol)
    try:
        execution = execute_with_cold_start_retry(
            assembled,
            "python",
            runtimes=runtimes,
            run_timeout=run_timeout_ms_for(timeout),
        )
    except PistonError as exc:
        return dict(
            task_id=task_id,
            result=f"failed: piston error: {type(exc).__name__}: {exc}",
            completion_id=completion_id,
            **_piston_metadata(),
        )

    classification = classify(execution)
    if classification is ResultClass.TIMEOUT:
        result_value: Any = RESULT_PAL_TIMED_OUT
    elif classification is ResultClass.PASS:
        answer = _last_sentinel_line(execution.stdout, ANSWER_SENTINEL)
        result_value = answer if answer is not None else "failed: no answer emitted"
    else:
        detail = _last_sentinel_line(execution.stdout, FAIL_SENTINEL)
        if detail is None:
            tail = (execution.stderr or "").strip().splitlines()
            detail = tail[-1] if tail else f"exit {execution.exit_code}"
        result_value = f"failed: {detail}"

    return dict(
        task_id=task_id,
        result=result_value,
        completion_id=completion_id,
        **_piston_metadata(execution),
    )
