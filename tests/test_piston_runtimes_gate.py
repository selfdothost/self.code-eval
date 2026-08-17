"""self.code-eval#6: listing runtimes is not executing code.

ENABLE_PISTON_EXECUTION used to gate `get_runtimes()` as well as execution,
which meant a deployment could not be asked *what it is able to run* until it
was already permitted to run things. That made language registration
(POST /api/languages) impossible to gate on live capability while the flag was
still held off for throughput testing (selfai/gitlab-profile#22) — every
registration was refused for a reason unrelated to the language.

The gate is relaxed for this one read-only lookup. These tests pin both halves:
discovery works with the flag off, and **execution is still gated** — the
safety property the flag exists for must be provably intact.
"""

import pytest
import requests

from code_eval.piston import gateway as piston_gateway
from code_eval.piston.client import piston_get, piston_post
from code_eval.piston.errors import PistonConnectionError, PistonNotEnabledError
from code_eval.piston.runtimes import clear_runtimes_cache, get_runtimes

RUNTIMES_PAYLOAD = [
    {"language": "python", "version": "3.12.0", "aliases": ["py"]},
    {"language": "rust", "version": "1.68.2", "aliases": ["rs"]},
]


@pytest.fixture(autouse=True)
def _reset_runtimes_cache():
    clear_runtimes_cache()
    yield
    clear_runtimes_cache()


@pytest.fixture
def piston_off(monkeypatch):
    """The live posture today: Piston reachable, execution not enabled."""
    monkeypatch.setenv("ENABLE_PISTON_EXECUTION", "false")
    monkeypatch.setenv("PISTON_BACKEND", "direct")
    monkeypatch.setenv("PISTON_BASE_URL", "http://piston.test:2000")


@pytest.fixture
def gateway_off(monkeypatch):
    monkeypatch.setenv("ENABLE_PISTON_EXECUTION", "false")
    monkeypatch.setenv("PISTON_BACKEND", "gateway")
    monkeypatch.setenv("SESSION_GATEWAY_URL", "http://session-gateway.test")
    monkeypatch.setenv("SESSION_GATEWAY_APP_KEY", "sk_theseus_test")


class _Resp:
    status_code = 200

    def json(self):
        return RUNTIMES_PAYLOAD


# ─── discovery works with execution disabled ───────────────────────────────


def test_direct_backend_lists_runtimes_with_execution_disabled(piston_off, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    got = get_runtimes()
    assert [r["language"] for r in got] == ["python", "rust"]


def test_gateway_backend_lists_runtimes_with_execution_disabled(gateway_off, monkeypatch):
    monkeypatch.setattr(piston_gateway, "_get", lambda *a, **k: RUNTIMES_PAYLOAD)
    got = get_runtimes()
    assert [r["language"] for r in got] == ["python", "rust"]


def test_opting_back_in_restores_the_old_refusal(gateway_off):
    """A caller that genuinely should not proceed while Piston is off can still
    say so — the behaviour was relaxed, not deleted."""
    with pytest.raises(PistonNotEnabledError):
        get_runtimes(require_enabled=True)


def test_direct_backend_opt_in_also_still_refuses(piston_off):
    with pytest.raises(PistonNotEnabledError):
        piston_get("/api/v2/runtimes", require_enabled=True)


# ─── execution is STILL gated (the property the flag exists for) ───────────


def test_execution_is_still_refused_when_disabled(piston_off, monkeypatch):
    """The regression that would matter: relaxing discovery must not relax
    submitting a program."""
    called = {"n": 0}
    monkeypatch.setattr(requests, "post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(PistonNotEnabledError):
        piston_post("/api/v2/execute", {"language": "python", "version": "3.12.0", "files": []})
    assert called["n"] == 0, "a request was built despite the execution gate"


def test_piston_get_still_gates_by_default(piston_off):
    """Only the runtimes caller opts out; the generic GET keeps its old default."""
    with pytest.raises(PistonNotEnabledError):
        piston_get("/api/v2/runtimes")


def test_transport_failure_still_raises_rather_than_returning_empty(piston_off, monkeypatch):
    """Never a guessed or empty runtimes list — an unreachable Piston must not
    look like a deployment that exposes no languages."""
    def _boom(*a, **k):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(PistonConnectionError):
        get_runtimes()


def test_result_is_cached_across_calls(piston_off, monkeypatch):
    calls = {"n": 0}

    def _get(*a, **k):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(requests, "get", _get)
    get_runtimes()
    get_runtimes()
    assert calls["n"] == 1
