---
created: "2026-07-18"
last_edited: "2026-07-18"
---

# Cavekit: MultiPL-E Missing Executors (clojure, dart, elixir, haskell, ocaml)

## Scope

Port the five still-missing MultiPL-E per-language scoring executors —
Clojure, Dart, Elixir, Haskell, OCaml — from upstream `nuprl/MultiPL-E`
into `code_eval/tasks/custom_metrics/multiple_metrics/`, install the
corresponding language runtimes in the Docker image, and wire each
executor into the `EVALUATORS` dispatch table so the existing
`multiple-clj` / `multiple-dart` / `multiple-elixir` / `multiple-hs` /
`multiple-ml` tasks actually score generations instead of erroring at the
scoring step.

This carries forward the same 5-language remainder from the pre-rename
repo's `self.bigcode-eval-deletion_scheduled-6#1` ("Install missing
MultiPL-E language toolchains (8 languages)") — D, Julia, and Swift from
that original 8-language list were already resolved by this repo's closed
`#1` (Java jar/classpath, Lua 5.3 + luaunit, D `rdmd`, Julia + precompile
warm, Swift). This is item 1 of `self.code-eval#2`.

### Confirmed current state (2026-07-18, re-checked against `main`)

- `code_eval/tasks/multiple.py`'s `LANGUAGES` list already contains `clj`,
  `dart`, `elixir`, `hs`, `ml` alongside the 18 already-working languages.
  `create_all_tasks()` builds a `GeneralMultiPLE` subclass for every entry
  unconditionally — **the tasks `multiple-clj`, `multiple-dart`,
  `multiple-elixir`, `multiple-hs`, `multiple-ml` are already registered
  and selectable today**, and generation (dataset load, prompting,
  postprocessing) already works, because none of that path touches the
  per-language executor. Confirmed via `code_eval/tasks/__init__.py`
  (`**multiple.create_all_tasks()` is unconditionally merged into the
  global task registry) and via the HF `datasets-server` API against
  `nuprl/MultiPL-E` configs `humaneval-clj`/`humaneval-dart`/
  `humaneval-elixir`/`humaneval-hs`/`humaneval-ml` (all resolve, all have a
  populated `test` split).
- What breaks is **scoring**: `GeneralMultiPLE.process_results()` →
  `evaluation.evaluate_problem()` → `containerized_eval.eval_string_script(
  language, program)`, where `language` is `doc["language"]` from the
  dataset row. Confirmed the exact string each language needs to be keyed
  by (from `datasets-server` `first-rows` on each config's `test` split):
  `clj`, `dart`, `elixir`, `hs`, `ml`. **None of these five keys exist in
  the `EVALUATORS` dict in `containerized_eval.py`.** The dict has an
  `__import__(f"eval_{language}")` fallback for unknown keys, but that's an
  absolute import against a package-relative module (`eval_clj` etc. only
  exist as `code_eval.tasks.custom_metrics.multiple_metrics.eval_clj`) — it
  is unverified whether this fallback resolves at all in this repo's
  execution context, and it should not be relied on. Treat "add the
  explicit `EVALUATORS` entry" as required work, not optional.
- No runtime for any of the five languages is installed in `Dockerfile`
  today — grep of the file confirms no `clojure`, `dart`, `elixir`/`erlang`,
  `ghc`, or `ocaml` install step exists anywhere. This matches the issue's
  framing: "installing a runtime alone won't score them" implies the
  runtime is a separate missing piece too, and it is — both halves (runtime
  + eval script) are absent for all five languages.
- No `eval_clj.py` / `eval_dart.py` / `eval_elixir.py` / `eval_hs.py` /
  `eval_ocaml.py` exists in `code_eval/tasks/custom_metrics/multiple_metrics/`
  (confirmed by directory listing: only cpp/cs/dlang/go/java/javascript/
  julia/lua/php/pl/python/r/racket/ruby/rust/scala/sh/swift/ts have
  executors).

### The common executor interface (read from `eval_java.py`, `eval_dlang.py`,
`eval_lua.py`, `eval_pl.py`)

Every `eval_<lang>.py` in this repo exports one function:

```python
def eval_script(path: Path) -> dict:
    # returns {"status": str, "exit_code": int, "stdout": str, "stderr": str}
```

`status` is one of `"OK"`, `"Timeout"`, `"SyntaxError"`, `"Exception"`, (and
occasionally `"AssertionError"` in upstream scripts, though none of this
repo's current executors use that value — `multiple.py`'s
`process_results()` only checks for `status == "OK" and exit_code == 0` as
"passed", so any other status string is scored as a fail; exact string
choice among the non-OK statuses doesn't change scoring, only debug
legibility).

The module is invoked two ways:
1. **Harness path (the one that matters for scoring):**
   `containerized_eval.py`'s `EVALUATORS` dict maps a `language` key to
   `(eval_script_fn, file_extension)`; `eval_string_script()` writes the
   generation + reference tests to a `NamedTemporaryFile` with that
   extension and calls `eval_script_fn(path)`. This is a relative import
   (`from . import eval_java`, etc.) — new modules must follow the same
   `from .safe_subprocess import run` / `from .generic_eval import main`
   relative-import style, not upstream's bare `from safe_subprocess import
   run`.
2. **Standalone CLI path (optional, cosmetic):** some scripts (`eval_java.py`,
   `eval_dlang.py`) have an `if __name__ == "__main__": main(eval_script,
   LANG_NAME, LANG_EXT)` block using `generic_eval.main()` to batch-score a
   directory of files outside the harness. Others (`eval_lua.py`,
   `eval_pl.py`) skip this entirely — it's not required for the harness to
   work. Upstream `eval_clj.py` has a dead `if __name__ == "__main__":
   main()` that calls an undefined `main` (bug in upstream, never fixed) —
   **do not port that block as-is**; either drop it (matching the
   `eval_lua.py`/`eval_pl.py` precedent) or wire it to `generic_eval.main`
   properly if a standalone CLI is wanted.

All execution goes through `safe_subprocess.run(args, timeout_seconds=15,
env=None) -> Result` (`Result` has `.timeout`, `.exit_code`, `.stdout`,
`.stderr`), which runs in a fresh process group and SIGKILLs the whole group
on timeout. Every existing executor uses this, and every ported executor
must too — **do not port upstream's raw `subprocess.run`/`subprocess.Popen`
calls verbatim** (see R3 and R5 below, both of which use a different
pattern upstream).

### Upstream source (pinned for reproducibility)

Fetched from `nuprl/MultiPL-E` at commit `3025a531af7450e7df8b96fe0440e9804480bbad`
(`main` branch, `evaluation/src/`), 2026-07-18. Re-fetch and diff against
this SHA if upstream has moved on by the time this kit is implemented — the
recipes below (esp. the elixir/clojure/dart install steps, transcribed from
`evaluation/Dockerfile` at the same commit) may have shifted.

## Requirements

### R1: OCaml executor (`multiple-ml`)
**Description:** Port `eval_ocaml.py`. Simplest of the five — upstream
invokes the `ocaml` toplevel directly as a script interpreter (bytecode,
not `ocamlfind ocamlopt` ahead-of-time compilation), one subprocess call,
same shape as this repo's existing `eval_lua.py`.

Upstream reference (verbatim, to adapt):
```python
def eval_script(path: Path):
    r = run(["ocaml", str(path)])
    if r.timeout:
        status = "Timeout"
    elif r.exit_code == 0:
        status = "OK"
    elif "Assert_failure" in r.stderr:
        status = "AssertionError"
    elif "Syntax error" in r.stderr:
        status = "SyntaxError"
    else:
        status = "Exception"
    return {"status": status, "exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr}
```

**Acceptance Criteria:**
- [ ] Dockerfile installs `ocaml` and `ocaml-interp` via `apt-get` (matches
  upstream's `evaluation/Dockerfile` package list; no version pin available
  or needed — this is a stable Ubuntu 22.04 archive package, unlike the
  D/Julia/Swift tarball installs elsewhere in this Dockerfile)
- [ ] `ocaml -version` (or equivalent) succeeds in the built image
- [ ] `code_eval/tasks/custom_metrics/multiple_metrics/eval_ocaml.py` exists,
  exports `eval_script(path: Path) -> dict`, uses `from .safe_subprocess
  import run` (relative import, not upstream's bare import)
- [ ] `EVALUATORS` dict in `containerized_eval.py` gains an `"ml": (
  eval_ocaml.eval_script, ".ml")` entry (key confirmed as `"ml"` via the
  `humaneval-ml` dataset config's `language` field, not `"ocaml"`)
- [ ] `eval_string_script("ml", <trivial passing program + test>)` returns
  `status == "OK"` when run against a hand-written known-good OCaml
  HumanEval-style solution
- [ ] `eval_string_script("ml", <trivial failing program>)` returns a
  non-`"OK"` status (confirms fail path isn't silently swallowed)
- [ ] A full `multiple-ml` task run (small `limit`, e.g. 2-3 problems)
  against a live generation source completes and produces a non-zero,
  non-error `pass@1` in the results JSON
**Dependencies:** none
**Risk:** Low. No cold-start or compile-time concern — `ocaml` toplevel
startup is fast and comparable to the already-supported interpreted
languages (lua, python, ruby).

### R2: Haskell executor (`multiple-hs`)
**Description:** Port `eval_hs.py`. Upstream uses `runghc`, GHC's *script
interpreter mode* — **this is not ahead-of-time compilation** (there is no
`ghc --make`/`ghc -o` step). Flagging this explicitly because GHC's
compile times are notoriously slow when building an optimized binary, but
`runghc` sidesteps that by interpreting the Haskell source directly against
GHC's bytecode interpreter, at the cost of needing to load the package
database (`base`, `Prelude`, etc.) on every invocation.

Upstream reference (verbatim, to adapt — note the upstream `elif "Syntax
error":` line is a bug: a non-empty string literal is always truthy, so
that branch is dead code and every non-timeout/non-zero-exit run falls
through to `"Exception"`; decide whether to fix this or preserve upstream
behavior for scoring parity):
```python
def eval_script(path: Path):
    r = run(["runghc", str(path)])
    if r.timeout:
        status = "Timeout"
    elif r.exit_code == 0:
        status = "OK"
    elif "Syntax error":  # BUG upstream: always-true condition, dead branch
        status = "SyntaxError"
    else:
        status = "Exception"
    return {"status": status, "exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr}
```

**Acceptance Criteria:**
- [ ] Dockerfile installs `ghc` via `apt-get` (provides `runghc`)
- [ ] `runghc --version` succeeds in the built image
- [ ] `eval_hs.py` exists, exports `eval_script`, relative imports; the
  upstream `elif "Syntax error":` always-true bug is either fixed (real
  string containment check against `r.stderr`) or explicitly left as-is
  with a comment noting why (decision, not silently copied without
  awareness)
- [ ] `EVALUATORS` dict gains `"hs": (eval_hs.eval_script, ".hs")`
  (key confirmed as `"hs"` via `humaneval-hs` dataset config)
- [ ] Hand-written passing/failing Haskell smoke tests both score correctly
  via `eval_string_script("hs", ...)`
- [ ] Per-problem wall time measured on a cold vs. warm container (analogous
  to the Julia `Test`-precompile lesson already documented in this
  Dockerfile) — if `runghc`'s package-db load pushes single-problem time
  close to the default `timeout_seconds=15`, either bump the timeout for
  this language or investigate a GHC package-cache warm step; record
  whichever decision is made and why
- [ ] Small-`limit` `multiple-hs` task run completes with a plausible
  `pass@1`
**Dependencies:** none
**Risk:** Medium. Not a compile-time problem (no AOT compile in this eval
path), but `runghc` package-db loading per invocation is a real,
under-documented cold-start cost upstream doesn't address — this repo has
already been burned once by an unmeasured cold-start assumption (Julia:
scored 0 cold, 1.0 warm, fixed by pre-warming the precompile cache at
image-build time). Measure before assuming the default 15s timeout is
sufficient.

### R3: Dart executor (`multiple-dart`)
**Description:** Port `eval_dart.py`. Unlike every other executor read
during this kit (Java, D, Lua, Perl, and the OCaml/Haskell/Elixir/Clojure
scripts above), Dart's upstream `eval_script` makes **two** subprocess
calls per problem: `dart analyze --no-fatal-warnings` first (returns
`SyntaxError` early on non-zero exit, without running anything), then
`dart <path>` to actually execute.

Upstream reference (verbatim, to adapt):
```python
def eval_script(path: Path):
    r = run(["dart", "analyze", "--no-fatal-warnings", str(path)], timeout_seconds=15)
    if r.exit_code != 0:
        return {"status": "SyntaxError", "exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr}
    r = run(["dart", str(path)], timeout_seconds=15)
    if r.timeout:
        status = "Timeout"
    elif r.exit_code == 0:
        status = "OK"
    else:
        status = "Exception"
    return {"status": status, "exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr}
```

**Acceptance Criteria:**
- [ ] Dockerfile adds Google's Dart apt repo (signing key + `sources.list.d`
  entry, per upstream's `evaluation/Dockerfile` recipe against
  `storage.googleapis.com/download.dartlang.org`) and installs `dart`
- [ ] Note: unlike D/Julia/Swift in this Dockerfile (pinned tarball
  versions), Dart's apt `stable` channel install has no explicit version
  pin available the same way — flag this inconsistency in the Dockerfile
  comment rather than silently matching the other entries' pinning style
- [ ] `dart --version` succeeds in the built image
- [ ] `eval_dart.py` exists, exports `eval_script`, both `run()` calls keep
  an **explicit** `timeout_seconds=15` (or repo-chosen value) rather than
  relying on the default — this repo's own issue #2 item 8 (Racket) flags
  a missing explicit `timeout=` as a likely cause of a scoring crash; don't
  repeat that omission here on a script that already makes two calls per
  problem (worst case ~2x single-call wall time if both legs are slow)
- [ ] `EVALUATORS` dict gains `"dart": (eval_dart.eval_script, ".dart")`
- [ ] Hand-written smoke test confirms a program with a static-analysis
  error is scored `SyntaxError` (first call's early-return path exercised,
  not just the happy path)
- [ ] Hand-written passing/failing Dart smoke tests score correctly
- [ ] Small-`limit` `multiple-dart` task run completes with a plausible
  `pass@1`
**Dependencies:** none
**Risk:** Low-medium. Dart VM startup itself is fast, but the two-call
pattern doubles subprocess spawns per problem versus every other executor
in this repo — mostly a latency/throughput concern for large eval runs,
not a correctness risk.

### R4: Clojure executor (`multiple-clj`)
**Description:** Port `eval_clj.py`. Runs on the **JVM** — this repo
already has `default-jdk` installed (for Java/Scala), so the JVM itself is
not new, but Clojure's own runtime (the `clojure` CLI/tools, `clojure.main`)
is not present and its upstream install path is a shell script from
`clojure/brew-install`, not an apt package.

Upstream reference (verbatim, to adapt — note the `if __name__ ==
"__main__": main()` call at the bottom is upstream dead code, `main` is
never defined/imported in this file; drop it per the interface note above):
```python
def eval_script(path: Path):
    result = run(["clojure", "-J-Dclojure.main.report=stderr", "-M", str(path)])
    if result.timeout:
        status = "Timeout"
    elif result.exit_code != 0:
        status = "Exception"
    elif "\n0 failures, 0 errors.\n" in result.stdout:
        status = "OK"
    else:  # test failure
        status = "Exception"
    return {"status": status, "exit_code": result.exit_code, "stdout": result.stdout, "stderr": result.stderr}
```

**Acceptance Criteria:**
- [ ] Dockerfile installs Clojure via the official `clojure/brew-install`
  `linux-install.sh` script (upstream's recipe), prefixed under `/opt`
  (e.g. `--prefix /opt/clojure`) to match this repo's existing convention
  for non-apt installs (D → `/opt/dlang`, Julia → `/opt/julia-*`, Swift →
  `/opt/swift-*`) rather than upstream's root-level `/clojure`
- [ ] Build step runs `clojure -P` once at image-build time (upstream does
  this too) to pre-resolve/download the default deps and populate the
  classpath cache — **do not skip this**: it is this repo's own
  Julia-precompile lesson applied preemptively. Without it, the first
  `clojure -M` invocation inside a scoring run pays a network/resolution
  cost that could false-negative as a timeout under the harness's per-problem
  budget, exactly like the pre-fix Julia symptom (scored 0 cold, 1.0 warm)
- [ ] `clojure -e "(+ 1 1)"` (or equivalent smoke command) succeeds in the
  built image
- [ ] `eval_clj.py` exists, exports `eval_script`, drops the dead
  `if __name__ == "__main__": main()` block (or wires it to
  `generic_eval.main` properly — see interface note above)
- [ ] `EVALUATORS` dict gains `"clj": (eval_clj.eval_script, ".clj")`
- [ ] Hand-written passing/failing Clojure smoke tests score correctly
  (note the pass condition is a string match on `"\n0 failures, 0
  errors.\n"` in stdout, not just exit code — this depends on whatever
  Clojure test-reporting library the translated MultiPL-E test harness
  code uses in its `tests` field; confirm this string actually appears in
  a real `humaneval-clj` reference-test run before trusting the port)
- [ ] Per-problem wall time measured cold vs. warm (same reasoning as R2)
  — JVM + Clojure startup (loading `clojure.core`, etc.) is commonly
  1-3+ seconds even warm, markedly slower than a bare `java -cp` invocation
  (which this repo's `eval_java.py` already pays, but Clojure adds its own
  runtime bootstrap on top of the JVM's); confirm the default
  `timeout_seconds=15` budget comfortably covers this before shipping
- [ ] Small-`limit` `multiple-clj` task run completes with a plausible
  `pass@1`
**Dependencies:** none (does not depend on R2/Java's javatuples jar or
classpath — Clojure's own `clojure -P`/deps.edn resolution is independent)
**Risk:** Medium-high. Two compounding cold-start costs (JVM boot +
Clojure runtime bootstrap) on top of a non-apt install path that must be
gotten right (prefix, `clojure -P` prewarm) or the language silently
scores near-zero the same way Java and Julia did before their respective
fixes in this repo's closed issue #1.

### R5: Elixir executor (`multiple-elixir`)
**Description:** Port `eval_elixir.py`. Runs on the **BEAM** (Erlang VM).
This is the most involved runtime install of the five — upstream does not
use an apt package for either Erlang or Elixir (Ubuntu 22.04's `erlang`
apt package is typically too old for a current Elixir release); it fetches
prebuilt Erlang/OTP and Elixir tarballs from `hex.pm` and installs both
onto `PATH`.

Upstream reference (verbatim, to adapt — **note this uses raw
`subprocess.run` with `timeout=5`, not this repo's `safe_subprocess.run`
pattern; must be rewritten to match**):
```python
def eval_script(path: Path):
    try:
        output = subprocess.run(["elixir", str(path)], capture_output=True, timeout=5)
        if output.returncode == 0:
            status = "OK"
        else:
            outmessage = str(output)
            if "Assertion with == failed" in outmessage:
                status = "AssertionError"
            elif "SyntaxError" in outmessage:
                status = "SyntaxError"
            else:
                status = "Exception"
        returncode = output.returncode
    except subprocess.TimeoutExpired as exc:
        status = "Timeout"
        output = exc
        returncode = -1
    return {"status": status, "exit_code": returncode,
            "stdout": "" if output.stdout is None else output.stdout.decode("utf-8"),
            "stderr": "" if output.stderr is None else output.stderr.decode("utf-8")}
```

Upstream's install recipe (from `evaluation/Dockerfile` at the pinned SHA,
to adapt to this repo's `/opt`-prefix convention):
```
# Erlang/OTP (minimal install, from hex.pm's prebuilt tarball, not apt)
wget https://builds.hex.pm/builds/otp/ubuntu-22.04/OTP-27.0.tar.gz
tar xzf ... && ./Install -minimal <prefix>
apt-get install ca-certificates libodbc1 libssl3 libsctp1   # Erlang runtime deps
# Elixir (prebuilt zip, from hex.pm, matched to the OTP version above)
wget https://builds.hex.pm/builds/elixir/v1.17.2-otp-27.zip
unzip elixir.zip
PATH="<erlang-prefix>/bin:<elixir-prefix>/bin:${PATH}"
ENV LANG=C.UTF-8
```

**Acceptance Criteria:**
- [ ] Dockerfile installs Erlang/OTP from the pinned hex.pm prebuilt tarball
  (matching upstream's `OTP-27.0`, or a documented newer pin if updated),
  under an `/opt`-prefixed path per this repo's convention, plus the
  Erlang runtime's own apt dependencies (`libodbc1`, `libssl3`, `libsctp1`,
  `ca-certificates`)
- [ ] Dockerfile installs Elixir from the pinned hex.pm prebuilt zip,
  version-matched to the installed OTP version (upstream pins
  `v1.17.2-otp-27` against `OTP-27.0` — a mismatched pairing is a known
  Elixir failure mode, not just a version-bump nicety)
- [ ] `PATH` includes both the Erlang and Elixir `bin` dirs; `LANG=C.UTF-8`
  set (upstream sets this explicitly — Elixir's compiler emits warnings
  about locale otherwise, which is likely noise in stderr that the eval
  script's string-matching (`"SyntaxError" in outmessage`) could
  false-positive on if not set)
- [ ] `elixir --version` succeeds in the built image
- [ ] `eval_elixir.py` exists, exports `eval_script`, **rewritten to use
  `from .safe_subprocess import run` instead of upstream's raw
  `subprocess.run`/`TimeoutExpired`** — this is the one script among the
  five that most needs adapting, not just import-path tweaking, since its
  control flow (try/except TimeoutExpired) doesn't match this repo's
  `Result`-object pattern used by every other executor
- [ ] Whatever `timeout_seconds` is chosen, it's a **deliberate** decision,
  not a silent carry-over of upstream's tight `timeout=5` — BEAM VM
  startup (`elixir <file>` script mode boots the Erlang VM, compiles the
  `.exs` in-memory, then runs it — there is no persistent VM or bytecode
  cache between invocations) has real per-invocation overhead that a
  5-second budget may not comfortably cover under contended/throttled
  container CPU; measure cold vs. warm before picking a value
- [ ] `EVALUATORS` dict gains `"elixir": (eval_elixir.eval_script, ".exs")`
  (note the extension is `.exs`, Elixir's *script* file extension, not
  `.ex`; key confirmed as `"elixir"` via `humaneval-elixir` dataset config)
- [ ] Hand-written passing/failing Elixir smoke tests score correctly
- [ ] Small-`limit` `multiple-elixir` task run completes with a plausible
  `pass@1`
**Dependencies:** none
**Risk:** High — the largest, most multi-step runtime install of the five
(two components that must version-match, non-apt tarball installs, apt
runtime-dependency packages, an env var upstream considers necessary), and
the executor itself needs a real rewrite (not a mechanical import-path
edit) to fit this repo's `safe_subprocess` pattern. BEAM cold-start
overhead per invocation is a genuine unknown until measured in this
repo's container — do not assume upstream's `timeout=5` transfers cleanly.

## Out of Scope
- Items 2-8 of `self.code-eval#2` (FIM/infill API path, HF token wiring,
  PAL-gsm8k error surfacing, Mercury `min()` on empty sequence, `apps`
  metric kwarg mismatch, Perl `multiple-pl` scoring 0, Racket
  `multiple-rkt` crash) — each is its own product decision per the issue,
  not part of this kit.
- The 18 already-working MultiPL-E languages (no changes needed).
- Any change to `code_eval/tasks/multiple.py`'s `LANGUAGES` list or
  `create_all_tasks()` — both already correctly advertise all five
  languages; the gap is purely in `containerized_eval.py`'s `EVALUATORS`
  dict and the missing runtimes/scripts, not the task registry itself.
- Investigating whether `containerized_eval.py`'s `__import__(f"eval_{
  language}")` fallback path works at all — explicit `EVALUATORS` entries
  are required regardless (see R1-R5), so the fallback's correctness is
  moot for this work and not worth spending time on.
- Full-suite MultiPL-E benchmark runs / leaderboard comparisons for these
  five languages — acceptance here is smoke tests + a small-`limit` run
  per language, not a full accuracy validation pass.

## Cross-References
- `self.code-eval#2` (this kit implements item 1 only)
- `self.code-eval#1` (closed — Java/Lua/D/Julia/Swift; establishes the
  precedent this kit follows for cold-start/precompile-warming risk and
  the `/opt`-prefix convention for non-apt runtime installs)
- `self.bigcode-eval-deletion_scheduled-6#1` (superseded; original
  8-language list, D/Julia/Swift portion closed via `#1` above, this kit
  closes the clojure/dart/elixir/haskell/ocaml remainder)
- Upstream: `nuprl/MultiPL-E`, `evaluation/src/eval_{clj,dart,elixir,hs,
  ocaml}.py` and `evaluation/Dockerfile`, pinned at commit
  `3025a531af7450e7df8b96fe0440e9804480bbad`
- `Data/context/treasuremaps/2026-07-05-code-eval-env-gaps.md` (sweep that
  originated this issue)

## Changelog
