---
created: "2026-07-18"
last_edited: "2026-07-18"
---

# Cavekit Overview

## Project

**self.code-eval** — the code-generation eval harness for self.ai, a hard fork
of BigCode's `bigcode-evaluation-harness` (`code_eval/`). Runs benchmark
suites (HumanEval, MultiPL-E, DS-1000, APPS, Mercury, etc.) against
generations from a remote OpenAI-compatible endpoint (self.llamolotl) or a
local model, scores them in a sandboxed subprocess-per-language executor, and
reports pass@k. Ships as a single Docker image (`Dockerfile`) with all
language runtimes baked in — no per-language sidecar containers.

This is the first `context/kits/` directory in this repo. No existing kit
convention was found here (`context/` did not exist prior to this kit); the
format below matches the house convention established in
`selfai/self.ai`'s `context/kits/` (front-matter, Domain Index, R-numbered
requirements with checklist acceptance criteria).

## Domain Index

| Domain | Cavekit File | Requirements | Status | Description |
|--------|-----------|-------------|--------|-------------|
| MultiPL-E Missing Executors | cavekit-multiple-e-executors.md | 5 | DRAFT | Port upstream MultiPL-E's `eval_clj.py`/`eval_dart.py`/`eval_elixir.py`/`eval_hs.py`/`eval_ocaml.py` into `code_eval/tasks/custom_metrics/multiple_metrics/`, add the five missing language runtimes to the Dockerfile, and wire each into the `EVALUATORS` dispatch table so `multiple-clj`/`multiple-dart`/`multiple-elixir`/`multiple-hs`/`multiple-ml` score instead of erroring. |

**Coverage summary:** 1 domain kit · 5 requirements.

## Source

Scoped from GitLab issue `selfshipyard/selfai/self.code-eval#2` ("[from
sweep] Remaining eval-coverage gaps post 2026-07-05 sweep"), item 1 only.
Items 2-8 of that issue (FIM/infill API path, HF token wiring, PAL-gsm8k
error surfacing, Mercury `min()` on empty sequence, `apps` metric kwarg
mismatch, Perl `multiple-pl` scoring 0, Racket `multiple-rkt` crash) are
out of scope for this kit — each is a separate product decision per the
issue, not mechanical porting work, and should get its own kit when picked
up.

## Gaps / Open Questions

- Items 2-8 of `self.code-eval#2` are not yet kitted. Whoever picks up the
  next item from that issue should add a sibling domain kit here and extend
  the Domain Index table above, rather than starting a parallel convention.
- The `EVALUATORS` dict fallback path in `containerized_eval.py`
  (`__import__(f"eval_{language}")` for languages not in the dict) is
  untested and likely broken for a package-relative module — see
  `cavekit-multiple-e-executors.md` R1-R5 registry acceptance criteria,
  which require explicit dict entries rather than relying on the fallback.
