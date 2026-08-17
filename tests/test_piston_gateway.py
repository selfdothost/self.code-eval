"""Tests for the session-gateway transport (T-019).

The gateway is the **live** path to theseus's Piston; the direct
``/api/v2/execute`` transport the rest of the suite covers is the one the
original spec named. These tests pin the contract that session-gateway's own
README and ``src/piston-backend/pistonHttpBackend.ts`` define — including the
three things the gateway path costs us, which are asserted rather than left as
prose:

- no per-request version pin (results record ``"unpinned"``, not a guess);
- no ``signal`` field (SIGKILL must be recognised off the synthesized exit 137);
- no separate compile stage (a compile error classifies as FAIL here).
"""

from unittest.mock import MagicMock

import pytest
import requests

from code_eval.piston import (
    BACKEND_GATEWAY,
    GATEWAY_COMPILED_RESOURCES,
    GATEWAY_INTERPRETED_RESOURCES,
    GatewayExecutionFailedError,
    GatewayRejectedError,
    PistonConnectionError,
    PistonNotEnabledError,
    PistonTimeoutError,
    ResultClass,
    build_submit_payload,
    classify,
    clear_runtimes_cache,
    execute,
    execute_with_cold_start_retry,
    gateway_execute,
    gateway_max_runtime_ms_for,
    gateway_resources_for,
    piston_backend,
    poll_until_settled,
)
from code_eval.piston import client as piston_client
from code_eval.piston import gateway as piston_gateway

PROGRAM = "print(6*7)"

# Stand-in for the gateway's /runtimes passthrough (self.theseus/gitlab-profile#9),
# which proxies Piston's own payload verbatim.
RUNTIMES = [
    {"language": "python", "version": "3.12.0", "aliases": ["py", "py3", "python3"]},
    {"language": "rust", "version": "1.68.2", "aliases": ["rs"]},
    {"language": "csharp.net", "version": "5.0.201", "aliases": ["csharp", "cs", "c#"]},
]


def _fake_response(json_body, *, status_code=200):
    resp = MagicMock(name="response")
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


def _dispatched(stdout="42", exit_code=0, state="completed"):
    return _fake_response(
        {
            "status": "dispatched",
            "requestId": "req-1",
            "recordId": "rec-1",
            "preemption": "admitted",
            "evicted": [],
            "detail": {
                "state": state,
                "result": {"stdout": stdout, "stderr": "", "exitCode": exit_code},
            },
        }
    )


@pytest.fixture(autouse=True)
def _reset_runtimes_cache():
    clear_runtimes_cache()
    yield
    clear_runtimes_cache()


@pytest.fixture
def gateway_on(monkeypatch):
    monkeypatch.setenv("ENABLE_PISTON_EXECUTION", "1")
    monkeypatch.setenv("PISTON_BACKEND", "gateway")
    monkeypatch.setenv("SESSION_GATEWAY_URL", "http://session-gateway.test")
    monkeypatch.setenv("SESSION_GATEWAY_APP_KEY", "sk_theseus_test")


# ─── backend selection ──────────────────────────────────────────────────────


def test_gateway_is_the_default_backend(monkeypatch):
    monkeypatch.delenv("PISTON_BACKEND", raising=False)
    assert piston_backend() == BACKEND_GATEWAY


def test_an_unrecognised_backend_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("PISTON_BACKEND", "carrier-pigeon")
    assert piston_backend() == BACKEND_GATEWAY


def test_execute_routes_through_the_gateway_when_selected(gateway_on, monkeypatch):
    # NOTE: piston_gateway.requests and piston_client.requests are the SAME
    # module object, so one spy sees both transports' calls. Assert on the URLs
    # rather than patching them separately.
    post_spy = MagicMock(return_value=_dispatched())
    get_spy = MagicMock()
    monkeypatch.setattr(piston_gateway.requests, "post", post_spy)
    monkeypatch.setattr(piston_client.requests, "get", get_spy)

    result = execute(PROGRAM, "python", runtimes=RUNTIMES)

    urls = [c.args[0] for c in post_spy.call_args_list]
    assert len(urls) == 1
    assert urls[0].endswith("/submit")
    assert not any("/api/v2/execute" in u for u in urls), "direct transport untouched"
    assert get_spy.call_count == 0, "no GET /api/v2/runtimes on the gateway path"
    assert result.stdout == "42"


def test_gateway_path_resolves_via_the_passthrough_not_piston_direct(
    gateway_on, monkeypatch
):
    # Since self.theseus/gitlab-profile#9 the gateway exposes its own authenticated
    # /runtimes. Resolution must go there, never to Piston's /api/v2/runtimes.
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=_dispatched()))
    get_spy = MagicMock(return_value=_fake_response(RUNTIMES))
    monkeypatch.setattr(piston_gateway.requests, "get", get_spy)

    execute_with_cold_start_retry(PROGRAM, "python")

    urls = [c.args[0] for c in get_spy.call_args_list]
    assert urls and all(u.endswith("/runtimes") for u in urls)
    assert not any("/api/v2/" in u for u in urls)
    # App-key authenticated like every other gateway call.
    assert get_spy.call_args.kwargs["headers"]["x-app-key"] == "sk_theseus_test"


def test_unknown_language_now_raises_before_submitting(gateway_on, monkeypatch):
    # Before the /runtimes passthrough existed this token went straight through
    # and came back as an opaque "cobol-* runtime is unknown" failure. Now the
    # gateway path gets the same R3 guarantee as the direct one.
    from code_eval.piston import UnknownLanguageError

    post_spy = MagicMock(return_value=_dispatched())
    monkeypatch.setattr(piston_gateway.requests, "post", post_spy)

    with pytest.raises(UnknownLanguageError):
        execute(PROGRAM, "cobol", runtimes=RUNTIMES)

    assert post_spy.call_count == 0


def test_the_resolved_version_is_pinned_on_the_request(gateway_on, monkeypatch):
    # self.theseus/gitlab-profile#8 added a top-level `version`. Sending the
    # version we resolved is what makes a score reproducible rather than
    # "whatever was installed that day".
    post_spy = MagicMock(return_value=_dispatched())
    monkeypatch.setattr(piston_gateway.requests, "post", post_spy)

    execute(PROGRAM, "py", runtimes=RUNTIMES)

    payload = post_spy.call_args.kwargs["json"]
    assert payload["language"] == "python"   # invocable name, not the token
    assert payload["version"] == "3.12.0"


def test_version_is_omitted_when_not_resolved():
    # Omitting it falls back to the gateway's deployment default rather than
    # sending an empty string Piston would reject.
    assert "version" not in build_submit_payload(PROGRAM, "python")
    assert build_submit_payload(PROGRAM, "python", version="3.12.0")["version"] == "3.12.0"


# ─── submit payload shape ───────────────────────────────────────────────────


def test_payload_puts_language_and_code_at_the_top_level():
    payload = build_submit_payload(PROGRAM, "python", priority="low")
    assert payload["type"] == "piston"
    assert payload["language"] == "python"
    assert payload["code"] == PROGRAM
    assert payload["priority"] == "low"


def test_payload_does_not_use_pistons_native_nested_shape():
    # The gateway rejects {language, version, files:[...]} with
    # invalid-piston-request — that mismatch is exactly what an integration
    # ported from calling Piston directly would hit.
    payload = build_submit_payload(PROGRAM, "python")
    assert "files" not in payload
    assert "version" not in payload
    assert "piston" not in payload


def test_priority_defaults_to_low_so_evals_never_preempt_interactive_work(gateway_on):
    payload = build_submit_payload(PROGRAM, "python")
    assert payload["priority"] == "low"


def test_max_runtime_is_clamped_to_the_gateway_ceiling():
    payload = build_submit_payload(PROGRAM, "python", max_runtime_ms=999_000)
    assert payload["maxRuntimeMs"] == piston_gateway.GATEWAY_MAX_RUNTIME_MS_CEILING


def test_stdin_is_only_sent_when_non_empty():
    assert "stdin" not in build_submit_payload(PROGRAM, "python")
    assert build_submit_payload(PROGRAM, "python", stdin="x")["stdin"] == "x"


def test_app_key_is_sent_as_a_header(gateway_on, monkeypatch):
    post_spy = MagicMock(return_value=_dispatched())
    monkeypatch.setattr(piston_gateway.requests, "post", post_spy)

    gateway_execute(PROGRAM, "python")

    assert post_spy.call_args.kwargs["headers"]["x-app-key"] == "sk_theseus_test"


def test_a_missing_app_key_is_a_named_error_not_a_mystery_401(monkeypatch):
    monkeypatch.setenv("ENABLE_PISTON_EXECUTION", "1")
    monkeypatch.setenv("PISTON_BACKEND", "gateway")
    monkeypatch.delenv("SESSION_GATEWAY_APP_KEY", raising=False)
    post_spy = MagicMock()
    monkeypatch.setattr(piston_gateway.requests, "post", post_spy)

    with pytest.raises(GatewayRejectedError) as excinfo:
        gateway_execute(PROGRAM, "python")

    assert excinfo.value.reason == "missing-key"
    assert "SESSION_GATEWAY_APP_KEY" in str(excinfo.value)
    assert post_spy.call_count == 0, "no request without a key"


def test_flag_off_refuses_to_submit(monkeypatch):
    monkeypatch.delenv("ENABLE_PISTON_EXECUTION", raising=False)
    monkeypatch.setenv("SESSION_GATEWAY_APP_KEY", "sk_theseus_test")
    post_spy = MagicMock()
    monkeypatch.setattr(piston_gateway.requests, "post", post_spy)

    with pytest.raises(PistonNotEnabledError):
        gateway_execute(PROGRAM, "python")
    assert post_spy.call_count == 0


# ─── quota-scale resources ──────────────────────────────────────────────────


def test_gateway_resources_are_quota_units_not_megabytes():
    budget = gateway_resources_for("python")
    assert budget == GATEWAY_INTERPRETED_RESOURCES
    # Sending the direct path's MB-scale numbers here would blow the quota.
    assert budget["ram"] < 10


def test_compiled_languages_get_the_verified_working_budget():
    # theseus observed cpu:1/ram:1/appDisk:1 SIGKILLing a dotnet build, and
    # cpu:2/ram:2/appDisk:4 running it clean — this is that budget, not a guess.
    assert gateway_resources_for("csharp") == GATEWAY_COMPILED_RESOURCES
    assert gateway_resources_for("rust") == GATEWAY_COMPILED_RESOURCES
    assert gateway_resources_for("cs") == GATEWAY_COMPILED_RESOURCES


def test_interpreted_and_compiled_budgets_differ():
    assert gateway_resources_for("python") != gateway_resources_for("rust")


def test_execute_sends_the_per_language_quota_budget(gateway_on, monkeypatch):
    post_spy = MagicMock(return_value=_dispatched())
    monkeypatch.setattr(piston_gateway.requests, "post", post_spy)

    execute(PROGRAM, "rust", runtimes=RUNTIMES)

    assert post_spy.call_args.kwargs["json"]["resources"] == GATEWAY_COMPILED_RESOURCES


def test_gateway_timeout_is_not_squeezed_into_pistons_3000ms_cap():
    # The gateway's ceiling is 60s, so the harness's 5-15s per-problem timeouts
    # pass through intact — the direct path's parity caveat largely disappears.
    assert gateway_max_runtime_ms_for("python", timeout_seconds=15) == 15000
    assert gateway_max_runtime_ms_for("python", timeout_seconds=120) == 60000


# ─── rejections never score ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reason",
    ["unknown-key", "quota-not-configured", "invalid-piston-request", "invalid-resources"],
)
def test_a_rejected_submission_raises_with_its_reason(gateway_on, monkeypatch, reason):
    body = _fake_response({"status": "rejected", "reason": reason, "message": "nope"})
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=body))

    with pytest.raises(GatewayRejectedError) as excinfo:
        gateway_execute(PROGRAM, "python")

    # The machine-readable reason is kept as a field, not just prose: a quota
    # problem is operationally different from a malformed request.
    assert excinfo.value.reason == reason


def test_a_rejection_classifies_as_an_execution_error_not_a_failing_test(gateway_on):
    from code_eval.piston import classify_exception

    exc = GatewayRejectedError("quota-not-configured", "no quota")
    assert classify_exception(exc) is ResultClass.ERROR


def test_transport_failure_is_a_connection_error(gateway_on, monkeypatch):
    def _boom(*args, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(piston_gateway.requests, "post", _boom)
    with pytest.raises(PistonConnectionError):
        gateway_execute(PROGRAM, "python")


def test_transport_timeout_stays_retriable(gateway_on, monkeypatch):
    def _slow(*args, **kwargs):
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(piston_gateway.requests, "post", _slow)
    with pytest.raises(PistonTimeoutError):
        gateway_execute(PROGRAM, "python")


def test_cold_start_retry_still_applies_on_the_gateway_path(gateway_on, monkeypatch):
    post_spy = MagicMock(side_effect=[requests.Timeout("cold"), _dispatched()])
    monkeypatch.setattr(piston_gateway.requests, "post", post_spy)

    result = execute_with_cold_start_retry(PROGRAM, "python", runtimes=RUNTIMES)

    assert post_spy.call_count == 2
    assert result.stdout == "42"


def test_http_error_status_is_not_scored(gateway_on, monkeypatch):
    body = _fake_response({"error": "database-unavailable"}, status_code=503)
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=body))
    with pytest.raises(PistonConnectionError):
        gateway_execute(PROGRAM, "python")


# ─── queued submissions poll to a terminal state ────────────────────────────


def test_a_queued_submission_polls_until_settled(gateway_on, monkeypatch):
    queued = _fake_response(
        {"status": "queued", "requestId": "req-9", "position": 3, "globalPosition": 7}
    )
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=queued))
    get_spy = MagicMock(
        side_effect=[
            _fake_response({"state": "queued"}),
            _fake_response({"state": "running"}),
            _fake_response(
                {
                    "state": "completed",
                    "result": {"stdout": "42", "stderr": "", "exitCode": 0},
                }
            ),
        ]
    )
    monkeypatch.setattr(piston_gateway.requests, "get", get_spy)

    result = gateway_execute(PROGRAM, "python", sleep=lambda _s: None)

    assert get_spy.call_count == 3
    assert get_spy.call_args.args[0].endswith("/status/req-9")
    assert result.stdout == "42"


def test_polling_gives_up_rather_than_hanging_forever(gateway_on, monkeypatch):
    monkeypatch.setattr(
        piston_gateway.requests,
        "get",
        MagicMock(return_value=_fake_response({"state": "queued"})),
    )
    with pytest.raises(PistonTimeoutError):
        poll_until_settled(
            "req-1", poll_timeout=2, poll_interval=1, sleep=lambda _s: None
        )


# ─── the transient not-found registration race (#8) ─────────────────────────
#
# Measured live: ~0.5% of executes at concurrency 6 (1/200 go), reproduced at
# concurrency 4, never once at concurrency 1 across 92 sequential runs. The
# gateway answers a status lookup for an id it just issued with 404 +
# {"state":"not-found"} for a short window. It used to raise
# PistonConnectionError, which the seam scored as a failed program that had in
# fact never run — a silent-0 concentrated in the compiled languages, because
# it takes the admission queue to provoke.


def _not_found(request_id="req-9"):
    return _fake_response({"state": "not-found", "id": request_id}, status_code=404)


def test_a_not_found_status_is_polled_through_not_scored(gateway_on, monkeypatch):
    """The measured case: 404 not-found, then the record appears."""
    queued = _fake_response({"status": "queued", "requestId": "req-9"})
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=queued))
    get_spy = MagicMock(
        side_effect=[
            _not_found(),
            _fake_response(
                {
                    "state": "completed",
                    "result": {"stdout": "42", "stderr": "", "exitCode": 0},
                }
            ),
        ]
    )
    monkeypatch.setattr(piston_gateway.requests, "get", get_spy)

    result = gateway_execute(PROGRAM, "python", sleep=lambda _s: None)

    assert get_spy.call_count == 2
    assert result.stdout == "42"
    assert result.exit_code == 0


def test_a_permanently_unknown_id_still_fails_rather_than_passing(gateway_on, monkeypatch):
    """The other half: not-found forever is a real loss and must not be settled.

    Before #8 this state was in SETTLED_STATES, so had the body ever reached the
    poll loop it would have been mapped into an ExecutionResult with no exit
    code — a resultless pass. It has to time out instead, and say the program
    did not run.
    """
    monkeypatch.setattr(
        piston_gateway.requests, "get", MagicMock(return_value=_not_found("req-gone"))
    )
    with pytest.raises(PistonTimeoutError) as exc:
        poll_until_settled(
            "req-gone", poll_timeout=2, poll_interval=1, sleep=lambda _s: None
        )
    assert "req-gone" in str(exc.value)
    assert "did not run" in str(exc.value)


def test_not_found_tolerance_does_not_swallow_other_failures(gateway_on, monkeypatch):
    """A revoked app key must not be mistaken for a registration race.

    The tolerance is scoped to a 404 whose body actually says not-found; every
    other status, and a 404 with any other body, still raises.
    """
    monkeypatch.setattr(
        piston_gateway.requests,
        "get",
        MagicMock(return_value=_fake_response({"error": "unauthorized"}, status_code=401)),
    )
    with pytest.raises(PistonConnectionError):
        poll_until_settled("req-1", poll_timeout=2, poll_interval=1, sleep=lambda _s: None)

    monkeypatch.setattr(
        piston_gateway.requests,
        "get",
        MagicMock(return_value=_fake_response({"error": "no such route"}, status_code=404)),
    )
    with pytest.raises(PistonConnectionError):
        poll_until_settled("req-1", poll_timeout=2, poll_interval=1, sleep=lambda _s: None)


def test_the_poll_deadline_is_still_the_ceiling(gateway_on, monkeypatch):
    """Tolerating not-found must not become an unbounded retry loop."""
    get_spy = MagicMock(return_value=_not_found("req-gone"))
    monkeypatch.setattr(piston_gateway.requests, "get", get_spy)
    with pytest.raises(PistonTimeoutError):
        poll_until_settled(
            "req-gone", poll_timeout=3, poll_interval=1, sleep=lambda _s: None
        )
    # deadline 3 / interval 1 -> polls at elapsed 0,1,2,3; the 4th sees elapsed
    # >= poll_timeout and raises. Bounded, and bounded by the same arithmetic as
    # every other non-terminal state.
    assert get_spy.call_count == 4


def test_queued_without_a_request_id_is_an_error(gateway_on, monkeypatch):
    queued = _fake_response({"status": "queued"})
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=queued))
    with pytest.raises(PistonConnectionError):
        gateway_execute(PROGRAM, "python")


# ─── what the gateway path costs us, asserted not assumed ───────────────────


def test_no_version_pin_is_recorded_honestly_not_guessed(gateway_on, monkeypatch):
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=_dispatched()))

    result = execute(PROGRAM, "python", runtimes=RUNTIMES)

    # There is no per-request version pin and the gateway echoes none back
    # (self.theseus/gitlab-profile#8). Recording a placeholder beats recording a
    # version we never verified.
    assert result.version == "unpinned"


def test_signal_is_always_none_on_the_gateway_path(gateway_on, monkeypatch):
    monkeypatch.setattr(
        piston_gateway.requests, "post", MagicMock(return_value=_dispatched(exit_code=1))
    )
    result = execute(PROGRAM, "python", runtimes=RUNTIMES)
    assert result.signal is None


def test_sigkill_is_still_caught_via_the_synthesized_exit_137(gateway_on, monkeypatch):
    # The gateway drops `signal` but synthesizes exit 137 for a signal kill, so
    # the taxonomy's SIGKILL rule still fires on the exit code alone.
    monkeypatch.setattr(
        piston_gateway.requests,
        "post",
        MagicMock(return_value=_dispatched(stdout="", exit_code=137)),
    )
    result = execute(PROGRAM, "rust", runtimes=RUNTIMES)
    assert classify(result) is ResultClass.SIGKILL


def test_compile_errors_are_not_distinguishable_on_this_path(gateway_on, monkeypatch):
    # Documented limitation, asserted so it can't silently change: the gateway
    # returns the compile stage's streams with no marker, so a build failure
    # classifies as FAIL rather than COMPILE_ERROR. The compiler's own message
    # still reaches stderr, so it stays diagnosable by a human.
    body = _fake_response(
        {
            "status": "dispatched",
            "detail": {
                "state": "completed",
                "result": {"stdout": "", "stderr": "error[E0425]", "exitCode": 1},
            },
        }
    )
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=body))

    result = execute(PROGRAM, "rust", runtimes=RUNTIMES)

    assert result.compile is None
    assert classify(result) is ResultClass.FAIL
    assert "E0425" in result.stderr


def test_a_passing_program_still_classifies_as_pass(gateway_on, monkeypatch):
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=_dispatched()))
    assert classify(execute(PROGRAM, "python", runtimes=RUNTIMES)) is ResultClass.PASS


def test_a_failed_state_falls_back_to_error_or_reason_text(gateway_on, monkeypatch):
    # A failed record with no `failure` block still has to say something useful:
    # `error`/`reason` are the fallbacks, so a gateway shape change degrades to
    # a vaguer message rather than an empty one.
    body = _fake_response(
        {
            "status": "dispatched",
            "detail": {"state": "failed", "error": "backend unreachable after 3 attempts"},
        }
    )
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=body))

    with pytest.raises(GatewayExecutionFailedError) as excinfo:
        execute(PROGRAM, "python", runtimes=RUNTIMES)

    assert "backend unreachable" in str(excinfo.value)


# ─── verified against the live gateway 2026-07-27 ───────────────────────────
#
# Everything below encodes a response shape observed from the real deployment,
# not one inferred from the README. Two were bugs on our side.


def test_a_failed_record_is_an_execution_error_not_a_failing_test(
    gateway_on, monkeypatch
):
    # THE bug this caught live: an unknown runtime settles as `state: "failed"`
    # with no result, and we were turning that into exit_code=None -> FAIL, i.e.
    # scoring "this language isn't installed" as "the candidate failed its
    # tests". Exactly the silent-0 class this client exists to prevent.
    body = _fake_response(
        {
            "status": "dispatched",
            "detail": {
                "state": "failed",
                "source": "piston",
                "failure": {
                    "kind": "non-transient",
                    "message": (
                        "piston execution failed with a non-retryable error: piston "
                        "service rejected the request (HTTP 400): rust-* runtime is unknown"
                    ),
                    "reason": "piston service rejected the request (HTTP 400)",
                },
                "retries": 0,
                "attempts": 1,
            },
        }
    )
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=body))

    with pytest.raises(GatewayExecutionFailedError) as excinfo:
        gateway_execute(PROGRAM, "rust")

    assert excinfo.value.kind == "non-transient"
    # The actionable detail lives at detail.failure.message, not detail.error.
    assert "runtime is unknown" in str(excinfo.value)


def test_a_failed_record_classifies_as_error(gateway_on):
    from code_eval.piston import classify_exception

    exc = GatewayExecutionFailedError("non-transient", "runtime is unknown")
    assert classify_exception(exc) is ResultClass.ERROR


def test_a_nonzero_exit_is_still_a_success_not_a_failed_record(gateway_on, monkeypatch):
    # The other half of the same distinction: a program that RAN and exited
    # non-zero comes back `completed` with a result, and must score as a
    # genuine test failure rather than an execution error.
    monkeypatch.setattr(
        piston_gateway.requests, "post", MagicMock(return_value=_dispatched(exit_code=1))
    )
    result = execute(PROGRAM, "python", runtimes=RUNTIMES)
    assert classify(result) is ResultClass.FAIL


def test_the_resolved_version_is_read_off_the_result(gateway_on, monkeypatch):
    # Observed live: the gateway echoes the runtime version at
    # detail.result.version (self.theseus/gitlab-profile#8's fix). We were
    # looking for it one level up and always recording "unpinned".
    body = _fake_response(
        {
            "status": "dispatched",
            "detail": {
                "state": "completed",
                "result": {
                    "stdout": "42\n",
                    "stderr": "",
                    "exitCode": 0,
                    "version": "3.12.0",
                },
            },
        }
    )
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=body))

    result = execute(PROGRAM, "python", runtimes=RUNTIMES)

    assert result.version == "3.12.0"


def test_version_falls_back_to_unpinned_when_absent(gateway_on, monkeypatch):
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=_dispatched()))
    assert execute(PROGRAM, "python", runtimes=RUNTIMES).version == "unpinned"


def test_a_run_timeout_overrun_arrives_as_exit_137(gateway_on, monkeypatch):
    # Observed live: `while True: pass` with maxRuntimeMs set comes back
    # `completed`, exitCode 137, empty streams. Piston enforces run_timeout with
    # SIGKILL, and the gateway drops the signal field and reports no timing —
    # so a timeout kill and an OOM kill are genuinely indistinguishable here.
    # Non-pass either way; the verdict detail names both causes rather than
    # asserting one.
    from code_eval.piston import verdict_for

    body = _fake_response(
        {
            "status": "dispatched",
            "detail": {
                "state": "completed",
                "result": {"stdout": "", "stderr": "", "exitCode": 137, "version": "3.12.0"},
            },
        }
    )
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=body))

    verdict = verdict_for(execute("while True: pass", "python", runtimes=RUNTIMES))

    assert verdict.classification is ResultClass.SIGKILL
    assert verdict.passed is False
    assert "run_timeout" in verdict.detail and "resource budget" in verdict.detail


def test_an_unsettled_dispatched_record_is_polled_not_scored(gateway_on, monkeypatch):
    # Observed under real burst load: a `dispatched` response whose detail has
    # not reached a terminal state. Mapping it straight through would yield an
    # ExecutionResult with no exit code, which scores as a failing test — the
    # same silent-0 shape as the failed-record bug. Poll instead.
    body = _fake_response(
        {"status": "dispatched", "requestId": "req-x", "detail": {"state": "running"}}
    )
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=body))
    get_spy = MagicMock(
        return_value=_fake_response(
            {
                "state": "completed",
                "result": {"stdout": "42\n", "stderr": "", "exitCode": 0},
            }
        )
    )
    monkeypatch.setattr(piston_gateway.requests, "get", get_spy)

    result = gateway_execute(PROGRAM, "python", sleep=lambda _s: None)

    assert get_spy.call_count == 1
    assert get_spy.call_args.args[0].endswith("/status/req-x")
    assert result.exit_code == 0


def test_an_unsettled_record_without_a_request_id_is_an_error(gateway_on, monkeypatch):
    body = _fake_response({"status": "dispatched", "detail": {"state": "running"}})
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=body))
    with pytest.raises(PistonConnectionError):
        gateway_execute(PROGRAM, "python")


def test_a_settled_dispatched_record_is_not_polled(gateway_on, monkeypatch):
    # The common case must stay a single round trip.
    monkeypatch.setattr(piston_gateway.requests, "post", MagicMock(return_value=_dispatched()))
    get_spy = MagicMock()
    monkeypatch.setattr(piston_gateway.requests, "get", get_spy)

    gateway_execute(PROGRAM, "python")

    assert get_spy.call_count == 0


def test_interpreted_budget_is_the_measured_one_not_the_default_quota(gateway_on):
    # 1/1/1 produced a ~8% spurious exit-137 rate on a trivial always-passing
    # program under nothing but back-to-back submission; 0.5/0.5/1 was clean.
    assert gateway_resources_for("python") == {"cpu": 0.5, "ram": 0.5, "appDisk": 1}


def test_compiled_budget_clears_the_measured_dotnet_floor(gateway_on):
    # Measured: csharp exit-137s at 0.5/0.5/1, 1/1/2 AND 1/2/4. cpu is the
    # binding constraint, so "double the interpreted ask" is not enough.
    budget = gateway_resources_for("csharp")
    assert budget["cpu"] >= 2
    assert budget == {"cpu": 2, "ram": 2, "appDisk": 4}
