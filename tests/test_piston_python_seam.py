"""Tests for the Python ``exec()`` seam (python-seam kit R1/R2/R3,
T-015 / T-016 / T-018).

Covers:
- Flag off -> the in-process ``multiprocessing`` + ``exec()`` path, unchanged.
- Flag on -> candidate+test executes via Piston, with the same result dict shape.
- Candidate+test assembly: the wrapper is one self-contained script whose exit
  code and stdout sentinel deterministically encode pass vs. fail. These are
  verified by ACTUALLY RUNNING the assembled program under the local
  interpreter — the closest stand-in available for the ``[needs-live-endpoint]``
  parity criteria, since it exercises the real generated source rather than a
  mock of it.
- Timeout pass-through and the cold-start retry reaching this seam.
- The "beyond" engine fails loudly instead of silently running in-container.
"""

import ast
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock

import pytest
import requests

from code_eval.piston import (
    ANSWER_SENTINEL,
    MAX_COLD_START_RETRIES,
    FAIL_SENTINEL,
    PASS_SENTINEL,
    UnsupportedEngineError,
    assemble_check_program,
    assemble_pal_program,
    check_correctness_piston,
    clear_runtimes_cache,
    interpret_check_result,
    parse_execute_response,
    require_supported_engine,
    run_program_piston,
    timeout_result,
)
from code_eval.piston import client as piston_client
from code_eval.piston import python_seam

RUNTIMES = [{"language": "python", "version": "3.10.0", "aliases": ["py", "py3"]}]

PASSING = "def add(a, b):\n    return a + b\n\nassert add(1, 2) == 3\n"
FAILING = "def add(a, b):\n    return a - b\n\nassert add(1, 2) == 3\n"


def _fake_response(json_body, *, status_code=200):
    resp = MagicMock(name="response")
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


def _run_locally(program, timeout=20):
    """Execute an assembled wrapper with the local interpreter.

    Stands in for the Piston runtime: same one-shot contract (source in, stdout +
    exit code out), so the assembly's pass/fail encoding is verified against real
    execution rather than a mocked response.
    """
    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc


def _piston_result(stdout, code, *, signal=None):
    return parse_execute_response(
        {"run": {"stdout": stdout, "stderr": "", "code": code, "signal": signal}},
        language="python",
        version="3.10.0",
    )


@pytest.fixture(autouse=True)
def _reset_runtimes_cache():
    clear_runtimes_cache()
    yield
    clear_runtimes_cache()


@pytest.fixture
def piston_on(monkeypatch):
    monkeypatch.setenv("ENABLE_PISTON_EXECUTION", "1")
    # These suites exercise the DIRECT Piston transport. The service default
    # is the session-gateway backend, so pin it here rather than depending on
    # ambient env — gateway coverage lives in test_piston_gateway.py.
    monkeypatch.setenv("PISTON_BACKEND", "direct")
    monkeypatch.setenv(
        "PISTON_BASE_URL", "http://piston.self-theseus.svc.cluster.local:2000"
    )


@pytest.fixture
def piston_off(monkeypatch):
    monkeypatch.delenv("ENABLE_PISTON_EXECUTION", raising=False)


# ─── R2: assembly is one self-contained program, run for real ──────────────


def test_assembled_program_is_self_contained():
    program = assemble_check_program(PASSING)
    # No separate importable candidate module: the source is embedded inline.
    assert "import base64" in program
    assert "b64decode" in program
    # The raw candidate text is NOT interpolated (so quoting cannot break out).
    assert "def add" not in program


def test_assembled_passing_program_exits_zero_with_the_pass_sentinel():
    proc = _run_locally(assemble_check_program(PASSING))
    assert proc.returncode == 0
    assert PASS_SENTINEL in proc.stdout


def test_assembled_failing_program_exits_nonzero_with_the_fail_sentinel():
    proc = _run_locally(assemble_check_program(FAILING))
    assert proc.returncode == 1
    assert FAIL_SENTINEL in proc.stdout
    assert PASS_SENTINEL not in proc.stdout
    assert "AssertionError" in proc.stdout


def test_quotes_and_backslashes_in_a_candidate_survive_assembly():
    nasty = textwrap.dedent(
        '''
        s = """triple " quoted \\\\ backslash '''
        + "'''"
        + '''"""
        assert len(s) > 0
        '''
    )
    proc = _run_locally(assemble_check_program(nasty))
    assert proc.returncode == 0, proc.stderr
    assert PASS_SENTINEL in proc.stdout


def test_candidate_calling_sys_exit_zero_is_not_scored_as_a_pass():
    # The in-process path catches SystemExit (a BaseException) and records a
    # failure; run bare, this candidate would exit 0 and read as a pass. The
    # wrapper is what preserves the in-process semantic.
    sneaky = "import sys\nsys.exit(0)\nassert False\n"
    proc = _run_locally(assemble_check_program(sneaky))
    assert proc.returncode == 1
    assert FAIL_SENTINEL in proc.stdout
    assert "SystemExit" in proc.stdout


def test_candidate_runs_as_main():
    program = "import sys\nassert __name__ == '__main__'\n"
    proc = _run_locally(assemble_check_program(program))
    assert proc.returncode == 0


def test_candidate_stdout_does_not_hide_the_sentinel():
    noisy = "print('lots')\nprint('of output')\n"
    proc = _run_locally(assemble_check_program(noisy))
    assert proc.returncode == 0
    # The sentinel is the LAST sentinel line, so candidate output cannot bury it.
    assert proc.stdout.strip().splitlines()[-1] == PASS_SENTINEL


# ─── R2: mapping an execution result back to pass/fail ─────────────────────


def test_pass_requires_both_exit_zero_and_the_sentinel():
    good = _piston_result(f"{PASS_SENTINEL}\n", 0)
    assert interpret_check_result(good) == {"passed": True, "result": "passed"}

    # Exit 0 but no sentinel: the wrapper never completed — not a pass.
    silent = _piston_result("", 0)
    assert interpret_check_result(silent)["passed"] is False


def test_a_printed_sentinel_cannot_fake_a_pass():
    # Candidate prints the pass sentinel then fails: non-zero exit wins.
    faked = _piston_result(f"{PASS_SENTINEL}\n{FAIL_SENTINEL} AssertionError: x\n", 1)
    verdict = interpret_check_result(faked)
    assert verdict["passed"] is False
    assert "AssertionError" in verdict["result"]


def test_failure_detail_is_carried_into_the_result_string():
    failed = _piston_result(f"{FAIL_SENTINEL} AssertionError: boom\n", 1)
    verdict = interpret_check_result(failed)
    assert verdict["passed"] is False
    assert verdict["result"].startswith("failed:")
    assert "boom" in verdict["result"]


def test_timeout_maps_to_the_harness_timed_out_string():
    verdict = interpret_check_result(timeout_result("python", "3.10.0"))
    assert verdict == {"passed": False, "result": "timed out"}


def test_sigkill_is_distinguishable_from_a_test_failure():
    killed = _piston_result("", 137, signal="SIGKILL")
    verdict = interpret_check_result(killed)
    assert verdict["passed"] is False
    assert "SIGKILL" in verdict["result"]


# ─── R1: routing ───────────────────────────────────────────────────────────


def test_flag_off_does_not_route(piston_off):
    assert python_seam.should_route_to_piston() is False


def test_flag_on_routes(piston_on):
    assert python_seam.should_route_to_piston() is True


def test_check_correctness_piston_keeps_the_result_dict_shape(piston_on, monkeypatch):
    monkeypatch.setattr(
        piston_client.requests,
        "post",
        MagicMock(return_value=_fake_response(
            {"run": {"stdout": f"{PASS_SENTINEL}\n", "stderr": "", "code": 0, "signal": None}}
        )),
    )

    result = check_correctness_piston(PASSING, 3.0, "HumanEval/0", 7, runtimes=RUNTIMES)

    for key in ("task_id", "passed", "result", "completion_id"):
        assert key in result
    assert result["task_id"] == "HumanEval/0"
    assert result["completion_id"] == 7
    assert result["passed"] is True
    assert result["result"] == "passed"


def test_failing_candidate_is_a_non_pass(piston_on, monkeypatch):
    monkeypatch.setattr(
        piston_client.requests,
        "post",
        MagicMock(return_value=_fake_response(
            {
                "run": {
                    "stdout": f"{FAIL_SENTINEL} AssertionError: \n",
                    "stderr": "Traceback",
                    "code": 1,
                    "signal": None,
                }
            }
        )),
    )

    result = check_correctness_piston(FAILING, 3.0, "HumanEval/0", 0, runtimes=RUNTIMES)

    assert result["passed"] is False
    assert result["result"].startswith("failed:")


def test_piston_failure_is_distinguishable_from_a_test_failure(piston_on, monkeypatch):
    def _boom(*args, **kwargs):
        raise requests.ConnectionError("name or service not known")

    monkeypatch.setattr(piston_client.requests, "post", _boom)

    result = check_correctness_piston(PASSING, 3.0, "HumanEval/0", 0, runtimes=RUNTIMES)

    assert result["passed"] is False
    # Not a silent 0: the result string names the infra failure, and does not
    # look like a candidate that ran and failed.
    assert result["result"].startswith("piston error:")
    assert not result["result"].startswith("failed:")
    assert result["piston_result_class"] == "error"


def test_check_result_records_reproducibility_metadata(piston_on, monkeypatch):
    monkeypatch.setattr(
        piston_client.requests,
        "post",
        MagicMock(return_value=_fake_response(
            {"run": {"stdout": f"{PASS_SENTINEL}\n", "stderr": "", "code": 0, "signal": None}}
        )),
    )

    result = check_correctness_piston(PASSING, 3.0, "t", 0, runtimes=RUNTIMES)

    assert result["execution_backend"] == "piston"
    assert result["piston_runtime"] == "python"
    assert result["piston_runtime_version"] == "3.10.0"


# ─── R3: timeout parity ────────────────────────────────────────────────────


def test_per_problem_timeout_is_passed_through(piston_on, monkeypatch):
    post_spy = MagicMock(
        return_value=_fake_response(
            {"run": {"stdout": f"{PASS_SENTINEL}\n", "stderr": "", "code": 0, "signal": None}}
        )
    )
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    check_correctness_piston(PASSING, 1.5, "t", 0, runtimes=RUNTIMES)

    assert post_spy.call_args.kwargs["json"]["run_timeout"] == 1500


def test_humaneval_default_timeout_lands_on_the_cap():
    # compute_code_eval's default per-problem timeout is 3.0s and Piston's cap
    # is 3000ms, so the common case is genuine parity rather than a clamp.
    assert python_seam.run_timeout_ms_for(3.0) == 3000


def test_a_longer_timeout_is_clamped_not_assumed():
    assert python_seam.run_timeout_ms_for(30.0) == 3000


def test_infinite_loop_is_reported_as_a_timeout_not_a_hang(piston_on, monkeypatch):
    # [needs-live-endpoint] stand-in: the transport times out on both attempts,
    # which is what a non-terminating candidate produces.
    post_spy = MagicMock(side_effect=[requests.Timeout("hang")] * 3)
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = check_correctness_piston(
        "while True:\n    pass\n", 3.0, "t", 0, runtimes=RUNTIMES
    )

    assert result["passed"] is False
    assert result["result"] == "timed out"
    assert post_spy.call_count == 1 + MAX_COLD_START_RETRIES


def test_cold_start_timeout_is_retried_once_at_this_seam(piston_on, monkeypatch):
    ok = _fake_response(
        {"run": {"stdout": f"{PASS_SENTINEL}\n", "stderr": "", "code": 0, "signal": None}}
    )
    post_spy = MagicMock(side_effect=[requests.Timeout("cold"), ok])
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = check_correctness_piston(PASSING, 3.0, "t", 0, runtimes=RUNTIMES)

    assert post_spy.call_count == 2
    assert result["passed"] is True


# ─── PAL: answer capture ───────────────────────────────────────────────────


def test_pal_answer_symbol_round_trips_through_repr():
    program = "answer = [1, 2, 3]\n"
    proc = _run_locally(assemble_pal_program(program, answer_symbol="answer"))
    assert proc.returncode == 0
    payload = proc.stdout.strip().splitlines()[-1]
    assert payload.startswith(ANSWER_SENTINEL)
    value = ast.literal_eval(payload[len(ANSWER_SENTINEL) :].strip())
    assert value == [1, 2, 3]


def test_pal_without_a_symbol_uses_the_programs_last_printed_line():
    program = "print('noise')\nprint(42)\n"
    proc = _run_locally(assemble_pal_program(program))
    assert proc.returncode == 0
    payload = proc.stdout.strip().splitlines()[-1]
    assert payload == f"{ANSWER_SENTINEL} 42"


def test_pal_failure_exits_nonzero_with_the_fail_sentinel():
    proc = _run_locally(assemble_pal_program("raise ValueError('nope')\n"))
    assert proc.returncode == 1
    assert FAIL_SENTINEL in proc.stdout


def test_run_program_piston_returns_the_answer(piston_on, monkeypatch):
    monkeypatch.setattr(
        piston_client.requests,
        "post",
        MagicMock(return_value=_fake_response(
            {"run": {"stdout": f"{ANSWER_SENTINEL} 42\n", "stderr": "", "code": 0, "signal": None}}
        )),
    )

    result = run_program_piston("print(42)", 3.0, "pal/0", 0, runtimes=RUNTIMES)

    assert result["task_id"] == "pal/0"
    assert result["completion_id"] == 0
    assert result["result"] == "42"


def test_run_program_piston_timeout_uses_the_pal_string(piston_on, monkeypatch):
    monkeypatch.setattr(
        piston_client.requests,
        "post",
        MagicMock(side_effect=[requests.Timeout("x")] * 3),
    )

    result = run_program_piston("while True: pass", 3.0, "pal/0", 0, runtimes=RUNTIMES)

    # PAL's in-process path words a timeout as "failed: timed out".
    assert result["result"] == "failed: timed out"


def test_run_program_piston_failure_is_a_failed_string(piston_on, monkeypatch):
    monkeypatch.setattr(
        piston_client.requests,
        "post",
        MagicMock(return_value=_fake_response(
            {
                "run": {
                    "stdout": f"{FAIL_SENTINEL} ValueError: nope\n",
                    "stderr": "",
                    "code": 1,
                    "signal": None,
                }
            }
        )),
    )

    result = run_program_piston("raise ValueError('nope')", 3.0, "pal/0", 0, runtimes=RUNTIMES)

    assert result["result"].startswith("failed:")
    assert "nope" in result["result"]


# ─── the "beyond" engine is not routable, and says so ──────────────────────


def test_supported_engines_are_the_two_one_shot_shapes():
    assert python_seam.SUPPORTED_ENGINES == frozenset({"check_correctness", "pal"})


def test_beyond_engine_fails_loudly_rather_than_running_in_container():
    with pytest.raises(UnsupportedEngineError) as excinfo:
        require_supported_engine("beyond")
    message = str(excinfo.value)
    # The error explains WHY and where the real ask lives.
    assert "lctk" in message
    assert "sortedcontainers" in message
    assert "piston-repo" in message


def test_supported_engines_do_not_raise():
    require_supported_engine("check_correctness")
    require_supported_engine("pal")
