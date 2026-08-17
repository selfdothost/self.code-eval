"""Tests for code-eval's own opt-in Piston config surface (cavekit R1 / T-001).

Covers:
- Flag off  -> no HTTP request is issued to PISTON_BASE_URL (call-count asserted
  against a mocked HTTP layer).
- Flag on + unreachable/bad URL -> a clear PistonConnectionError surfaces,
  never a silent 0-score.
- Both vars read from this service's own env surface (ENABLE_PISTON_EXECUTION /
  PISTON_BASE_URL), not the FastAPI backend's config.py.
"""

from unittest.mock import MagicMock

import pytest
import requests

from code_eval.piston import (
    BASE_URL_ENV_VAR,
    DEFAULT_PISTON_BASE_URL,
    ENABLE_ENV_VAR,
    PistonConnectionError,
    PistonError,
    PistonNotEnabledError,
    piston_base_url,
    piston_enabled,
    piston_post,
    require_piston_enabled,
)
from code_eval.piston import client as piston_client


# ─── Defaults / own config surface (AC3) ────────────────────────────────


def test_env_var_names_are_the_expected_pair():
    # The service reads its OWN vars, matching (by name) the parallel backend
    # pair but from this service's environment, not the backend's config.py.
    assert ENABLE_ENV_VAR == "ENABLE_PISTON_EXECUTION"
    assert BASE_URL_ENV_VAR == "PISTON_BASE_URL"


def test_defaults_are_off_and_point_at_live_theseus(monkeypatch):
    monkeypatch.delenv(ENABLE_ENV_VAR, raising=False)
    monkeypatch.delenv(BASE_URL_ENV_VAR, raising=False)
    assert piston_enabled() is False
    assert piston_base_url() == DEFAULT_PISTON_BASE_URL
    assert DEFAULT_PISTON_BASE_URL == "http://piston.self-theseus.svc.cluster.local:2000"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "  True  "])
def test_enable_flag_truthy_spellings(monkeypatch, raw):
    monkeypatch.setenv(ENABLE_ENV_VAR, raw)
    assert piston_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "  ", "banana"])
def test_enable_flag_falsy_spellings(monkeypatch, raw):
    monkeypatch.setenv(ENABLE_ENV_VAR, raw)
    assert piston_enabled() is False


def test_base_url_override_is_read_from_env(monkeypatch):
    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://example.invalid:2000/")
    # trailing slash normalized away so `${base}${path}` never double-slashes
    assert piston_base_url() == "http://example.invalid:2000"


def test_blank_base_url_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(BASE_URL_ENV_VAR, "   ")
    assert piston_base_url() == DEFAULT_PISTON_BASE_URL


# ─── Flag off -> no HTTP request issued (AC1) ───────────────────────────


def test_flag_off_issues_no_http_request(monkeypatch):
    monkeypatch.delenv(ENABLE_ENV_VAR, raising=False)
    post_spy = MagicMock()
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    with pytest.raises(PistonNotEnabledError):
        require_piston_enabled()
    with pytest.raises(PistonNotEnabledError):
        piston_post("/api/v2/execute", {"language": "python"})

    # The gate rejected the call BEFORE any request object was built.
    assert post_spy.call_count == 0


def test_flag_off_gate_error_is_a_piston_error(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV_VAR, "0")
    with pytest.raises(PistonError):
        require_piston_enabled()


# ─── Flag on + bad URL -> clear error, never a silent 0 (AC2) ───────────


def test_flag_on_bad_url_raises_connection_error_not_silent_zero(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV_VAR, "1")
    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://piston.unreachable.invalid:2000")

    def _boom(*args, **kwargs):
        raise requests.ConnectionError("name or service not known")

    monkeypatch.setattr(piston_client.requests, "post", _boom)

    with pytest.raises(PistonConnectionError) as excinfo:
        piston_post("/api/v2/execute", {"language": "python", "version": "3.10.0"})

    # The endpoint URL is surfaced in the error -> diagnosable, not a mute 0.
    assert "piston.unreachable.invalid" in str(excinfo.value)
    # And it is a Piston error, distinguishable from a normal test-failure 0.
    assert isinstance(excinfo.value, PistonError)


def test_flag_on_reachable_returns_raw_response_for_t004(monkeypatch):
    monkeypatch.setenv(ENABLE_ENV_VAR, "1")
    monkeypatch.setenv(BASE_URL_ENV_VAR, "http://piston.self-theseus.svc.cluster.local:2000")

    sentinel = MagicMock(name="response")
    post_spy = MagicMock(return_value=sentinel)
    monkeypatch.setattr(piston_client.requests, "post", post_spy)

    resp = piston_post("/api/v2/execute", {"language": "python"})

    assert resp is sentinel
    called_url = post_spy.call_args.args[0]
    assert called_url == "http://piston.self-theseus.svc.cluster.local:2000/api/v2/execute"
