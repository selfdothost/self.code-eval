"""
NOTE: Nothing containerized about this any more. This is just a helper
for problem_evaluator.py.
"""

import tempfile
from pathlib import Path

from . import (eval_cpp, eval_dlang, eval_java, eval_javascript, eval_julia,
               eval_lua, eval_php, eval_python, eval_r, eval_racket, eval_ruby,
               eval_rust, eval_swift, eval_ts, eval_go, eval_pl, eval_sh, eval_scala, eval_cs,
               eval_clj, eval_dart, eval_elixir, eval_hs, eval_ocaml)

EVALUATORS = {
    "rb": (eval_ruby.eval_script, ".rb"),
    "lua": (eval_lua.eval_script, ".lua"),
    "python": (eval_python.eval_script, ".py"),
    "py": (eval_python.eval_script, ".py"),
    "notypes.py": (eval_python.eval_script, ".py"),
    "julia": (eval_julia.eval_script, ".jl"),
    "java": (eval_java.eval_script, ".java"),
    "rust": (eval_rust.eval_script, ".rs"),
    "rs": (eval_rust.eval_script, ".rs"),
    "swift": (eval_swift.eval_script, ".swift"),
    "lua": (eval_lua.eval_script, ".lua"),
    "racket": (eval_racket.eval_script, ".rkt"),
    "rkt": (eval_racket.eval_script, ".rkt"),
    "javascript": (eval_javascript.eval_script, ".js"),
    "js": (eval_javascript.eval_script, ".js"),
    "cpp": (eval_cpp.eval_script, ".cpp"),
    "cs": (eval_cs.eval_script, ".cs"),
    "php": (eval_php.eval_script, ".php"),
    "humaneval_to_dlang.py": (eval_dlang.eval_script, ".d"),
    "d": (eval_dlang.eval_script, ".d"),
    "r": (eval_r.eval_script, ".r"),
    "humaneval_to_r.py": (eval_r.eval_script, ".r"),
    "jl": (eval_julia.eval_script, ".jl"),
    "ts": (eval_ts.eval_script, ".ts"),
    "go": (eval_go.eval_script, ".go"),
    "go_test.go": (eval_go.eval_script, "_test.go"),
    "pl": (eval_pl.eval_script, ".pl"),
    "sh": (eval_sh.eval_script, ".sh"),
    "scala": (eval_scala.eval_script, ".scala"),
    "clj": (eval_clj.eval_script, ".clj"),
    "clojure": (eval_clj.eval_script, ".clj"),
    "dart": (eval_dart.eval_script, ".dart"),
    "elixir": (eval_elixir.eval_script, ".exs"),
    "exs": (eval_elixir.eval_script, ".exs"),
    "hs": (eval_hs.eval_script, ".hs"),
    "haskell": (eval_hs.eval_script, ".hs"),
    "ml": (eval_ocaml.eval_script, ".ml"),
    "ocaml": (eval_ocaml.eval_script, ".ml"),
}


def eval_string_script(language, program):
    # Opt-in Piston routing (multiple-seam kit R1 / T-011). Imported lazily and
    # checked first so that with ENABLE_PISTON_EXECUTION unset this function is
    # byte-identical to the local path below — the HTTP layer is never touched.
    from code_eval.piston.multiple_seam import (eval_string_script_piston,
                                                should_route_to_piston)

    if should_route_to_piston():
        return eval_string_script_piston(language, program)

    return _eval_string_script_local(language, program)


def _eval_string_script_local(language, program):
    """The original in-container path: per-language eval_*.py -> safe_subprocess.

    Unchanged from before the Piston seam existed; this is what runs whenever
    ENABLE_PISTON_EXECUTION is off.
    """
    if language in EVALUATORS:
        (eval_script, file_ext) = EVALUATORS[language]
    else:
        eval_module = __import__(
            f"eval_{language}" if language != "go_test.go" else "eval_go"
        )
        eval_script = eval_module.eval_script
        file_ext = f".{language}" if language != "go_test.go" else "_test.go"
    with tempfile.NamedTemporaryFile(suffix=file_ext, delete=True) as f:
        f.write(program.encode("utf-8"))
        f.flush()
        result = eval_script(Path(f.name))
        # Only save the first 2K of output from the running program. Any futher
        # output is very likely an exceptionally long stack trace or a long
        # series of prints.
        if type(result["stdout"]) == bytes:
            result["stdout"] = result["stdout"].decode("utf-8", errors="ignore")
        if result["stdout"] is None:
            result["stdout"] = ""
        if result["stderr"] is None:
            result["stderr"] = ""
        if type(result["stderr"]) == bytes:
            result["stderr"] = result["stderr"].decode("utf-8", errors="ignore")
        assert type(result["stdout"]) == str
        assert type(result["stderr"]) == str
        return {
            "program": program,
            "stdout": result["stdout"].replace("!!int", "")[:2048],
            "stderr": result["stderr"][:2048],
            "exit_code": result["exit_code"],
            "status": result["status"],
        }
