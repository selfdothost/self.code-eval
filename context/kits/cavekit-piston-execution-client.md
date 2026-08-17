---
created: "2026-07-24"
last_edited: "2026-07-24"
---

# Cavekit: Piston Execution Client

## Scope

A language-agnostic execution client for self.code-eval that runs one program in
theseus's live Piston sandbox and returns stdout/stderr/exit/signal, plus the
config knobs, language-name resolution, resource sizing, timeout/retry, and
reproducibility metadata that back it. This is the reusable adapter both
execution seams call; build it first. It does **not** decide where the two
harness seams branch — that is the multiple-seam and python-seam kits.

Decision grounding:
`context/treasuremaps/2026-07-24-evals-piston-theseus.md` (Decision §1, §4; Live
facts). Endpoint and constraints from `selfai/gitlab-profile#22`.

## Requirements

### R1: Opt-in config, default off, in-container unchanged when unset
**Description:** The self.code-eval service gains its own
`ENABLE_PISTON_EXECUTION` (default False) and `PISTON_BASE_URL` (default the live
endpoint `http://piston.self-theseus.svc.cluster.local:2000`), parallel to — not
reusing — the FastAPI backend's Tools-scoped pair. The matching env is declared
in the code-eval Flux manifest (`manifests/eval/code-eval.yaml` in the self.ai
backend repo). When the flag is unset/off, execution behavior is byte-identical
to today's in-container path.
**Acceptance Criteria:**
- [ ] With `ENABLE_PISTON_EXECUTION` off, no HTTP request is issued to
      `PISTON_BASE_URL` during an eval run (assertable by mocking/observing the
      HTTP layer).
- [ ] With the flag on and `PISTON_BASE_URL` pointed at an unreachable/bad URL, a
      run surfaces a clear connection/error signal — never a silent 0-score that
      is indistinguishable from a genuine test failure.
- [ ] `ENABLE_PISTON_EXECUTION` and `PISTON_BASE_URL` are read from the
      code-eval service's own config surface, not from the backend's
      `config.py:782,788` pair.
- [ ] The code-eval Flux manifest declares both env vars (default off).
**Dependencies:** none.

### R2: Execute contract
**Description:** The client executes one program by POSTing to Piston v2
`/api/v2/execute` with program source, stdin, resolved language + version,
per-language resources, and run_timeout, and parses stdout, stderr, exit code,
and signal back out of the response into a result the harness seams can score on.
**Acceptance Criteria:**
- [ ] A request carries, at minimum: source, stdin, language, version,
      `resources`, and `run_timeout` fields.
- [ ] The parsed result exposes stdout, stderr, exit code, and signal as distinct
      fields.
- [ ] `[needs-live-endpoint]` A known-good Python program that prints `42`
      returns stdout `42` and exit 0 through the client end-to-end against the
      live theseus endpoint.
**Dependencies:** R3 (language resolution), R4 (resources), R5 (run_timeout).

### R3: Language-name resolution to invocable runtime
**Description:** The client resolves the harness's language token to Piston's
**invocable** runtime name and version via `GET /api/v2/runtimes` — the invocable
name is not the package name (`gcc` → `c`/`c++`; `dotnet` →
`csharp`/`fsharp`/`basic`). An unknown or unresolved language raises a clear
error rather than silently firing the wrong runtime.
**Acceptance Criteria:**
- [ ] A resolution table/lookup maps each supported harness language token to a
      Piston invocable name + version.
- [ ] The mapping is derived from / validated against `GET /api/v2/runtimes`
      output, not from a static `packages.txt`.
- [ ] The `gcc`→c/c++ and `dotnet`→csharp/fsharp/basic aliasing cases resolve to
      the invocable name.
- [ ] An unresolved language token raises a clear, named error; no request is
      sent with a wrong or guessed runtime.
**Dependencies:** none.

### R4: Per-language resource sizing
**Description:** Each execute request carries `resources: {cpu, ram, appDisk}`,
sized per language. Compiled languages (C#, Rust, Java) get a meaningfully larger
budget than interpreted ones, because undersized requests SIGKILL (exit 137)
during the build step.
**Acceptance Criteria:**
- [ ] Resource sizing is configurable per language (not one global constant).
- [ ] The default budget for a compiled language (e.g. C#, Rust, Java) is
      strictly larger than the default interpreted budget on at least one of
      cpu/ram/appDisk.
- [ ] An exit-137 / SIGKILL result is distinguishable in the parsed result from a
      normal non-zero exit (so undersizing is diagnosable, not conflated with a
      test failure).
**Dependencies:** R2.

### R5: run_timeout handling with single cold-start retry
**Description:** Requests carry a per-language `run_timeout` bounded by Piston's
hard 3000 ms default cap (only raisable if theseus sets `PISTON_RUN_TIMEOUT`
globally). On a first-invocation cold-start transient timeout, the client retries
**exactly once** before scoring; a genuine timeout that persists after the retry
is reported as a timeout, not masked or retried indefinitely.
**Acceptance Criteria:**
- [ ] `run_timeout` is configurable per language, and requests do not assume a
      per-request raise above Piston's global cap.
- [ ] A first-attempt cold-start timeout triggers exactly one retry (at most one
      retry, assertable by counting execute calls).
- [ ] A program that still times out after the retry is reported as a timeout
      result, distinct from a normal failure and distinct from a successful run.
- [ ] `[needs-live-endpoint]` A cold first invocation of a runtime that succeeds
      on the second attempt yields a scored result, not a 0 on the untried first
      attempt.
**Dependencies:** R2.

### R6: Reproducibility metadata
**Description:** The resolved Piston runtime version used for each execution is
recorded in the eval results metadata, so Piston-backed runs are distinguishable
from container-backed runs and are reproducible against a known package version.
**Acceptance Criteria:**
- [ ] Results include, per language executed, the Piston runtime version used.
- [ ] Results carry a marker distinguishing a Piston-backed execution from an
      in-container execution.
**Dependencies:** R3.

## Out of Scope

- Where each harness seam branches into this client — see multiple-seam and
  python-seam kits.
- Adding or patching Piston-side language packages on theseus (that is a
  `self.theseus/piston-repo` `packages.txt` MR / issue ask, not harness work).
- Raising theseus's global `PISTON_RUN_TIMEOUT` — an infra ask on
  `selfai/gitlab-profile#22`, not code in this client.
- Throughput / batch-rate measurement and any "Piston by default" decision —
  explicitly deferred (treasuremap Open Questions).
- gVisor+nsjail phase-2 hardening (`selfinfra/gitlab-profile#10`).

## Cross-References

- See also: cavekit-code-eval-multiple-seam.md (consumes this client at the
  MultiPL-E seam)
- See also: cavekit-code-eval-python-seam.md (consumes this client at the Python
  exec() seam)

## Changelog

- 2026-07-24 — DRAFT created.
