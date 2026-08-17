# Point the code + language evals at theseus (Piston is live)

**Date:** 2026-07-24
**Status:** decided; kitted; wiring not yet built. Piston is **live** at
`piston.self-theseus.svc.cluster.local:2000` (per
`selfshipyard/selfai/gitlab-profile#22`). This map turns the placeholder
"second consumer" decision from [[2026-07-20-tools-piston-sandboxing]] into a
grounded, actionable wiring spec now that the endpoint and its real
constraints are known. Kitted 2026-07-24 as three cavekits
(`context/kits/cavekit-piston-execution-client.md`,
`cavekit-code-eval-multiple-seam.md`, `cavekit-code-eval-python-seam.md`;
overview `cavekit-overview-evals-piston.md`) + build site
(`context/plans/build-site-evals-piston.md`), on `gitlab-profile!30`. Build
work order filed as **self.code-eval#5** (the build target repo). Namespace
+ language-priority answers posted back to `gitlab-profile#22`.
**Related:** [[2026-07-20-tools-piston-sandboxing]] (parent decision; this
resolves its code-eval open questions), [[2026-07-05-code-eval-env-gaps]] (the
runtime/executor gap taxonomy that determines which languages Piston can
actually help), [[2026-07-04-selfai-eval-vendoring]] (why the harnesses are
self.code-eval / self.language-eval), [[2026-07-01-eval-sweep-multiple-toolchains]].
GitLab: `selfai/gitlab-profile#22` (Piston-is-live follow-through),
`selfinfra/gitlab-profile#11` (substrate delivered), `selfinfra/gitlab-profile#10`
(phase-2 gVisor+nsjail hardening), `self.crew#128` (persona posture).

## Context

`selfai/gitlab-profile#22` reports that theseus's Piston sandbox is up and
verified end-to-end — the follow-through on the substrate ask
(`selfinfra/gitlab-profile#11`). The 07-20 Piston treasuremap already **decided**
self.code-eval should point at Piston as a "second consumer," but it was
written against a `self-piston:2000` placeholder with open questions about the
real address, throughput, and cold-start timeouts. #22 answers those, so this
map records the live facts and the concrete wiring.

### What "code and language evals" actually maps to (measured, not assumed)

The two vendored harnesses have very different exposure. Confirmed by reading
the current source, not inferred from names:

- **self.code-eval** (`self.code-eval/`, service `self-code-eval:8094`) — **this
  is the real untrusted-execution surface.** Two execution engines, both
  same-host, neither sandboxed beyond "it's in the pod":
  - **MultiPL-E "multiple-\*" (multi-language).** `code_eval/tasks/multiple.py`
    → `custom_metrics/multiple_metrics/containerized_eval.py:45`
    (`eval_string_script`) → per-language `eval_*.py` → `safe_subprocess/__init__.py:41`
    (`subprocess.Popen(..., start_new_session=True)`). Writes each candidate to a
    tempfile and shells out to the local interpreter/compiler. The header of
    `containerized_eval.py` literally says *"Nothing containerized about this any
    more."* This is the **"language evals"** and the **prime Piston target** — its
    per-problem contract (run this program, capture stdout/exit) is exactly
    Piston's one-shot `POST /api/v2/execute`.
  - **Python HumanEval / MBPP / PAL / beyond / studenteval.** In-process
    `exec()`: `custom_metrics/execute.py:76`, `pal_metric/python_executor.py:69`,
    `beyond_eval.py:196+`. This is the **"code evals"** (Python). Model-emitted
    code `exec`'d directly in the service process, guarded only by a
    `reliability_guard()` + signal timeout — the classic OpenAI human-eval
    "unsafe" sandbox, same host.
- **self.language-eval** (`self.language-eval/`, service `self-language-eval:8096`)
  — **executes no model code.** Fork of lm-evaluation-harness (loglikelihood /
  generation NLP tasks). Its only `subprocess`/`os.system` calls are tooling
  (`git describe`, `zstd -d`, the errant GEC scorer) — not untrusted candidate
  code. **Not a Piston target.** If a future derivative language-eval task starts
  executing generations, revisit; today there is nothing to repoint.
- **self.curator** (`self.curator/`) — **no untrusted-code execution stage, and
  no "Node Studio" / "escape-hatch execution" exists in the repo.** All
  `exec`/`subprocess` hits are data tooling (downloads, WER, model wrappers) or
  tests. The issue's `module:curator` label and its "Node Studio's escape-hatch
  execution" line point at a surface that **is not built yet** — flagging rather
  than pretending there's something to wire. When that escape-hatch is designed,
  Piston is the right backend for it, but it's net-new, not a repoint.

**Net:** "point the code and language evals at theseus" == wire **self.code-eval**
(both its engines) at Piston. language-eval has nothing to execute; curator's
execution surface doesn't exist yet.

### The Piston config scaffold today (unconsumed, and scoped to the wrong consumer)

`api/selfai_ui/config.py:782,788` defines `ENABLE_PISTON_EXECUTION` (default
False) + `PISTON_BASE_URL` (default `http://self-piston:2000`), surfaced to app
state (`main.py:638`) and `/api/config` (`main.py:1567`). But:

- **It is unconsumed** — grep finds no HTTP client, no `code_interpreter.py`, no
  executor abstraction reading `PISTON_BASE_URL`. Pure scaffolding.
- **It lives in the wrong service for this work.** These knobs are in the FastAPI
  backend and are scoped to Tools/Functions (`utils/plugin.py:100,144` `exec()`).
  self.code-eval is a **separate service** with its own config
  (`ENABLE_CODE_EVAL_API` / `CODE_EVAL_BASE_URLS`, `config.py:763`). The eval
  wiring must add a **parallel** `ENABLE_PISTON_EXECUTION` / `PISTON_BASE_URL`
  pair **on the code-eval service**, not reuse the backend's.
- **Duct tape, named:** the config.py comment points at
  `context/treasuremaps/2026-07-20-tools-piston-sandboxing.md` — a path that does
  not exist inside the self.ai backend repo (the treasuremap lives in *this*
  gitlab-profile repo). Harmless, but a dangling cross-repo doc reference worth
  fixing when that config block is next touched.

## Live facts from theseus (#22) that constrain the wiring

- **Endpoint:** `piston.self-theseus.svc.cluster.local:2000` (in-cluster
  ClusterIP). API: `POST /api/v2/execute`, `GET /api/v2/runtimes`. Replaces the
  `self-piston:2000` placeholder — set `PISTON_BASE_URL` to this on the code-eval
  service.
- **NetworkPolicy is namespace-scoped to `self-ai`.** Confirmed both
  `self-code-eval` and `self-language-eval` run in `self-ai`
  (`manifests/eval/code-eval.yaml:9`, `language-eval.yaml:9`) — so theseus's
  ingress **already permits them**; no `namespaceSelector` widening needed.
- **Isolation is `isolate`-level** (cgroups + namespaces, non-root uid) — **not
  yet gVisor+nsjail** (that's phase-2, `selfinfra/gitlab-profile#10`). Threat
  model today: "contains a buggy/careless script," not "contains an adversarial
  escape." For benchmark code this is a real upgrade over today's in-pod
  `exec()`, and model-emitted benchmark answers aren't an active adversary — but
  public benchmarks are training-data-poisoning targets, so isolate-level is the
  floor, not the ceiling. Take it now; track phase-2.
- **`run_timeout` is hard-capped at 3000 ms** and not raisable per-request — this
  is Piston's own built-in default, **not** something theseus set
  (`PISTON_RUN_TIMEOUT` is unset in their cloud-init). This collides directly
  with code-eval's known cold-start problem (the julia `using Test` precompile
  blew the per-problem timeout — see [[2026-07-05-code-eval-env-gaps]] and
  [[2026-07-20-tools-piston-sandboxing]]). dart is already unusable for this
  reason (~3.1 s cold start). theseus flagged raising it (e.g.
  `PISTON_RUN_TIMEOUT=8000`) as cheap and low-risk (doesn't weaken sandboxing) —
  **we need to ask for it deliberately** if we want the slower toolchains.
- **Invocable language name ≠ package name.** `gcc` → `c`/`c++`; `dotnet` → the
  `csharp`/`fsharp`/`basic` aliases; check `GET /api/v2/runtimes` for the real
  invocable name, don't map from `packages.txt`. The code-eval Piston adapter
  must resolve `eval_*.py`'s language to Piston's invocable name explicitly.
- **Resource sizing matters and is per-request.** Piston's execute call takes
  `resources: {cpu, ram, appDisk}`. Compiled languages (C#, Rust, Java) need
  meaningfully more than interpreted ones — tiny requests SIGKILL (exit 137)
  during the build step. The harness should size per-language, budgeting more for
  compiled toolchains.
- **Cold-start is a real, transient first-invocation artifact.** First-ever call
  to a runtime can time out, then run clean on retry. The harness should retry
  once on a cold-start timeout rather than scoring a single untried attempt 0.
- **Language coverage as of #22:** 18 of code-eval's 24 needed languages usable;
  rust and csharp were driven to production-ready during the issue (rust:
  `patchelf --clear-execstack`; csharp: fresh VM disk + bundled OpenSSL-1.1).
  Still broken: `fsharp.net` (NuGet sig + compile-script rename), `haskell`
  (`cannot find 'ld'`), `julia`/`rust`-class execstack (julia still open),
  `dart` (timeout ceiling), `rscript` (`libreadline.so.7`). Adding/patching a
  language = MR a `language,version` line to `self.theseus/piston-repo`'s
  `packages.txt`, or just ask on the issue (few-minute turnaround).

## Decision

**Wire self.code-eval at Piston as an opt-in execution backend**, mirroring the
seam the 07-20 map established, now with the live endpoint and constraints:

1. **Config (code-eval service, parallel to the backend's).** Add
   `ENABLE_PISTON_EXECUTION` (default False — in-container execution unchanged
   when unset) + `PISTON_BASE_URL`
   (`http://piston.self-theseus.svc.cluster.local:2000`) to the code-eval
   service and its manifest (`manifests/eval/code-eval.yaml`).
2. **MultiPL-E first (highest value, cleanest fit).** Add a Piston branch at
   `containerized_eval.py:eval_string_script` (L45): when Piston is enabled,
   POST `{language, source, stdin, resources, run_timeout}` to
   `/api/v2/execute` instead of the local `safe_subprocess` path; map the
   harness language to Piston's invocable name via `/api/v2/runtimes`. Keep the
   local path as the fallback when Piston is off. This closes the biggest
   exposure (per-language `subprocess` on the pod) with the smallest change.
3. **Python engines second.** Route `execute.py`/`check_correctness` (HumanEval/
   MBPP) and the PAL/beyond/studenteval `exec()` paths through the same Piston
   client when enabled. Larger change (they `exec()` in-process today), lower
   marginal isolation win than MultiPL-E per problem, so sequence it after.
4. **Reproducibility guardrails.** Piston packages are versioned — pin runtime
   versions per language and record them in results metadata so Piston-backed
   and container-backed scores are distinguishable. Per-language timeout +
   resource config, not one global number.
5. **Don't over-claim coverage.** Repointing at Piston only helps languages that
   **already have an `eval_*.py` executor** in the harness. Per
   [[2026-07-05-code-eval-env-gaps]] group C, code-eval has **no executor** for
   `clojure`/`dart`/`elixir`/`haskell`/`ocaml` — Piston *having* those runtimes
   does not make them scorable; they still need an executor port from newer
   MultiPL-E. The real unlock set is: languages with an executor **and** a
   working Piston runtime **and** (if slow) a raised `run_timeout`.

**language-eval: no work.** Nothing executes model code; leave it in-container.
**curator: no work yet.** The Node-Studio escape-hatch is unbuilt; when it's
designed, spec it as a net-new Piston consumer, not a repoint.

## What self.ai owes theseus back (the #22 asks)

1. **Namespace — answered: `self-ai`.** Both eval services run there; the
   existing NetworkPolicy covers them. No widening needed. (Post this on #22.)
2. **Which languages we actually need.** Drive this off the executor∩runtime
   intersection, not a wishlist. From the env-gaps taxonomy, the languages that
   have executors and matter for real MultiPL-E runs are the priority; the
   executor-less five (clj/dart/elixir/hs/ml) are **not** worth theseus's
   package effort until code-eval has executors for them. Tell theseus the
   with-executor set first.
3. **`run_timeout` raise.** Ask for `PISTON_RUN_TIMEOUT=8000` (or a measured
   value) **if** we adopt cold-start-heavy compiled/JIT languages through the
   harness — theseus called it cheap and low-risk. Not needed for the fast
   interpreted set.

## Alternatives rejected

- **Reuse the backend's `ENABLE_PISTON_EXECUTION`/`PISTON_BASE_URL` for the eval
  wiring.** Rejected — those are on the FastAPI backend and scoped to
  Tools/Functions; self.code-eval is a separate service and needs its own
  parallel knobs (same names, different service), consistent with the
  `CODE_EVAL_*` seam it already uses.
- **Make Piston the default execution path for code-eval.** Rejected for now —
  an eval run is hundreds of executions and Piston queues per runtime;
  throughput must be measured on theseus before defaulting anything. Opt-in
  (default off), in-container unchanged when unset.
- **Install clj/dart/elixir/haskell/ocaml runtimes in Piston to "fix" those
  MultiPL-E tasks.** Rejected as premature — no `eval_*.py` executor exists for
  them, so the runtime is dead weight until the executors are ported. Sequence
  the executor port first, then the Piston package.
- **Wait for gVisor+nsjail (phase-2) before adopting Piston for evals.**
  Rejected — isolate-level is already a real upgrade over in-pod `exec()`, and
  benchmark code isn't an active adversary. Adopt now, track phase-2
  (`selfinfra/gitlab-profile#10`) for the poisoned-benchmark threat.
- **Repoint language-eval / curator too, for symmetry.** Rejected — neither
  executes untrusted code today; there is literally nothing to sandbox.

## Open questions

- **Throughput.** Batch execution rate on theseus for a full MultiPL-E run
  (hundreds of executes, queued per runtime) is unmeasured — gate any
  "Piston-by-default" decision on it.
- **Adapter for the Python `exec()` engines.** HumanEval/MBPP/PAL run
  candidate + test string in one process with a `reliability_guard`; mapping
  that to Piston's one-shot script-in/stdout-out contract (esp. tests that
  import the candidate as a module) needs its own small spike — the MultiPL-E
  tempfile-per-program shape is a cleaner first cut.
- **fsharp.net / haskell / julia / dart / rscript** — which, if any, are on
  code-eval's real critical path (and have executors)? Answer before asking
  theseus to chase Piston-side package fixes.
- **Where the code-eval Piston client lives** — inside the vendored harness
  (`containerized_eval.py`) vs. a thin shared executor module — is an
  implementation call for the self.code-eval repo, not decided here.
