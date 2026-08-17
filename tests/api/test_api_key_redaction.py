"""self.code-eval#11: a job's api_key must not survive into any artifact.

Found live: the key was written verbatim to results.json's config block, echoed
into the job log, stored in .jobs.json, returned by GET /api/jobs/{id} and the
creation 201, and passed to the eval subprocess in argv where any `ps` could
read it. The natural key to hand a job is the broadest one in the system, and
results.json is the artifact people copy around by hand — the worst possible
carrier for a credential.

The assertions here deliberately search for the SECRET ITSELF in the serialised
output rather than checking a field equals "***". A redaction that only fixes
the field you thought of is the failure mode this is guarding.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

_API_DIR = str(Path(__file__).resolve().parents[2] / "api")
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

# APPEND, never insert(0): the repo root also contains a `main.py` (the harness
# CLI entrypoint), and putting it ahead of `api/` on sys.path makes `import main`
# resolve to the wrong module for every test in this directory — including the
# sibling files, which is how it presents: 48 unrelated errors in test_auth.py
# and test_languages.py that vanish when this file is run alone.
_APP_DIR = str(Path(__file__).resolve().parents[2])
if _APP_DIR not in sys.path:
    sys.path.append(_APP_DIR)

from conftest import TEST_SERVICE_AUTH_SECRET, mint_test_ticket  # noqa: E402

SECRET = "sk-a-real-looking-admin-key-0123456789"


@pytest.fixture
def client(temp_workspace, monkeypatch):
    import auth as auth_module
    import main as main_module

    # Never actually launch an eval; capture what would have been launched.
    launched = {}

    def _popen(cmd, **kw):
        launched["cmd"] = list(cmd)
        launched["env"] = dict(kw.get("env") or {})
        proc = MagicMock(name="proc")
        proc.pid = 4321
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(main_module.subprocess, "Popen", _popen)
    # The harness registry is not importable in this light test env, so task
    # validation would reject every job before reaching the code under test.
    monkeypatch.setattr(main_module, "_get_all_tasks", lambda: ["humaneval"])

    original = auth_module.SERVICE_AUTH_SECRET
    auth_module.SERVICE_AUTH_SECRET = TEST_SERVICE_AUTH_SECRET
    c = TestClient(main_module.app)
    c.launched = launched
    yield c
    auth_module.SERVICE_AUTH_SECRET = original


def _hdr():
    return {"X-Selfai-Ticket": mint_test_ticket()}


def _create(client, **over):
    body = {
        "tasks": "humaneval",
        "api_endpoint": "http://selfai-api:80/api/completions",
        "api_key": SECRET,
        "model": "some-model",
    }
    body.update(over)
    resp = client.post("/api/jobs", json=body, headers=_hdr())
    assert resp.status_code == 201, resp.text
    return resp


# ─── the API surfaces ───────────────────────────────────────────────────


def test_the_creation_response_does_not_carry_the_key(client):
    """The 201 came straight back with it — the very first place it leaked."""
    resp = _create(client)
    assert SECRET not in resp.text
    assert resp.json()["config"]["api_key"].startswith("sha256:")


def test_get_job_does_not_carry_the_key(client):
    job_id = _create(client).json()["job_id"]
    resp = client.get(f"/api/jobs/{job_id}", headers=_hdr())
    assert resp.status_code == 200
    assert SECRET not in resp.text


def test_list_jobs_does_not_carry_the_key(client):
    _create(client)
    resp = client.get("/api/jobs", headers=_hdr())
    assert resp.status_code == 200
    assert SECRET not in resp.text


# ─── what lands on disk ─────────────────────────────────────────────────


def test_the_state_file_does_not_carry_the_key(client, temp_workspace):
    import main as main_module

    _create(client)
    assert SECRET not in main_module.JOBS_STATE_FILE.read_text()


def test_a_prefix_job_is_redacted_when_reloaded(client, temp_workspace, monkeypatch):
    """Jobs written before this fix still hold raw keys. Loading them unchanged
    would keep serving the key from GET /api/jobs/{id} forever."""
    import main as main_module

    main_module.JOBS_STATE_FILE.write_text(json.dumps({
        "old1": {
            "job_id": "old1", "status": "completed", "tasks": "humaneval",
            "model": "m", "api_endpoint": "http://x/api/completions",
            "created_at": "2026-08-01T00:00:00", "log_file": "/l", "results_file": "/r",
            "config": {"tasks": "humaneval", "api_key": SECRET},
        }
    }))
    monkeypatch.setattr(main_module, "_jobs", {})
    main_module._load_jobs()

    assert main_module._jobs["old1"].config["api_key"].startswith("sha256:")
    resp = client.get("/api/jobs/old1", headers=_hdr())
    assert SECRET not in resp.text


def test_reloading_rewrites_the_state_file_without_the_key(client, temp_workspace, monkeypatch):
    """Redacting in memory is not enough — the cleartext must leave DISK, and
    on an idle deployment nothing else would trigger a save."""
    import main as main_module

    main_module.JOBS_STATE_FILE.write_text(json.dumps({
        "old2": {
            "job_id": "old2", "status": "completed", "tasks": "humaneval",
            "model": "m", "api_endpoint": "http://x/api/completions",
            "created_at": "2026-08-01T00:00:00", "log_file": "/l", "results_file": "/r",
            "config": {"api_key": SECRET},
        }
    }))
    monkeypatch.setattr(main_module, "_jobs", {})
    main_module._load_jobs()
    main_module._save_jobs()

    assert SECRET not in main_module.JOBS_STATE_FILE.read_text()


# ─── the subprocess ─────────────────────────────────────────────────────


def test_the_key_is_not_in_the_subprocess_argv(client):
    """argv is readable by anything that can see /proc for the life of the job."""
    _create(client)
    cmd = client.launched["cmd"]
    assert "--api_key" not in cmd
    assert not any(SECRET in str(part) for part in cmd)


def test_the_key_still_reaches_the_subprocess_via_the_environment(client):
    """Redaction that broke authentication would be worse than the leak — the
    whole point is that the run still works."""
    _create(client)
    assert client.launched["env"].get("CODE_EVAL_API_KEY") == SECRET


def test_no_key_means_no_env_var(client):
    """An unauthenticated endpoint must not inherit a stale key from the pod's
    own environment by accident."""
    _create(client, api_key=None)
    assert "CODE_EVAL_API_KEY" not in client.launched["env"]


# ─── the redaction helper itself ────────────────────────────────────────


def test_fingerprints_are_stable_and_distinguish_keys():
    """Telling whether two runs used the same credential is a real question,
    and answerable without carrying the secret."""
    from code_eval.secrets import fingerprint

    assert fingerprint(SECRET) == fingerprint(SECRET)
    assert fingerprint(SECRET) != fingerprint(SECRET + "x")
    assert SECRET not in fingerprint(SECRET)


def test_absent_and_present_keys_are_distinguishable():
    from code_eval.secrets import fingerprint

    assert fingerprint(None) == "none"
    assert fingerprint("") == "none"
    assert fingerprint("k").startswith("sha256:")


def test_redaction_does_not_mutate_the_callers_config():
    """The running process still needs the real value; redacting for an
    artifact must not strip it out from under it."""
    from code_eval.secrets import redact_config

    original = {"api_key": SECRET, "tasks": "humaneval"}
    out = redact_config(original)
    assert original["api_key"] == SECRET
    assert out["api_key"] != SECRET
    assert out["tasks"] == "humaneval"


def test_booleans_are_left_readable():
    """`use_auth_token` is a bool in practice; fingerprinting it would destroy
    readable information and protect nothing."""
    from code_eval.secrets import redact_config

    assert redact_config({"use_auth_token": False})["use_auth_token"] is False
    assert redact_config({"use_auth_token": True})["use_auth_token"] is True


def test_only_exact_field_names_are_redacted():
    """Substring matching would mangle a field legitimately named something
    like api_key_required."""
    from code_eval.secrets import redact_config

    out = redact_config({"api_key_required": True, "model": "api_key-lookalike"})
    assert out["api_key_required"] is True
    assert out["model"] == "api_key-lookalike"
