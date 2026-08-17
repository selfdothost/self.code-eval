"""Tests for per-language resource sizing and run_timeout (cavekit R4 / R5,
T-006 / T-007).

Covers:
- Resource sizing is per-language, not one global constant, and a compiled
  language's default budget is strictly larger than the interpreted baseline.
- An exit-137 / SIGKILL result is distinguishable from a normal non-zero exit
  (so an undersized compiled build is diagnosable, not scored as a test failure).
- ``run_timeout`` is per-language and never assumes a per-request raise above
  Piston's hard 3000 ms global cap.
- The cold-start retry policy fires EXACTLY once, and a persistent timeout is a
  distinct timeout result — not a success, not a normal failure.
"""

from unittest.mock import MagicMock

import pytest
import requests

from code_eval.piston import (
    COMPILED_RESOURCES,
    DEFAULT_RESOURCES,
    DEFAULT_RUN_TIMEOUT_MS,
    INTERPRETED_RESOURCES,
    MAX_COLD_START_RETRIES,
    PISTON_RUN_TIMEOUT_CAP_MS,
    PistonConnectionError,
    PistonTimeoutError,
    clear_runtimes_cache,
    execute,
    execute_with_cold_start_retry,
    parse_execute_response,
    resources_for,
    run_timeout_for,
    timeout_result,
)
from code_eval.piston import client as piston_client
from code_eval.piston import sizing as piston_sizing

RUNTIMES = [
    {"language": "python", "version": "3.10.0", "aliases": ["py", "py3", "python3"]},
    {"language": "rust", "version": "1.68.2", "aliases": ["rs"]},
    {"language": "csharp", "version": "6.12.0", "aliases": ["cs"], "runtime": "dotnet"},
]


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
    monkeypatch.setenv(
        "PISTON_BASE_URL", "http://piston.self-theseus.svc.cluster.local:2000"
    )


# ─── R4 AC: sizing is per-language, not one global constant ─────────────────


def test_resources_are_configurable_per_language_not_one_global():
    # More than one distinct budget exists in the table, and two languages with
    # different build needs resolve to different budgets.
    distinct = {tuple(sorted(v.items())) for v in piston_sizing.LANGUAGE_RESOURCES.values()}
    assert len(distinct) > 1, "sizing collapsed to a single global budget"
    assert resources_for("python") != resources_for("rust")


def test_single_language_budget_is_tunable_in_isolation(monkeypatch):
    # Editing one entry changes only that language — the table is the policy.
    monkeypatch.setattr(
        piston_sizing,
        "LANGUAGE_RESOURCES",
        {**piston_sizing.LANGUAGE_RESOURCES, "lua": {"cpu": 7, "ram": 77, "appDisk": 77}},
    )
    assert resources_for("lua") == {"cpu": 7, "ram": 77, "appDisk": 77}
    assert resources_for("python") == dict(INTERPRETED_RESOURCES)


@pytest.mark.parametrize("language", ["csharp", "rust", "java"])
def test_compiled_budget_strictly_larger_than_interpreted(language):
    compiled = resources_for(language)
    interpreted = resources_for("python")
    # AC: strictly larger on AT LEAST one of cpu/ram/appDisk...
    assert any(compiled[axis] > interpreted[axis] for axis in ("cpu", "ram", "appDisk"))
    # ...and never smaller on any axis.
    assert all(compiled[axis] >= interpreted[axis] for axis in ("cpu", "ram", "appDisk"))


def test_unmapped_language_falls_back_to_interpreted_baseline():
    assert resources_for("some-unmapped-lang") == dict(DEFAULT_RESOURCES)
    assert DEFAULT_RESOURCES == INTERPRETED_RESOURCES


def test_unmapped_but_known_compiled_invocable_gets_compiled_budget(monkeypatch):
    # Defensive path: a compiled invocable missing from the table still gets the
    # compiled budget rather than an undersized interpreted one.
    monkeypatch.setattr(
        piston_sizing,
        "LANGUAGE_RESOURCES",
        {k: v for k, v in piston_sizing.LANGUAGE_RESOURCES.items() if k != "rust"},
    )
    assert resources_for("rust") == dict(COMPILED_RESOURCES)


def test_resources_for_returns_a_copy_not_the_table_entry():
    budget = resources_for("python")
    budget["ram"] = 1
    assert resources_for("python")["ram"] != 1


def test_execute_injects_the_per_language_budget(piston_on, monkeypatch):
    post_spy = MagicMock(
        return_value=_fake_response(
            {"run": {"stdout": "", "stderr": "", "code": 0, "signal": None}}
        )
    )
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    execute("fn main(){}", "rs", runtimes=RUNTIMES)

    payload = post_spy.call_args.kwargs["json"]
    assert payload["resources"] == resources_for("rust")
    assert payload["resources"] != dict(INTERPRETED_RESOURCES)


def test_explicit_resources_override_the_table(piston_on, monkeypatch):
    post_spy = MagicMock(
        return_value=_fake_response(
            {"run": {"stdout": "", "stderr": "", "code": 0, "signal": None}}
        )
    )
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    execute("x", "python", runtimes=RUNTIMES, resources={"cpu": 9, "ram": 9, "appDisk": 9})

    assert post_spy.call_args.kwargs["json"]["resources"] == {
        "cpu": 9,
        "ram": 9,
        "appDisk": 9,
    }


# ─── R4 AC: exit 137 / SIGKILL distinguishable from a normal non-zero exit ──


def test_sigkill_run_stage_is_distinguishable_from_normal_nonzero_exit():
    killed = parse_execute_response(
        {"run": {"stdout": "", "stderr": "", "code": 137, "signal": "SIGKILL"}},
        language="rust",
        version="1.68.2",
    )
    failed = parse_execute_response(
        {"run": {"stdout": "", "stderr": "assert", "code": 1, "signal": None}},
        language="rust",
        version="1.68.2",
    )
    # The raw fields differ, so the two are never conflated.
    assert (killed.exit_code, killed.signal) == (137, "SIGKILL")
    assert (failed.exit_code, failed.signal) == (1, None)
    assert killed.signal != failed.signal


def test_sigkill_at_the_compile_stage_is_visible(monkeypatch):
    # The undersized-build case: the kill happens while compiling.
    result = parse_execute_response(
        {"compile": {"stdout": "", "stderr": "", "code": 137, "signal": "SIGKILL"}},
        language="csharp",
        version="6.12.0",
    )
    assert result.compile is not None
    assert result.compile.signal == "SIGKILL"
    assert result.compile.code == 137


# ─── R5 AC: run_timeout per-language, clamped to Piston's global cap ────────


def test_run_timeout_is_configurable_per_language():
    distinct = set(piston_sizing.LANGUAGE_RUN_TIMEOUTS.values())
    assert len(distinct) > 1, "run_timeout collapsed to a single global value"
    assert run_timeout_for("python") != run_timeout_for("rust")


@pytest.mark.parametrize(
    "language", ["python", "rust", "csharp", "java", "some-unmapped-lang"]
)
def test_run_timeout_never_exceeds_pistons_hard_cap(language):
    assert run_timeout_for(language) <= PISTON_RUN_TIMEOUT_CAP_MS


def test_no_table_entry_assumes_a_raise_above_the_cap():
    assert all(
        value <= PISTON_RUN_TIMEOUT_CAP_MS
        for value in piston_sizing.LANGUAGE_RUN_TIMEOUTS.values()
    )


def test_an_over_cap_entry_is_clamped_not_sent(monkeypatch):
    monkeypatch.setattr(
        piston_sizing,
        "LANGUAGE_RUN_TIMEOUTS",
        {**piston_sizing.LANGUAGE_RUN_TIMEOUTS, "python": 60000},
    )
    assert run_timeout_for("python") == PISTON_RUN_TIMEOUT_CAP_MS


def test_unmapped_language_uses_the_default_run_timeout():
    assert run_timeout_for("some-unmapped-lang") == DEFAULT_RUN_TIMEOUT_MS


def test_execute_injects_the_per_language_run_timeout(piston_on, monkeypatch):
    post_spy = MagicMock(
        return_value=_fake_response(
            {"run": {"stdout": "", "stderr": "", "code": 0, "signal": None}}
        )
    )
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    execute("print(42)", "python", runtimes=RUNTIMES)

    assert post_spy.call_args.kwargs["json"]["run_timeout"] == run_timeout_for("python")


# ─── R5 AC: exactly one cold-start retry, persistent timeout is distinct ────


def test_transport_timeout_raises_piston_timeout_error_not_connection_error(
    piston_on, monkeypatch
):
    def _timeout(*args, **kwargs):
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(piston_client.requests, "post", _timeout)
    with pytest.raises(PistonTimeoutError):
        execute("print(42)", "python", runtimes=RUNTIMES)


def test_connection_error_is_not_treated_as_a_cold_start_timeout(piston_on, monkeypatch):
    # A plain unreachable endpoint must NOT be swallowed by the retry policy.
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise requests.ConnectionError("name or service not known")

    monkeypatch.setattr(piston_client.requests, "post", _boom)
    with pytest.raises(PistonConnectionError):
        execute_with_cold_start_retry("print(42)", "python", runtimes=RUNTIMES)
    assert calls["n"] == 1, "connection error must not be retried"


def test_success_on_first_attempt_makes_exactly_one_call(piston_on, monkeypatch):
    # Counted at the transport layer: a first-attempt success must not retry.
    post_spy = MagicMock(
        return_value=_fake_response(
            {"run": {"stdout": "42\n", "stderr": "", "code": 0, "signal": None}}
        )
    )
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = execute_with_cold_start_retry("print(42)", "python", runtimes=RUNTIMES)

    assert post_spy.call_count == 1
    assert result.exit_code == 0
    assert result.timed_out is False


def test_cold_start_timeout_retries_exactly_once_then_succeeds(piston_on, monkeypatch):
    ok = _fake_response({"run": {"stdout": "42\n", "stderr": "", "code": 0, "signal": None}})
    post_spy = MagicMock(side_effect=[requests.Timeout("cold start"), ok])
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = execute_with_cold_start_retry("print(42)", "python", runtimes=RUNTIMES)

    assert post_spy.call_count == 2, "exactly one retry after a cold-start timeout"
    assert result.stdout.strip() == "42"
    assert result.timed_out is False


def test_persistent_timeout_is_bounded_and_is_a_timeout_result(piston_on, monkeypatch):
    post_spy = MagicMock(side_effect=[requests.Timeout("still cold")] * 3)
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = execute_with_cold_start_retry("while True: pass", "python", runtimes=RUNTIMES)

    # Bounded: 1 attempt + MAX_COLD_START_RETRIES, never an unbounded loop.
    assert post_spy.call_count == 1 + MAX_COLD_START_RETRIES
    # Distinct from a success (exit 0) AND from a normal failure (non-zero exit).
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.exit_code != 0


def test_timeout_result_carries_resolved_language_and_version_for_metadata():
    result = timeout_result("python", "3.10.0", detail="timed out")
    assert (result.language, result.version) == ("python", "3.10.0")
    assert result.timed_out is True


def test_retry_resolves_language_before_any_request(piston_on, monkeypatch):
    from code_eval.piston import UnknownLanguageError

    post_spy = MagicMock()
    monkeypatch.setattr(piston_client.requests, "post", post_spy)
    with pytest.raises(UnknownLanguageError):
        execute_with_cold_start_retry("x", "cobol", runtimes=RUNTIMES)
    assert post_spy.call_count == 0


# ─── SIGKILL cold-start retry (revised R5) ──────────────────────────────────
#
# Through session-gateway a Piston run_timeout kill arrives as a bare exit 137
# with no signal field, so the transport-timeout retry alone missed it. Measured
# live: a cold julia is SIGKILLed twice, then succeeds. These pin the revised
# policy, including the deliberate limits on it.


def _killed(status=200):
    return _fake_response(
        {"run": {"stdout": "", "stderr": "", "code": 137, "signal": "SIGKILL"}},
        status_code=status,
    )


def _ok(stdout="42\n"):
    return _fake_response(
        {"run": {"stdout": stdout, "stderr": "", "code": 0, "signal": None}}
    )


def test_a_cold_sigkill_is_retried_and_can_succeed(piston_on, monkeypatch):
    # The measured julia shape: kill, kill, pass.
    post_spy = MagicMock(side_effect=[_killed(), _killed(), _ok()])
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = execute_with_cold_start_retry("print(42)", "python", runtimes=RUNTIMES)

    assert post_spy.call_count == 3
    assert result.exit_code == 0
    assert result.stdout.strip() == "42"


def test_one_retry_would_not_have_been_enough(piston_on, monkeypatch):
    # Guards the reason the bound is 2 rather than the kit's original 1.
    assert MAX_COLD_START_RETRIES >= 2


def test_a_persistent_sigkill_stays_a_kill_not_a_timeout(piston_on, monkeypatch):
    # A genuinely undersized language must still read as a resource kill after
    # the retries are exhausted — relabelling it a timeout would hide the cause.
    post_spy = MagicMock(side_effect=[_killed()] * 3)
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = execute_with_cold_start_retry("print(42)", "python", runtimes=RUNTIMES)

    assert post_spy.call_count == 1 + MAX_COLD_START_RETRIES
    assert result.exit_code == 137
    assert result.timed_out is False


def test_an_explicit_budget_is_never_retried_on_sigkill(piston_on, monkeypatch):
    # THE guard on this policy: if the caller chose the budget, a kill is a real
    # sizing problem and must surface immediately rather than be retried into
    # looking like a slow success.
    post_spy = MagicMock(side_effect=[_killed(), _ok()])
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = execute_with_cold_start_retry(
        "print(42)", "python", runtimes=RUNTIMES, resources={"cpu": 1, "ram": 1, "appDisk": 1}
    )

    assert post_spy.call_count == 1, "an explicit budget must not be retried"
    assert result.exit_code == 137


def test_a_genuine_test_failure_is_never_retried(piston_on, monkeypatch):
    # Only cold-start signals retry. A candidate that ran and failed is a result.
    failing = _fake_response(
        {"run": {"stdout": "", "stderr": "AssertionError", "code": 1, "signal": None}}
    )
    post_spy = MagicMock(side_effect=[failing])
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = execute_with_cold_start_retry("assert False", "python", runtimes=RUNTIMES)

    assert post_spy.call_count == 1
    assert result.exit_code == 1


def test_retries_are_recorded_not_silent(piston_on, monkeypatch):
    # A retry can turn a scored 0 into a pass, so it must be auditable.
    monkeypatch.setattr(
        piston_client.requests, "post", MagicMock(side_effect=[_killed(), _ok()])
    )

    result = execute_with_cold_start_retry("print(42)", "python", runtimes=RUNTIMES)

    assert result.attempts == 2
    assert len(result.retry_reasons) == 1
    assert "137" in result.retry_reasons[0] or "SIGKILL" in result.retry_reasons[0]


def test_a_first_attempt_success_records_no_retry(piston_on, monkeypatch):
    monkeypatch.setattr(piston_client.requests, "post", MagicMock(return_value=_ok()))
    result = execute_with_cold_start_retry("print(42)", "python", runtimes=RUNTIMES)
    assert result.attempts == 1
    assert result.retry_reasons == ()
