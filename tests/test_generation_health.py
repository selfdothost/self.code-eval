"""Generation delivery accounting (#10): did the infrastructure fail, or the model?

The bug these pin: a run in which every generation request 403'd still finished
exit 0 and wrote `{"humaneval": {"pass@1": 0.0}}`. Empty generations score as
failures, and 0.0 is a plausible number for a weak model, so nothing about the
artifact invited suspicion. Observed for real on job 91916120 (2026-08-03).

These exercise `code_eval/api_generation.py` directly with a stub session rather
than a real endpoint — the property under test is the accounting, not HTTP.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

# APPEND, never insert(0): the repo root contains a `main.py` (the harness CLI)
# and tests/api/ imports its own `main` (the control plane). Putting the root
# first makes `import main` resolve to the wrong module for every test in
# tests/api/ — 39 errors that appear only when both suites run in ONE pytest
# process, which the split CI jobs never do.
sys.path.append(str(Path(__file__).resolve().parents[1]))

from code_eval.api_generation import (  # noqa: E402
    AllGenerationsFailedError,
    GenerationHealth,
    api_parallel_generations,
)


def _args(**over):
    a = types.SimpleNamespace(
        load_generations_path=None, limit_start=0, prefix="", api_key=None,
        model="m", max_length_generation=64, n_samples=1, temperature=0.2,
        top_p=0.95, do_sample=True, postprocess=False, instruction_tokens=None,
        save_every_k_tasks=-1,
    )
    for k, v in over.items():
        setattr(a, k, v)
    return a


def _task():
    t = MagicMock(name="task")
    t.stop_words = []
    t.get_prompt.side_effect = lambda doc: doc["prompt"]
    t.postprocess_generation.side_effect = lambda text, idx: text
    return t


def _dataset(n):
    return [{"prompt": f"def f{i}():\n", "task_id": f"T/{i}"} for i in range(n)]


def _ok_response(text="    return 1\n"):
    r = MagicMock(name="response")
    r.status_code = 200
    r.raise_for_status.return_value = None
    r.json.return_value = {"choices": [{"text": text}]}
    return r


def _http_error(status):
    """A requests.HTTPError carrying a response, as raise_for_status produces."""
    resp = MagicMock(name="err_response")
    resp.status_code = status
    return requests.HTTPError(f"{status} Client Error", response=resp)


def _session_yielding(*outcomes):
    """Stub session whose POST returns a response or raises, per call."""
    session = MagicMock(name="session")

    def _post(*a, **k):
        outcome = outcomes[_post.calls]
        _post.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    _post.calls = 0
    session.post.side_effect = _post
    return session


@pytest.fixture
def patched_session(monkeypatch):
    """Install a stub for requests.Session so no socket is ever opened."""
    holder = {}

    def _install(*outcomes):
        s = _session_yielding(*outcomes)
        holder["session"] = s
        monkeypatch.setattr(requests, "Session", lambda: s)
        return s

    return _install


# ─── the counting itself ────────────────────────────────────────────────


def test_a_clean_run_reports_no_failures(patched_session):
    patched_session(_ok_response(), _ok_response(), _ok_response())
    health = GenerationHealth()
    gens = api_parallel_generations(
        _task(), _dataset(3), "http://api/v1/completions", 3, _args(), health=health
    )
    assert len(gens) == 3
    assert health.as_dict() == {
        "attempted": 3, "failed": 0, "succeeded": 3,
        "failure_rate": 0.0, "reasons": {},
    }


def test_a_partial_failure_is_counted_not_hidden(patched_session):
    """The score is still computed, but the artifact says it covers fewer
    samples than it appears to."""
    patched_session(_ok_response(), _http_error(500), _ok_response(), _http_error(500))
    health = GenerationHealth()
    api_parallel_generations(
        _task(), _dataset(4), "http://api/v1/completions", 4, _args(), health=health
    )
    d = health.as_dict()
    assert (d["attempted"], d["failed"], d["succeeded"]) == (4, 2, 2)
    assert d["failure_rate"] == 0.5
    assert d["reasons"] == {"http_500": 2}


def test_failure_reasons_separate_refusal_from_unreachable(patched_session):
    """`http_403` and `ConnectionError` are the difference between "the endpoint
    turned us away" and "we never got there" — the first thing you'd want to
    know when a run comes back empty."""
    patched_session(
        _http_error(403),
        requests.ConnectionError("no route to host"),
        _http_error(403),
        requests.Timeout("timed out"),
    )
    health = GenerationHealth()
    with pytest.raises(AllGenerationsFailedError):
        api_parallel_generations(
            _task(), _dataset(4), "http://api/v1/completions", 4, _args(), health=health
        )
    assert health.as_dict()["reasons"] == {
        "http_403": 2, "ConnectionError": 1, "Timeout": 1,
    }


# ─── refusing to score nothing ──────────────────────────────────────────


def test_a_total_outage_raises_instead_of_scoring_empty_generations(patched_session):
    """THE REGRESSION. Job 91916120 returned 164 empty strings here and was
    scored pass@1 0.0. Returning generations at all is the bug."""
    patched_session(*[_http_error(403)] * 5)
    with pytest.raises(AllGenerationsFailedError) as exc:
        api_parallel_generations(
            _task(), _dataset(5), "http://api/v1/completions", 5, _args()
        )
    msg = str(exc.value)
    assert "http_403x5" in msg
    assert "infrastructure failure, not a model result" in msg


def test_the_error_names_the_endpoint_that_failed(patched_session):
    """Which endpoint refused is the whole diagnostic — there is more than one
    in play (api-core, llamolotl, an external provider)."""
    patched_session(*[_http_error(401)] * 2)
    with pytest.raises(AllGenerationsFailedError) as exc:
        api_parallel_generations(
            _task(), _dataset(2), "http://selfai-api:80/api/completions", 2, _args()
        )
    assert "http://selfai-api:80/api/completions" in str(exc.value)


def test_nothing_attempted_is_not_treated_as_a_total_outage(patched_session):
    """n_tasks=0 (everything resumed from an intermediate file) must not look
    like an outage — 0 failures out of 0 is not 100% failure."""
    patched_session()
    health = GenerationHealth()
    gens = api_parallel_generations(
        _task(), _dataset(0), "http://api/v1/completions", 0, _args(), health=health
    )
    assert gens == []
    assert health.all_failed is False
    assert health.as_dict()["failure_rate"] == 0.0


def test_one_success_is_enough_to_not_refuse(patched_session):
    """The boundary. A single delivered generation means the model WAS reached,
    so the run is scoreable however bad the score turns out to be."""
    patched_session(*([_http_error(500)] * 9 + [_ok_response()]))
    health = GenerationHealth()
    gens = api_parallel_generations(
        _task(), _dataset(10), "http://api/v1/completions", 10, _args(), health=health
    )
    assert len(gens) == 10
    assert health.all_failed is False
    assert health.as_dict()["failed"] == 9


def test_health_is_created_even_when_the_caller_passes_none(patched_session):
    """The refusal must not depend on the caller opting in to accounting."""
    patched_session(*[_http_error(403)] * 3)
    with pytest.raises(AllGenerationsFailedError):
        api_parallel_generations(
            _task(), _dataset(3), "http://api/v1/completions", 3, _args()
        )


# ─── the artifact is what a reader actually sees ────────────────────────
#
# NOT covered here, deliberately rather than by oversight: evaluator.evaluate()
# attaching this record to results[task]["generation"]. Importing
# code_eval.evaluator pulls in code_eval.tasks, which imports apps_metric ->
# pyext -> the full harness runtime (torch/transformers/datasets). This file
# runs on the light `yard` runner, and installing that stack to assert one
# dict assignment is not a trade worth making. That leg is verified live
# against the deployed image instead, which is a stronger check than a mock
# of it would be — see the MR.
