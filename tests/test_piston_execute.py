"""Tests for the core execute contract + runtimes fetch (cavekit R2 / T-004).

Covers:
- The execute request carries source, stdin, language, version, ``resources``
  and ``run_timeout`` (fields present + passed through; VALUES are T-006/T-007).
- The parsed result exposes stdout, stderr, exit code and signal as DISTINCT
  fields (plus the compile stage for compiled languages).
- ``[needs-live-endpoint]`` unit stand-in: a known-good Python program printing
  ``42`` returns stdout ``42`` and exit 0 end-to-end, with the Piston
  ``/api/v2/execute`` HTTP response mocked (the live theseus endpoint is not
  reachable from here).
- The ``GET /api/v2/runtimes`` fetch + cache: gated, connection-error-honouring,
  fetched once then memoized.
"""

from unittest.mock import MagicMock

import pytest
import requests

from code_eval.piston import (
    DEFAULT_RESOURCES,
    DEFAULT_RUN_TIMEOUT_MS,
    ExecutionResult,
    PistonConnectionError,
    PistonNotEnabledError,
    PistonStage,
    UnknownLanguageError,
    build_execute_payload,
    clear_runtimes_cache,
    execute,
    get_runtimes,
    parse_execute_response,
)
from code_eval.piston import client as piston_client
from code_eval.piston import runtimes as piston_runtimes


# A stand-in for `GET /api/v2/runtimes` (invocable `language` names + versions).
RUNTIMES = [
    {"language": "python", "version": "3.10.0", "aliases": ["py", "py3", "python3"]},
    {"language": "c++", "version": "10.2.0", "aliases": ["cpp", "g++"], "runtime": "gcc"},
    {"language": "rust", "version": "1.68.2", "aliases": ["rs"]},
]


def _fake_response(json_body, *, status_code=200):
    """Build a mock ``requests.Response`` returning ``json_body``."""
    resp = MagicMock(name="response")
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


@pytest.fixture(autouse=True)
def _reset_runtimes_cache():
    """Keep the module-level runtimes cache from leaking across tests."""
    clear_runtimes_cache()
    yield
    clear_runtimes_cache()


@pytest.fixture
def piston_on(monkeypatch):
    """Turn the feature flag on and point at a stable base URL."""
    monkeypatch.setenv("ENABLE_PISTON_EXECUTION", "1")
    # These suites exercise the DIRECT Piston transport. The service default
    # is the session-gateway backend, so pin it here rather than depending on
    # ambient env — gateway coverage lives in test_piston_gateway.py.
    monkeypatch.setenv("PISTON_BACKEND", "direct")
    monkeypatch.setenv(
        "PISTON_BASE_URL", "http://piston.self-theseus.svc.cluster.local:2000"
    )


# ─── AC: request carries the minimum fields, passed through ─────────────────


def test_build_payload_carries_minimum_fields():
    payload = build_execute_payload(
        "print(42)", invocable="python", version="3.10.0", stdin="in\n"
    )
    # source is carried in files[].content (the upstream Piston v2 shape).
    assert payload["files"][0]["content"] == "print(42)"
    assert payload["stdin"] == "in\n"
    assert payload["language"] == "python"
    assert payload["version"] == "3.10.0"
    # resources + run_timeout fields present (sensible placeholder defaults).
    assert payload["resources"] == DEFAULT_RESOURCES
    assert payload["run_timeout"] == DEFAULT_RUN_TIMEOUT_MS


def test_build_payload_injects_resources_and_run_timeout_for_t006_t007():
    # T-006/T-007 inject per-language values via these args; structure honours it.
    payload = build_execute_payload(
        "code",
        invocable="rust",
        version="1.68.2",
        resources={"cpu": 2, "ram": 512, "appDisk": 512},
        run_timeout=5000,
    )
    assert payload["resources"] == {"cpu": 2, "ram": 512, "appDisk": 512}
    assert payload["run_timeout"] == 5000


def test_execute_sends_resolved_language_and_fields(piston_on, monkeypatch):
    post_spy = MagicMock(
        return_value=_fake_response(
            {"run": {"stdout": "", "stderr": "", "code": 0, "signal": None}}
        )
    )
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    execute("print(42)", "py", stdin="", runtimes=RUNTIMES)

    # requests.post(url, json=payload, ...) — url is positional, json is a kwarg.
    called_url = post_spy.call_args.args[0]
    payload = post_spy.call_args.kwargs["json"]
    assert called_url.endswith("/api/v2/execute")
    # `py` resolved -> invocable `python` + concrete version, never the token.
    assert payload["language"] == "python"
    assert payload["version"] == "3.10.0"
    for key in ("files", "stdin", "language", "version", "resources", "run_timeout"):
        assert key in payload


# ─── AC: parsed result exposes 4 distinct fields ────────────────────────────


def test_parse_response_exposes_distinct_fields():
    data = {"run": {"stdout": "hi\n", "stderr": "warn\n", "code": 3, "signal": "SIGSEGV"}}
    result = parse_execute_response(data, language="python", version="3.10.0")
    assert isinstance(result, ExecutionResult)
    assert result.stdout == "hi\n"
    assert result.stderr == "warn\n"
    assert result.exit_code == 3
    assert result.signal == "SIGSEGV"
    # the four fields are genuinely distinct attributes, not one blob.
    assert (result.stdout, result.stderr, result.exit_code, result.signal) == (
        "hi\n",
        "warn\n",
        3,
        "SIGSEGV",
    )


def test_parse_response_keeps_compile_stage_distinct():
    # Compiled languages fail at the compile stage; surface it distinctly.
    data = {
        "compile": {"stdout": "", "stderr": "error[E0425]", "code": 1, "signal": None},
        "run": {"stdout": "", "stderr": "", "code": 0, "signal": None},
    }
    result = parse_execute_response(data, language="rust", version="1.68.2")
    assert isinstance(result.compile, PistonStage)
    assert result.compile.stderr == "error[E0425]"
    assert result.compile_failed is True
    # run stage still parsed and top-level fields come from it.
    assert result.run is not None


def test_parse_response_compile_only_failure_surfaces_at_top_level():
    # No run stage (build failed hard): the compile failure must not read as a
    # silent empty pass.
    data = {"compile": {"stdout": "", "stderr": "boom", "code": 1, "signal": None}}
    result = parse_execute_response(data, language="rust", version="1.68.2")
    assert result.run is None
    assert result.exit_code == 1
    assert result.stderr == "boom"
    assert result.compile_failed is True


# ─── AC [needs-live-endpoint] unit stand-in: prints 42 -> stdout 42, exit 0 ─


def test_known_good_python_prints_42_end_to_end(piston_on, monkeypatch):
    """[needs-live-endpoint] stand-in. The live theseus endpoint isn't reachable
    from here, so the Piston /api/v2/execute HTTP response is mocked with a
    realistic Piston v2 run payload and the parsed result is asserted."""
    live_body = {
        "language": "python",
        "version": "3.10.0",
        "run": {"stdout": "42\n", "stderr": "", "code": 0, "signal": None, "output": "42\n"},
    }
    post_spy = MagicMock(return_value=_fake_response(live_body))
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = execute("print(42)", "python", runtimes=RUNTIMES)

    assert result.stdout.strip() == "42"
    assert result.exit_code == 0
    assert result.signal is None
    # end-to-end: request actually went out to the execute path once.
    assert post_spy.call_count == 1
    assert post_spy.call_args.args[0].endswith("/api/v2/execute")


def test_execute_unknown_language_raises_before_any_request(piston_on, monkeypatch):
    post_spy = MagicMock()
    monkeypatch.setattr(piston_client.requests, "post", post_spy)
    with pytest.raises(UnknownLanguageError):
        execute("x", "cobol", runtimes=RUNTIMES)
    assert post_spy.call_count == 0


def test_execute_bad_status_raises_connection_error_not_silent_zero(piston_on, monkeypatch):
    resp = _fake_response({"message": "bad runtime"}, status_code=400)
    monkeypatch.setattr(piston_client.requests, "post", MagicMock(return_value=resp))
    with pytest.raises(PistonConnectionError):
        execute("print(42)", "python", runtimes=RUNTIMES)


# ─── GET /api/v2/runtimes fetch + cache ─────────────────────────────────────


def test_get_runtimes_fetches_once_and_caches(piston_on, monkeypatch):
    get_spy = MagicMock(return_value=_fake_response(RUNTIMES))
    monkeypatch.setattr(piston_client.requests, "get", get_spy)

    first = get_runtimes()
    second = get_runtimes()

    assert [rt["language"] for rt in first] == ["python", "c++", "rust"]
    assert second is first  # served from cache
    assert get_spy.call_count == 1  # fetched exactly once

    # force_refresh re-fetches.
    get_runtimes(force_refresh=True)
    assert get_spy.call_count == 2


def test_get_runtimes_uses_the_get_path(piston_on, monkeypatch):
    get_spy = MagicMock(return_value=_fake_response(RUNTIMES))
    monkeypatch.setattr(piston_client.requests, "get", get_spy)
    get_runtimes()
    assert get_spy.call_args.args[0].endswith("/api/v2/runtimes")


def test_get_runtimes_flag_off_still_fetches(monkeypatch):
    """CONTRACT CHANGE (self.code-eval#6): this used to assert the opposite.

    Listing runtimes was gated on ENABLE_PISTON_EXECUTION, which meant a
    deployment could not be asked what it is *able* to run until it was already
    permitted to run things — so language registration could never be gated on
    live capability while the flag was held off for throughput testing
    (selfai/gitlab-profile#22). Discovery reads; it does not execute.

    The property this test used to protect — "flag off issues no request" — is
    the one that matters for EXECUTION, and it is still asserted, against
    piston_post, in tests/test_piston_runtimes_gate.py.
    """
    monkeypatch.delenv("ENABLE_PISTON_EXECUTION", raising=False)
    monkeypatch.setenv("PISTON_BACKEND", "direct")
    get_spy = MagicMock(return_value=_fake_response(RUNTIMES))
    monkeypatch.setattr(piston_client.requests, "get", get_spy)

    assert get_runtimes()  # no PistonNotEnabledError
    assert get_spy.call_count == 1


def test_get_runtimes_flag_off_can_still_be_made_to_refuse(monkeypatch):
    """The refusal was relaxed, not removed."""
    monkeypatch.delenv("ENABLE_PISTON_EXECUTION", raising=False)
    monkeypatch.setenv("PISTON_BACKEND", "direct")
    get_spy = MagicMock()
    monkeypatch.setattr(piston_client.requests, "get", get_spy)
    with pytest.raises(PistonNotEnabledError):
        get_runtimes(require_enabled=True)
    assert get_spy.call_count == 0


def test_get_runtimes_transport_failure_is_connection_error(piston_on, monkeypatch):
    def _boom(*args, **kwargs):
        raise requests.ConnectionError("name or service not known")

    monkeypatch.setattr(piston_client.requests, "get", _boom)
    with pytest.raises(PistonConnectionError):
        get_runtimes()


def test_get_runtimes_non_array_body_is_connection_error(piston_on, monkeypatch):
    resp = _fake_response({"not": "an array"})
    monkeypatch.setattr(piston_client.requests, "get", MagicMock(return_value=resp))
    with pytest.raises(PistonConnectionError):
        get_runtimes()


def test_execute_fetches_runtimes_when_not_supplied(piston_on, monkeypatch):
    # execute() with no runtimes arg pulls them via get_runtimes (cached fetch).
    get_spy = MagicMock(return_value=_fake_response(RUNTIMES))
    post_spy = MagicMock(
        return_value=_fake_response(
            {"run": {"stdout": "42\n", "stderr": "", "code": 0, "signal": None}}
        )
    )
    monkeypatch.setattr(piston_client.requests, "get", get_spy)
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    result = execute("print(42)", "python")
    assert result.stdout.strip() == "42"
    assert get_spy.call_count == 1
