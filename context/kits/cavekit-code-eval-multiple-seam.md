---
created: "2026-07-24"
last_edited: "2026-07-24"
---

# Cavekit: Code-Eval MultiPL-E Seam

## Scope

Route self.code-eval's multi-language MultiPL-E ("multiple-*") execution through
the Piston execution client when enabled, keeping the local subprocess path as
the fallback when off. The seam is
`self.code-eval/code_eval/tasks/custom_metrics/multiple_metrics/containerized_eval.py:45`
(`eval_string_script`), which today dispatches to per-language `eval_*.py`
executors and ultimately `safe_subprocess/__init__.py:41` (`subprocess.Popen`).
This is the highest-value, cleanest-fit seam — its per-problem contract (run this
program, capture stdout/exit) maps directly onto Piston's one-shot execute.

Decision grounding:
`context/treasuremaps/2026-07-24-evals-piston-theseus.md` (Decision §2, §5).

## Requirements

### R1: Opt-in routing at the seam
**Description:** When Piston is enabled, `eval_string_script` runs the candidate
program via the Piston execution client instead of the local `safe_subprocess`
path; when Piston is off, the local per-language subprocess path runs unchanged.
**Acceptance Criteria:**
- [ ] With `ENABLE_PISTON_EXECUTION` off, `eval_string_script` behavior is
      byte-identical to today's local subprocess path (same scoring output for
      the same generations).
- [ ] With the flag on, execution for a supported language routes through the
      Piston client (assertable by observing the HTTP execute call / absence of
      the local `Popen`).
- [ ] The routing decision reads the code-eval service's own
      `ENABLE_PISTON_EXECUTION` (per client kit R1).
**Dependencies:** cavekit-piston-execution-client.md R1, R2.

### R2: Scoring parity
**Description:** A Piston-backed run of a given MultiPL-E language produces the
same pass/fail scoring as the local path for the same generations, on languages
where both the local toolchain and the Piston runtime work.
**Acceptance Criteria:**
- [ ] `[needs-live-endpoint]` For Python, a fixed set of generations scores
      pass/fail identically via the Piston path and the local path.
- [ ] `[needs-live-endpoint]` For at least one compiled language (e.g. C#, Rust,
      or Java) with a working Piston runtime, the same fixed generations score
      pass/fail identically via both paths.
**Dependencies:** R1; cavekit-piston-execution-client.md R2, R3, R4.

### R3: Coverage honesty — executor gate
**Description:** Only the 19 languages that have an `eval_*.py` executor present
(cpp, cs, d, go, java, javascript, julia, lua, perl, php, python, r, racket,
ruby, rust, scala, sh, swift, ts) are in scope for this seam. The 5
registered-but-executor-less languages (clojure, dart, elixir, haskell, ocaml)
are **not** scorable via this seam regardless of Piston runtime availability —
they need executor ports first (out of scope here). Attempting a Piston run of an
executor-less language fails loudly, not silently as a 0.
**Acceptance Criteria:**
- [ ] An enumerated list of the 19 in-scope executor-backed languages exists in
      the kit-derived work. The Architect validates the exact set against the live
      `eval_*.py` executor files in the harness source before hard-coding it (the
      grounding treasuremap asserts the count and the executor-less five, not the
      positive 19 tokens verbatim).
- [ ] Attempting to route an executor-less language (clojure/dart/elixir/haskell/
      ocaml) through this seam raises a clear, named error.
- [ ] That error is distinguishable from a genuine test failure — it does not
      record a 0 score as if the candidate had been executed and failed.
**Dependencies:** R1.

### R4: Per-language timeout and resource pass-through
**Description:** MultiPL-E's per-problem timeout maps to the client's
`run_timeout`, and per-language resource sizing (client kit R4) is applied
end-to-end, so compiled languages get their larger budget.
**Acceptance Criteria:**
- [ ] The per-problem timeout configured in the harness is passed through to the
      client's `run_timeout` for that execution.
- [ ] `[needs-live-endpoint]` A compiled-language MultiPL-E execution carries the
      larger compiled resource budget end-to-end (observable in the execute
      request `resources`).
- [ ] A cold-start timeout on a first invocation is retried once via the client
      (client kit R5), not scored 0 on the first attempt.
**Dependencies:** R1; cavekit-piston-execution-client.md R4, R5.

## Out of Scope

- Porting executors for clojure/dart/elixir/haskell/ocaml (executor gate, R3) —
  net-new harness work, not a repoint.
- The Python in-process `exec()` engines (HumanEval/MBPP/PAL/beyond/studenteval)
  — see python-seam kit.
- The client's transport, resolution, sizing, retry, and metadata internals — see
  client kit.
- Requesting theseus install/patch Piston runtimes for languages that lack
  executors.

## Cross-References

- See also: cavekit-piston-execution-client.md (the client this seam calls)
- See also: cavekit-code-eval-python-seam.md (the sibling Python exec() seam)

## Changelog

- 2026-07-24 — DRAFT created.
