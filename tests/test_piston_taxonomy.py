"""Tests for the result taxonomy / error-signal path (cavekit R1 / T-005).

The contract under test: with the flag on, ANY execution failure — an
unreachable/bad ``PISTON_BASE_URL``, a transport timeout, an undersized-build
SIGKILL, a compile error — surfaces as a clear, distinct signal. It is never a
silent 0-score indistinguishable from a genuine failing test.
"""

from unittest.mock import MagicMock

import pytest
import requests

from code_eval.piston import (
    NON_TEST_FAILURE_CLASSES,
    PistonConnectionError,
    PistonNotEnabledError,
    PistonTimeoutError,
    ResultClass,
    UnknownLanguageError,
    Verdict,
    classify,
    classify_exception,
    clear_runtimes_cache,
    execute,
    parse_execute_response,
    safe_verdict,
    timeout_result,
    verdict_for,
    verdict_for_exception,
)
from code_eval.piston import client as piston_client

RUNTIMES = [
    {"language": "python", "version": "3.10.0", "aliases": ["py"]},
    {"language": "rust", "version": "1.68.2", "aliases": ["rs"]},
]


def _result(**stages):
    """Parse a synthetic Piston response into an ExecutionResult."""
    return parse_execute_response(stages, language="python", version="3.10.0")


def _fake_response(json_body, *, status_code=200):
    resp = MagicMock(name="response")
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


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
    monkeypatch.setenv("PISTON_BASE_URL", "http://piston.invalid:2000")


# ─── classify(): the five result-shaped classes ────────────────────────────


def test_exit_zero_is_a_pass():
    result = _result(run={"stdout": "ok", "stderr": "", "code": 0, "signal": None})
    assert classify(result) is ResultClass.PASS


def test_nonzero_exit_is_a_genuine_test_fail():
    result = _result(run={"stdout": "", "stderr": "AssertionError", "code": 1, "signal": None})
    assert classify(result) is ResultClass.FAIL


def test_persistent_timeout_is_a_timeout_not_a_fail():
    result = timeout_result("python", "3.10.0", detail="timed out twice")
    assert classify(result) is ResultClass.TIMEOUT
    # Distinct from both a pass and a genuine failure.
    assert classify(result) is not ResultClass.FAIL
    assert classify(result) is not ResultClass.PASS


def test_sigkill_is_its_own_class_not_a_fail():
    result = _result(run={"stdout": "", "stderr": "", "code": 137, "signal": "SIGKILL"})
    assert classify(result) is ResultClass.SIGKILL


def test_exit_137_without_a_signal_name_still_reads_as_sigkill():
    result = _result(run={"stdout": "", "stderr": "", "code": 137, "signal": None})
    assert classify(result) is ResultClass.SIGKILL


def test_compile_failure_is_a_compile_error_not_a_test_fail():
    result = parse_execute_response(
        {
            "compile": {"stdout": "", "stderr": "error[E0425]", "code": 1, "signal": None},
            "run": {"stdout": "", "stderr": "", "code": 0, "signal": None},
        },
        language="rust",
        version="1.68.2",
    )
    assert classify(result) is ResultClass.COMPILE_ERROR


def test_undersized_build_sigkill_beats_compile_error():
    # An undersized compiled build SIGKILLs at compile. It must read as a
    # resource kill (diagnosable per R4), not as an ordinary compile error.
    result = parse_execute_response(
        {"compile": {"stdout": "", "stderr": "", "code": 137, "signal": "SIGKILL"}},
        language="rust",
        version="1.68.2",
    )
    assert classify(result) is ResultClass.SIGKILL


def test_exit_zero_with_a_signal_is_not_a_pass():
    result = _result(run={"stdout": "", "stderr": "", "code": 0, "signal": "SIGTERM"})
    assert classify(result) is not ResultClass.PASS


# ─── classify_exception(): raised failures are distinct signals ────────────


@pytest.mark.parametrize(
    "exc",
    [
        PistonConnectionError("unreachable"),
        PistonNotEnabledError("flag off"),
        UnknownLanguageError("cobol"),
    ],
)
def test_piston_errors_classify_as_error(exc):
    assert classify_exception(exc) is ResultClass.ERROR


def test_transport_timeout_classifies_as_timeout():
    assert classify_exception(PistonTimeoutError("read timed out")) is ResultClass.TIMEOUT


# ─── Verdict: only PASS scores; execution errors are flagged as such ───────


def test_only_pass_scores_as_passed():
    passing = verdict_for(_result(run={"stdout": "", "stderr": "", "code": 0, "signal": None}))
    failing = verdict_for(_result(run={"stdout": "", "stderr": "", "code": 1, "signal": None}))
    assert passing.passed is True
    assert failing.passed is False


@pytest.mark.parametrize(
    "cls",
    [
        ResultClass.TIMEOUT,
        ResultClass.SIGKILL,
        ResultClass.COMPILE_ERROR,
        ResultClass.ERROR,
    ],
)
def test_non_test_failure_classes_are_flagged_as_execution_errors(cls):
    assert cls in NON_TEST_FAILURE_CLASSES
    verdict = Verdict(classification=cls, passed=False, detail="")
    assert verdict.is_execution_error is True


def test_a_genuine_test_failure_is_not_an_execution_error():
    verdict = verdict_for(_result(run={"stdout": "", "stderr": "", "code": 1, "signal": None}))
    assert verdict.classification is ResultClass.FAIL
    assert verdict.is_execution_error is False
    # This is the whole point: FAIL and the infra classes are distinguishable.
    assert verdict.passed is False


def test_verdict_detail_is_non_empty_for_every_class():
    for result in (
        _result(run={"stdout": "", "stderr": "", "code": 0, "signal": None}),
        _result(run={"stdout": "", "stderr": "", "code": 1, "signal": None}),
        _result(run={"stdout": "", "stderr": "", "code": 137, "signal": "SIGKILL"}),
        timeout_result("python", "3.10.0"),
    ):
        assert verdict_for(result).detail.strip() != ""


def test_sigkill_detail_points_at_resource_sizing():
    verdict = verdict_for(_result(run={"stdout": "", "stderr": "", "code": 137, "signal": "SIGKILL"}))
    assert "137" in verdict.detail or "SIGKILL" in verdict.detail


def test_verdict_for_exception_is_never_a_pass():
    verdict = verdict_for_exception(PistonConnectionError("unreachable host"))
    assert verdict.passed is False
    assert verdict.classification is ResultClass.ERROR
    assert "PistonConnectionError" in verdict.detail
    assert verdict.result is None


# ─── safe_verdict(): the never-a-silent-0 entry point ──────────────────────


def test_safe_verdict_turns_a_bad_base_url_into_a_clear_error_signal(piston_on, monkeypatch):
    def _boom(*args, **kwargs):
        raise requests.ConnectionError("name or service not known")

    monkeypatch.setattr(piston_client.requests, "post", _boom)

    verdict = safe_verdict(execute, "print(42)", "python", runtimes=RUNTIMES)

    # Not a silent 0: a distinct ERROR verdict naming the failure.
    assert verdict.classification is ResultClass.ERROR
    assert verdict.passed is False
    assert verdict.is_execution_error is True
    assert "piston.invalid" in verdict.detail


def test_safe_verdict_distinguishes_error_from_a_real_failing_test(piston_on, monkeypatch):
    resp = _fake_response(
        {"run": {"stdout": "", "stderr": "AssertionError", "code": 1, "signal": None}}
    )
    monkeypatch.setattr(piston_client.requests, "post", MagicMock(return_value=resp))

    verdict = safe_verdict(execute, "assert False", "python", runtimes=RUNTIMES)

    assert verdict.classification is ResultClass.FAIL
    assert verdict.is_execution_error is False


def test_safe_verdict_maps_a_disabled_flag_to_an_error_verdict(monkeypatch):
    monkeypatch.delenv("ENABLE_PISTON_EXECUTION", raising=False)
    verdict = safe_verdict(execute, "print(42)", "python", runtimes=RUNTIMES)
    assert verdict.classification is ResultClass.ERROR
    assert verdict.passed is False


def test_safe_verdict_maps_an_unknown_language_to_an_error_verdict(piston_on):
    verdict = safe_verdict(execute, "x", "cobol", runtimes=RUNTIMES)
    assert verdict.classification is ResultClass.ERROR
    assert "cobol" in verdict.detail


def test_safe_verdict_does_not_swallow_non_piston_bugs():
    def _bug(*args, **kwargs):
        raise ValueError("a real bug, not an execution outcome")

    with pytest.raises(ValueError):
        safe_verdict(_bug)


def test_result_class_serializes_as_a_stable_token():
    # str-valued enum so results metadata carries a stable token.
    assert ResultClass.PASS == "pass"
    assert ResultClass.SIGKILL.value == "sigkill"
