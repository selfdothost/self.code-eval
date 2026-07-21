from pathlib import Path

from .safe_subprocess import run


def eval_script(path: Path):
    # -J-Dclojure.main.report=stderr keeps compiler/runtime error reports on
    # stderr instead of an EDN temp file. Success is clojure.test's summary
    # line, not exit code alone — `clojure -M` exits 0 even when tests fail.
    r = run(["clojure", "-J-Dclojure.main.report=stderr", "-M", str(path)])
    if r.timeout:
        status = "Timeout"
    elif r.exit_code != 0:
        status = "Exception"
    elif "\n0 failures, 0 errors.\n" in r.stdout:
        status = "OK"
    else:
        status = "Exception"
    return {
        "status": status,
        "exit_code": r.exit_code,
        "stdout": r.stdout,
        "stderr": r.stderr,
    }
