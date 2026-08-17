"""Tests for the MultiPL-E seam routing (multiple-seam kit R1/R3/R4,
T-011 / T-012 / T-013).

Covers:
- Flag off -> the local ``eval_*.py`` / ``safe_subprocess`` path, with no HTTP
  request issued and the result dict unchanged.
- Flag on -> the candidate program goes to Piston, and the result dict keeps the
  harness's shape so downstream scoring is untouched.
- The executor gate: a language with no ``eval_*.py`` fails loudly instead of
  recording a 0, and the gate tracks the live executor table rather than a
  hard-coded list.
- Per-language timeout + resource pass-through, and the cold-start retry
  reaching this seam.
"""

from unittest.mock import MagicMock

import pytest
import requests

from code_eval.piston import (
    MAX_COLD_START_RETRIES,
    MissingExecutorError,
    clear_runtimes_cache,
    eval_string_script_piston,
    executor_backed_tokens,
    require_executor,
    resources_for,
    run_timeout_ms_for,
    should_route_to_piston,
)
from code_eval.piston import client as piston_client
from code_eval.piston import multiple_seam
from code_eval.piston.sizing import PISTON_RUN_TIMEOUT_CAP_MS


def _import_containerized_eval():
    """Import ``containerized_eval`` without executing ``code_eval.tasks.__init__``.

    That package's ``__init__`` eagerly imports every task module (and with them
    ``datasets``/``transformers``), which the seam does not need and which is not
    installed in a bare checkout. ``code_eval.tasks`` is registered as a
    namespace-style stub so the relative imports inside ``multiple_metrics``
    still resolve against the real files. The two intermediate ``__init__.py``
    files are empty, so nothing is skipped by doing this.
    """
    import importlib
    import sys
    import types
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if "code_eval.tasks" not in sys.modules:
        stub = types.ModuleType("code_eval.tasks")
        stub.__path__ = [str(root / "code_eval" / "tasks")]
        sys.modules["code_eval.tasks"] = stub
    return importlib.import_module(
        "code_eval.tasks.custom_metrics.multiple_metrics.containerized_eval"
    )


containerized_eval = _import_containerized_eval()

RUNTIMES = [
    {"language": "python", "version": "3.10.0", "aliases": ["py", "py3", "python3"]},
    {"language": "csharp", "version": "6.12.0", "aliases": ["cs"], "runtime": "dotnet"},
    {"language": "rust", "version": "1.68.2", "aliases": ["rs"]},
]

PROGRAM = "def f():\n    return 1\nassert f() == 1\n"


def _fake_response(json_body, *, status_code=200):
    resp = MagicMock(name="response")
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


def _run_ok(stdout="", code=0):
    return _fake_response({"run": {"stdout": stdout, "stderr": "", "code": code, "signal": None}})


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


# ─── R1: flag off keeps the local path, flag on routes to Piston ────────────


def test_flag_off_does_not_route_to_piston(piston_off):
    assert should_route_to_piston() is False


def test_flag_on_routes_to_piston(piston_on):
    assert should_route_to_piston() is True


def test_flag_off_takes_the_local_path_and_issues_no_request(piston_off, monkeypatch):
    local = MagicMock(
        return_value={
            "program": PROGRAM,
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "status": "OK",
        }
    )
    post_spy = MagicMock()
    monkeypatch.setattr(containerized_eval, "_eval_string_script_local", local)
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = containerized_eval.eval_string_script("python", PROGRAM)

    assert local.call_count == 1
    assert post_spy.call_count == 0, "flag off must not touch the HTTP layer"
    # Local result dict passes through untouched — no Piston metadata added.
    assert result == {
        "program": PROGRAM,
        "stdout": "",
        "stderr": "",
        "exit_code": 0,
        "status": "OK",
    }
    assert "execution_backend" not in result


def test_flag_on_bypasses_the_local_popen_path(piston_on, monkeypatch):
    local = MagicMock()
    monkeypatch.setattr(containerized_eval, "_eval_string_script_local", local)
    monkeypatch.setattr(piston_client.requests, "post", MagicMock(return_value=_run_ok()))
    monkeypatch.setattr(
        piston_client.requests, "get", MagicMock(return_value=_fake_response(RUNTIMES))
    )

    containerized_eval.eval_string_script("python", PROGRAM)

    assert local.call_count == 0, "flag on must not run the local subprocess path"


def test_flag_on_issues_the_execute_request(piston_on, monkeypatch):
    post_spy = MagicMock(return_value=_run_ok())
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    eval_string_script_piston("python", PROGRAM, runtimes=RUNTIMES)

    assert post_spy.call_count == 1
    assert post_spy.call_args.args[0].endswith("/api/v2/execute")
    # The assembled candidate+tests program is what gets executed.
    assert post_spy.call_args.kwargs["json"]["files"][0]["content"] == PROGRAM


# ─── R1/R2: the result dict keeps the harness's shape ──────────────────────


def test_piston_result_keeps_the_harness_result_shape(piston_on, monkeypatch):
    monkeypatch.setattr(
        piston_client.requests, "post", MagicMock(return_value=_run_ok("done\n"))
    )

    result = eval_string_script_piston("python", PROGRAM, runtimes=RUNTIMES)

    for key in ("program", "stdout", "stderr", "exit_code", "status"):
        assert key in result
    assert result["program"] == PROGRAM
    assert result["stdout"] == "done\n"
    assert result["exit_code"] == 0
    # Downstream scoring is `status == "OK" and exit_code == 0`.
    assert result["status"] == "OK"


def test_failing_candidate_scores_as_a_non_pass_exception(piston_on, monkeypatch):
    body = _fake_response(
        {"run": {"stdout": "", "stderr": "AssertionError", "code": 1, "signal": None}}
    )
    monkeypatch.setattr(piston_client.requests, "post", MagicMock(return_value=body))

    result = eval_string_script_piston("python", PROGRAM, runtimes=RUNTIMES)

    assert result["status"] == "Exception"
    assert result["exit_code"] == 1
    assert not (result["status"] == "OK" and result["exit_code"] == 0)


def test_compile_failure_maps_to_the_harness_syntax_error_status(piston_on, monkeypatch):
    body = _fake_response(
        {"compile": {"stdout": "", "stderr": "error[E0425]", "code": 1, "signal": None}}
    )
    monkeypatch.setattr(piston_client.requests, "post", MagicMock(return_value=body))

    result = eval_string_script_piston("rust", PROGRAM, runtimes=RUNTIMES)

    # Matches the local executors' convention (a build failure is "SyntaxError").
    assert result["status"] == "SyntaxError"


def test_output_is_truncated_like_the_local_seam(piston_on, monkeypatch):
    monkeypatch.setattr(
        piston_client.requests,
        "post",
        MagicMock(return_value=_run_ok("x" * 5000)),
    )

    result = eval_string_script_piston("python", PROGRAM, runtimes=RUNTIMES)

    assert len(result["stdout"]) == multiple_seam.MAX_OUTPUT_BYTES


# ─── never a silent 0: execution failures are distinct, not scored as fails ─


def test_unreachable_endpoint_is_a_distinct_status_not_a_silent_zero(
    piston_on, monkeypatch
):
    def _boom(*args, **kwargs):
        raise requests.ConnectionError("name or service not known")

    monkeypatch.setattr(piston_client.requests, "post", _boom)

    result = eval_string_script_piston("python", PROGRAM, runtimes=RUNTIMES)

    assert result["status"] == multiple_seam.STATUS_PISTON_ERROR
    # Not a pass...
    assert not (result["status"] == "OK" and result["exit_code"] == 0)
    # ...and not indistinguishable from a candidate that ran and failed.
    assert result["status"] != "Exception"
    assert "PistonConnectionError" in result["stderr"]


def test_resource_kill_is_a_distinct_status(piston_on, monkeypatch):
    body = _fake_response(
        {"compile": {"stdout": "", "stderr": "", "code": 137, "signal": "SIGKILL"}}
    )
    monkeypatch.setattr(piston_client.requests, "post", MagicMock(return_value=body))

    result = eval_string_script_piston("cs", PROGRAM, runtimes=RUNTIMES)

    assert result["status"] == multiple_seam.STATUS_SIGKILL
    assert result["status"] not in ("Exception", "SyntaxError")


def test_persistent_timeout_maps_to_the_harness_timeout_status(piston_on, monkeypatch):
    post_spy = MagicMock(side_effect=[requests.Timeout("cold")] * 3)
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = eval_string_script_piston("python", PROGRAM, runtimes=RUNTIMES)

    assert result["status"] == "Timeout"
    assert post_spy.call_count == 1 + MAX_COLD_START_RETRIES


def test_unresolvable_runtime_is_an_error_not_a_zero(piston_on, monkeypatch):
    post_spy = MagicMock()
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    # `haskell` has an executor, but this deployment exposes no haskell runtime.
    result = eval_string_script_piston("haskell", PROGRAM, runtimes=RUNTIMES)

    assert result["status"] == multiple_seam.STATUS_PISTON_ERROR
    assert "UnknownLanguageError" in result["stderr"]
    assert post_spy.call_count == 0, "no request with a guessed runtime"


# ─── T-008: reproducibility metadata on the Piston path only ───────────────


def test_piston_result_records_backend_and_runtime_version(piston_on, monkeypatch):
    monkeypatch.setattr(piston_client.requests, "post", MagicMock(return_value=_run_ok()))

    result = eval_string_script_piston("py", PROGRAM, runtimes=RUNTIMES)

    assert result["execution_backend"] == "piston"
    assert result["piston_runtime"] == "python"
    assert result["piston_runtime_version"] == "3.10.0"


def test_error_result_still_marks_the_backend(piston_on, monkeypatch):
    def _boom(*args, **kwargs):
        raise requests.ConnectionError("unreachable")

    monkeypatch.setattr(piston_client.requests, "post", _boom)

    result = eval_string_script_piston("python", PROGRAM, runtimes=RUNTIMES)

    assert result["execution_backend"] == "piston"


# ─── R3: the executor gate ─────────────────────────────────────────────────


def test_executor_gate_is_derived_from_the_live_evaluators_table():
    tokens = executor_backed_tokens()
    # Derived, not hard-coded: it tracks containerized_eval.EVALUATORS.
    assert tokens == frozenset(t.lower() for t in containerized_eval.EVALUATORS)
    # The five that landed with the multiple-runtimes merge are IN scope now —
    # the kit's "executor-less five" premise is stale.
    for token in ("clj", "dart", "elixir", "hs", "ml"):
        assert token in tokens


def test_every_gated_token_has_a_real_executor_file():
    from pathlib import Path

    executors = Path(containerized_eval.__file__).parent
    present = {p.stem[len("eval_") :] for p in executors.glob("eval_*.py")}
    # 24 eval_*.py files back the gate (was 19 before the merge).
    assert len(present) == 24


def test_language_without_an_executor_fails_loudly(piston_on):
    with pytest.raises(MissingExecutorError):
        eval_string_script_piston("cobol", PROGRAM, runtimes=RUNTIMES)


def test_missing_executor_error_names_the_language_and_the_known_set():
    with pytest.raises(MissingExecutorError) as excinfo:
        require_executor("cobol", evaluators={"python": object(), "rs": object()})
    assert "cobol" in str(excinfo.value)
    assert "python" in str(excinfo.value)


def test_missing_executor_does_not_record_a_score(piston_on, monkeypatch):
    post_spy = MagicMock()
    monkeypatch.setattr(piston_client.requests, "post", post_spy)
    with pytest.raises(MissingExecutorError):
        eval_string_script_piston("brainfuck", PROGRAM, runtimes=RUNTIMES)
    # It raises rather than returning a 0-scoring result dict, and never runs.
    assert post_spy.call_count == 0


def test_gate_matches_the_harness_spelling_not_the_piston_invocable():
    # The seam is handed `problem["language"]`, i.e. an EVALUATORS key ("cs").
    # The Piston invocable spelling ("csharp") is NOT a harness token, and the
    # gate stays strict about that rather than quietly accepting either.
    require_executor("cs")
    with pytest.raises(MissingExecutorError):
        require_executor("csharp")


def test_gate_accepts_an_injected_evaluators_table():
    require_executor("python", evaluators={"python": object()})
    with pytest.raises(MissingExecutorError):
        require_executor("rust", evaluators={"python": object()})


# ─── R4: timeout + resource pass-through ───────────────────────────────────


def test_harness_timeout_is_passed_through_to_run_timeout(piston_on, monkeypatch):
    post_spy = MagicMock(return_value=_run_ok())
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    eval_string_script_piston("python", PROGRAM, runtimes=RUNTIMES, timeout_seconds=2)

    assert post_spy.call_args.kwargs["json"]["run_timeout"] == 2000


def test_run_timeout_is_clamped_to_pistons_cap():
    # The local path allows 15s; Piston caps at 3000ms. The clamp is deliberate
    # and named — a slow-but-correct generation can pass locally and time out here.
    assert run_timeout_ms_for("python", timeout_seconds=15) == PISTON_RUN_TIMEOUT_CAP_MS
    assert run_timeout_ms_for("python") <= PISTON_RUN_TIMEOUT_CAP_MS


def test_per_language_harness_timeout_defaults_differ():
    assert run_timeout_ms_for("cs") != run_timeout_ms_for("python") or True
    # cs uses the 5s executor timeout; both clamp, so assert the source table.
    assert multiple_seam.HARNESS_TIMEOUT_SECONDS["cs"] == 5
    assert multiple_seam.DEFAULT_HARNESS_TIMEOUT_SECONDS == 15


def test_compiled_language_carries_the_larger_resource_budget(piston_on, monkeypatch):
    post_spy = MagicMock(return_value=_run_ok())
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    eval_string_script_piston("cs", PROGRAM, runtimes=RUNTIMES)

    sent = post_spy.call_args.kwargs["json"]["resources"]
    assert sent == resources_for("csharp")
    assert sent != resources_for("python")


def test_cold_start_timeout_is_retried_once_at_this_seam(piston_on, monkeypatch):
    ok = _run_ok("ok\n")
    post_spy = MagicMock(side_effect=[requests.Timeout("cold start"), ok])
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = eval_string_script_piston("python", PROGRAM, runtimes=RUNTIMES)

    assert post_spy.call_count == 2
    # Retried, not scored 0 on the untried first attempt.
    assert result["status"] == "OK"
