from pathlib import Path

from .safe_subprocess import run


def eval_script(path: Path):
    # Runs in .exs script mode. Generated tests use ExUnit's assert macros
    # (stdlib) — their "Assertion with == failed" failure text is what
    # distinguishes a wrong answer from a crash.
    r = run(["elixir", str(path)])
    combined = r.stdout + r.stderr
    if r.timeout:
        status = "Timeout"
    elif r.exit_code == 0:
        status = "OK"
    elif "Assertion with == failed" in combined:
        status = "AssertionError"
    elif "SyntaxError" in combined:
        status = "SyntaxError"
    else:
        status = "Exception"
    return {
        "status": status,
        "exit_code": r.exit_code,
        "stdout": r.stdout,
        "stderr": r.stderr,
    }
