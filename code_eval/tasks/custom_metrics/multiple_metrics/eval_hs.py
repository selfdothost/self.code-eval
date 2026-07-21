from pathlib import Path

from .safe_subprocess import run


def eval_script(path: Path):
    # Upstream's version had `elif "Syntax error":` (always truthy) — classify
    # on GHC's actual "error:" marker in stderr instead. Only "OK" affects
    # pass@k either way.
    r = run(["runghc", str(path)])
    if r.timeout:
        status = "Timeout"
    elif r.exit_code == 0:
        status = "OK"
    elif "error:" in r.stderr:
        status = "SyntaxError"
    else:
        status = "Exception"
    return {
        "status": status,
        "exit_code": r.exit_code,
        "stdout": r.stdout,
        "stderr": r.stderr,
    }
