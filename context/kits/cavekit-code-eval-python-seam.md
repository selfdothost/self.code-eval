---
created: "2026-07-24"
last_edited: "2026-07-24"
---

# Cavekit: Code-Eval Python exec() Seam

## Scope

Route self.code-eval's Python in-process `exec()` engines through the Piston
execution client when enabled, keeping the in-process path as the fallback when
off. Seams:
`self.code-eval/code_eval/tasks/custom_metrics/execute.py:76`
(HumanEval/MBPP `check_correctness`, exec of candidate+test in a
`multiprocessing.Process`), `pal_metric/python_executor.py:69`, and
`beyond_eval.py:196+`. This is the harder shape — tests import/reference the
candidate, so a one-shot Piston script must reconstruct candidate+test into a
single self-contained runnable program. Sequence this **after** the MultiPL-E
seam.

Decision grounding:
`context/treasuremaps/2026-07-24-evals-piston-theseus.md` (Decision §3; Open
Questions — the exec()-engine adapter spike).

## Requirements

### R1: Opt-in routing for the Python exec() engines
**Description:** When Piston is enabled, the HumanEval/MBPP candidate+test (and
the PAL/beyond paths sharing this shape) execute in Piston instead of in-process
`exec()`; when off, the existing `reliability_guard()` + `multiprocessing.Process`
+ signal-timeout path runs unchanged.
**Acceptance Criteria:**
- [ ] With `ENABLE_PISTON_EXECUTION` off, `check_correctness` behavior is
      unchanged from today's in-process path (same pass/fail for the same input).
- [ ] With the flag on, the candidate+test executes via the Piston client
      (assertable by observing the execute call / absence of the in-process
      `exec()`).
- [ ] The routing decision reads the code-eval service's own
      `ENABLE_PISTON_EXECUTION` (per client kit R1).
**Dependencies:** cavekit-piston-execution-client.md R1, R2.

### R2: Candidate+test assembly
**Description:** The candidate program and its test string are combined into a
single self-contained script whose exit code / stdout signals pass or fail,
preserving today's pass/fail semantics — including the case where the test
imports or references the candidate as a module.
**Acceptance Criteria:**
- [ ] Assembly produces one self-contained program (no reliance on a separate
      importable candidate module file on the Piston side).
- [ ] The assembled program's exit code and/or stdout deterministically encode
      pass vs. fail, mapped back to the harness's existing pass/fail result.
- [ ] `[needs-live-endpoint]` A known HumanEval problem scores the same pass/fail
      via the Piston path as via the in-process `exec()` path.
**Dependencies:** R1; cavekit-piston-execution-client.md R2.

### R3: Timeout parity
**Description:** The per-problem signal timeout maps onto the client's
`run_timeout` with the same effective ceiling semantics, so a non-terminating
candidate is reported as a timeout/fail rather than hanging the run.
**Acceptance Criteria:**
- [ ] The per-problem timeout is passed through to the client's `run_timeout`.
- [ ] `[needs-live-endpoint]` A candidate that infinite-loops is reported as a
      timeout/fail via the Piston path, not a hang.
- [ ] A cold-start timeout on first invocation is retried once via the client
      (client kit R5), not scored 0 on the first attempt.
**Dependencies:** R1; cavekit-piston-execution-client.md R5.

## Out of Scope

- **Module-import / test-shape complexity is an explicit design risk.**
  `[needs-human-review]` HumanEval/MBPP/PAL/beyond tests that import the candidate
  as a module, rely on in-process fixtures, or depend on `reliability_guard`'s
  interpreter-level neutering may not translate cleanly into a single one-shot
  Piston script. The candidate+test assembly (R2) needs a small spike and human
  review before this seam is considered done; the MultiPL-E tempfile-per-program
  shape is the cleaner first cut and is sequenced first.
- The MultiPL-E "multiple-*" seam — see multiple-seam kit.
- The client's transport, resolution, sizing, retry, and metadata internals — see
  client kit.
- studenteval-specific harness quirks beyond the shared exec() shape, unless they
  fall out of R2 for free.

## Cross-References

- See also: cavekit-piston-execution-client.md (the client this seam calls)
- See also: cavekit-code-eval-multiple-seam.md (the sibling MultiPL-E seam,
  sequenced first)

## Changelog

- 2026-07-24 — DRAFT created.
