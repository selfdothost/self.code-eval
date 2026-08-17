"""self.code-eval#6: runtime MultiPL-E language registration.

Adding a language is a parameter change, not a code path — the family is
generated and Piston already owns execution. What has to hold:

* all THREE checks gate registration (executor / invocable mapping / live
  runtime), because each failing alone yields a task that registers and then
  scores nothing,
* the check happens at ADD time, not mid-benchmark,
* the task list invalidates, or the language stays invisible until a restart,
* built-ins can never be dropped or shadowed.
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_API_DIR = str(Path(__file__).resolve().parents[2] / "api")
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

from conftest import TEST_SERVICE_AUTH_SECRET, mint_test_ticket  # noqa: E402


@pytest.fixture
def client(temp_workspace):
    """`auth.py` reads SERVICE_AUTH_SECRET into a module constant at import, so
    the attribute must be patched rather than the env var."""
    import auth as auth_module
    import main as main_module

    original = auth_module.SERVICE_AUTH_SECRET
    auth_module.SERVICE_AUTH_SECRET = TEST_SERVICE_AUTH_SECRET
    yield TestClient(main_module.app)
    auth_module.SERVICE_AUTH_SECRET = original


def _hdr(scope="tasks:read tasks:write"):
    return {"X-Selfai-Ticket": mint_test_ticket(scope=scope)}


def _runnable(token):
    return {"language": token, "executor": True, "dataset": True, "invocable": "zig",
            "runtime": "0.11.0", "runnable": True, "detail": None}


def _not_runnable(token, detail):
    return {"language": token, "executor": True, "dataset": True, "invocable": None,
            "runtime": None, "runnable": False, "detail": detail}


@pytest.mark.tier1
def test_registering_a_runnable_language_persists_and_invalidates(client, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs"])
    monkeypatch.setattr(main_module, "_language_report", lambda t: _runnable(t))
    monkeypatch.setattr(main_module, "_ALL_TASKS", ["stale"])

    resp = client.post("/api/languages", json={"language": "zig"}, headers=_hdr())
    assert resp.status_code == 201, resp.text
    assert resp.json()["task"] == "multiple-zig"

    assert json.loads(main_module.CUSTOM_LANGUAGES_FILE.read_text()) == ["zig"]
    # Otherwise the language stays invisible until a pod restart.
    assert main_module._ALL_TASKS == []


@pytest.mark.tier1
def test_a_language_with_no_live_runtime_is_refused(client, monkeypatch):
    """The table mapping a token is not evidence the deployment can run it."""
    import main as main_module

    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs"])
    monkeypatch.setattr(
        main_module, "_language_report",
        lambda t: _not_runnable(t, "no runtime present for 'zig'"),
    )
    resp = client.post("/api/languages", json={"language": "zig"}, headers=_hdr())
    assert resp.status_code == 422
    assert "no runtime" in json.dumps(resp.json())
    # Nothing persisted — a refused language must not leave a task behind.
    assert not main_module.CUSTOM_LANGUAGES_FILE.exists()


@pytest.mark.tier1
def test_a_language_with_no_executor_is_refused(client, monkeypatch):
    import main as main_module

    report = {"language": "nim", "executor": False, "invocable": None, "runtime": None,
              "runnable": False, "detail": "No eval_*.py executor for 'nim'"}
    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs"])
    monkeypatch.setattr(main_module, "_language_report", lambda t: report)
    resp = client.post("/api/languages", json={"language": "nim"}, headers=_hdr())
    assert resp.status_code == 422
    assert "executor" in json.dumps(resp.json())


@pytest.mark.tier1
def test_builtin_languages_cannot_be_registered(client, monkeypatch):
    """`rs` already exists; re-registering it would be a no-op that looks real."""
    import main as main_module

    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs", "go"])
    resp = client.post("/api/languages", json={"language": "rs"}, headers=_hdr())
    assert resp.status_code == 409
    assert "built-in" in resp.json()["detail"]


@pytest.mark.tier1
def test_registration_fails_closed_when_the_builtin_set_is_unknown(client, monkeypatch):
    """If the registry is not importable we cannot tell whether the token would
    shadow a built-in, and a shadow is not recoverable by inspection later."""
    import main as main_module

    monkeypatch.setattr(main_module, "_builtin_languages", lambda: None)
    monkeypatch.setattr(main_module, "_language_report", lambda t: _runnable(t))
    resp = client.post("/api/languages", json={"language": "zig"}, headers=_hdr())
    assert resp.status_code == 503
    assert not main_module.CUSTOM_LANGUAGES_FILE.exists()


@pytest.mark.tier1
@pytest.mark.parametrize("bad", ["../escape", "With/Slash", "", "-lead", "x" * 40, "has space"])
def test_unsafe_tokens_are_refused(client, bad):
    resp = client.post("/api/languages", json={"language": bad}, headers=_hdr())
    assert resp.status_code in (400, 422)


@pytest.mark.tier1
def test_token_case_is_normalised_not_rejected(client, monkeypatch):
    """Tokens are lowercased before validation, so 'ZIG' is a valid way to say
    'zig' rather than an error -- and it must land in the file lowercased, since
    that is the form multiple.py matches against."""
    import main as main_module

    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs"])
    monkeypatch.setattr(main_module, "_language_report", lambda t: _runnable(t))

    resp = client.post("/api/languages", json={"language": "  ZIG  "}, headers=_hdr())
    assert resp.status_code == 201
    assert resp.json()["task"] == "multiple-zig"
    assert json.loads(main_module.CUSTOM_LANGUAGES_FILE.read_text()) == ["zig"]


@pytest.mark.tier1
def test_write_requires_tasks_write_scope(client, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs"])
    monkeypatch.setattr(main_module, "_language_report", lambda t: _runnable(t))
    resp = client.post("/api/languages", json={"language": "zig"}, headers=_hdr("tasks:read"))
    assert resp.status_code == 403


@pytest.mark.tier1
def test_check_endpoint_reports_without_registering(client, monkeypatch):
    import main as main_module

    monkeypatch.setattr(
        main_module, "_language_report",
        lambda t: _not_runnable(t, "no runtime present"),
    )
    resp = client.get("/api/languages/zig/check", headers=_hdr())
    assert resp.status_code == 200
    assert resp.json()["runnable"] is False
    assert not main_module.CUSTOM_LANGUAGES_FILE.exists()


@pytest.mark.tier1
def test_registered_language_appears_in_task_discovery(client, monkeypatch):
    import main as main_module

    import types

    fake = types.ModuleType("code_eval.tasks")
    fake.ALL_TASKS = ["humaneval", "multiple-rs"]
    monkeypatch.setitem(sys.modules, "code_eval.tasks", fake)
    monkeypatch.setattr(main_module, "_read_custom_languages", lambda: ["zig"])
    monkeypatch.setattr(main_module, "_ALL_TASKS", [])

    names = main_module._discover_tasks()
    assert "multiple-zig" in names
    # The union must not drop what the registry already reported.
    assert "multiple-rs" in names and "humaneval" in names


@pytest.mark.tier1
def test_delete_removes_and_invalidates(client, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs"])
    monkeypatch.setattr(main_module, "_language_report", lambda t: _runnable(t))
    assert client.post("/api/languages", json={"language": "zig"}, headers=_hdr()).status_code == 201

    monkeypatch.setattr(main_module, "_ALL_TASKS", ["stale"])
    resp = client.delete("/api/languages/zig", headers=_hdr())
    assert resp.status_code == 200
    assert json.loads(main_module.CUSTOM_LANGUAGES_FILE.read_text()) == []
    assert main_module._ALL_TASKS == []


@pytest.mark.tier1
def test_delete_unknown_language_is_404(client):
    assert client.delete("/api/languages/never", headers=_hdr()).status_code == 404


@pytest.mark.tier1
def test_list_reports_builtin_and_custom_separately(client, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module, "_read_custom_languages", lambda: ["zig"])
    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs", "go"])
    body = client.get("/api/languages", headers=_hdr()).json()
    assert body["custom"] == ["zig"]
    assert "rs" in body["builtin"] and "zig" not in body["builtin"]


@pytest.mark.tier1
def test_malformed_languages_file_degrades_to_none(client, monkeypatch):
    """A broken file must not take out task discovery for every language."""
    import main as main_module

    main_module.CUSTOM_LANGUAGES_FILE.write_text("not json")
    assert main_module._read_custom_languages() == []
    main_module.CUSTOM_LANGUAGES_FILE.write_text('{"not": "a list"}')
    assert main_module._read_custom_languages() == []


def _stub_piston(monkeypatch, executor_tokens, resolve=None):
    """Stand in for code_eval.piston.* so _language_report is exercisable
    without the harness's heavy dependencies."""
    import types

    pkg = types.ModuleType("code_eval.piston")
    errors = types.ModuleType("code_eval.piston.errors")

    class UnknownLanguageError(Exception):
        pass

    errors.UnknownLanguageError = UnknownLanguageError

    languages = types.ModuleType("code_eval.piston.languages")
    def _resolve(token, runtimes):
        if resolve is None:
            raise UnknownLanguageError(f"no runtime for {token}")
        return resolve
    languages.resolve_language = _resolve

    seam = types.ModuleType("code_eval.piston.multiple_seam")
    seam.executor_backed_tokens = lambda *a, **k: frozenset(executor_tokens)

    runtimes_mod = types.ModuleType("code_eval.piston.runtimes")
    runtimes_mod.get_runtimes = lambda *a, **k: []

    for name, mod in [
        ("code_eval.piston", pkg),
        ("code_eval.piston.errors", errors),
        ("code_eval.piston.languages", languages),
        ("code_eval.piston.multiple_seam", seam),
        ("code_eval.piston.runtimes", runtimes_mod),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)


# ─── dataset existence (self.code-eval#7) ──────────────────────────────


@pytest.mark.tier1
def test_a_language_with_no_multiple_dataset_config_is_refused(client, monkeypatch):
    """The case that exposed this: `python` passes every EXECUTION check but
    MultiPL-E has no humaneval-python config, so the task would fail at load."""
    import main as main_module

    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs"])
    monkeypatch.setattr(main_module, "_multiple_dataset_languages", lambda **k: frozenset({"rs", "go"}))
    # `executor_backed_tokens` is imported INSIDE _language_report, and the
    # piston deps are not installed in the API test env — so the modules are
    # stubbed in sys.modules rather than patched on main.
    _stub_piston(monkeypatch, executor_tokens={"python"})

    report = main_module._language_report("python")
    # Executor says yes; the dataset says no, and that is decisive.
    assert report["dataset"] is False
    assert report["runnable"] is False
    assert "humaneval-python" in report["detail"]

    resp = client.post("/api/languages", json={"language": "python"}, headers=_hdr())
    assert resp.status_code == 422
    assert not main_module.CUSTOM_LANGUAGES_FILE.exists()


@pytest.mark.tier1
def test_unreachable_huggingface_refuses_and_says_so(client, monkeypatch):
    """An outage must not be reported as a problem with the language, and must
    not be silently treated as 'no configs exist'."""
    import main as main_module

    def _boom(**k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main_module, "_builtin_languages", lambda: ["rs"])
    monkeypatch.setattr(main_module, "_multiple_dataset_languages", _boom)
    # Without this the report short-circuits one leg earlier, on "piston is not
    # importable", and would pass an assertion about the WRONG refusal reason.
    _stub_piston(monkeypatch, executor_tokens={"go"})

    resp = client.post("/api/languages", json={"language": "go"}, headers=_hdr())
    assert resp.status_code == 422
    body = json.dumps(resp.json())
    assert "reachability" in body
    assert not main_module.CUSTOM_LANGUAGES_FILE.exists()


@pytest.mark.tier1
def test_dataset_language_set_is_parsed_and_cached(monkeypatch):
    import main as main_module

    calls = {"n": 0}

    class _Resp:
        status_code = 200

        def json(self):
            calls["n"] += 1
            return {"splits": [
                {"config": "humaneval-rs"}, {"config": "humaneval-go"},
                {"config": "mbpp-rs"},  # a different family — must not be counted
            ]}

    import requests as _requests

    monkeypatch.setattr(main_module, "_MULTIPLE_CONFIGS", None)
    monkeypatch.setattr(_requests, "get", lambda *a, **k: _Resp())

    got = main_module._multiple_dataset_languages()
    assert got == frozenset({"rs", "go"})
    main_module._multiple_dataset_languages()
    assert calls["n"] == 1, "config list should be memoised"


@pytest.mark.tier1
def test_empty_config_list_raises_rather_than_refusing_everything_silently(monkeypatch):
    import main as main_module

    class _Resp:
        status_code = 200

        def json(self):
            return {"splits": []}

    import requests as _requests

    monkeypatch.setattr(main_module, "_MULTIPLE_CONFIGS", None)
    monkeypatch.setattr(_requests, "get", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError):
        main_module._multiple_dataset_languages()
