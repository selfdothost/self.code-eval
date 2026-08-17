---
created: "2026-07-24"
last_edited: "2026-07-24"
---

# Cavekit: Overview — self.code-eval Piston wiring

## Project

**self.code-eval Piston wiring** — wire self.code-eval at theseus's live Piston
sandbox (`piston.self-theseus.svc.cluster.local:2000`) as an **opt-in** execution
backend (default off, in-container unchanged when unset). One reusable execution
client, plus two harness seams that route through it: the multi-language
MultiPL-E path and the Python in-process `exec()` path. Build target is the
**self.code-eval** submodule — a separate repo from this Data
(`gitlab-profile`) repo, which holds kits only.

Decision grounding:
`context/treasuremaps/2026-07-24-evals-piston-theseus.md`. Endpoint + live
constraints: `selfai/gitlab-profile#22`; substrate: `selfinfra/gitlab-profile#11`.

> **Scoping note.** This `context/kits/` directory is **shared** with a separate,
> unrelated project — **self.ai alpha-publish** (indexed by `cavekit-overview.md`,
> covering `cavekit-publish-*`, `cavekit-deploy-*`, `cavekit-repo-topology`,
> `cavekit-public-readiness`, `cavekit-pre-mirror-scrub`). Those files are **not**
> part of this project. A `/ck:map` for **this** project must be filtered to the
> three files below:
> - `cavekit-piston-execution-client.md`
> - `cavekit-code-eval-multiple-seam.md`
> - `cavekit-code-eval-python-seam.md`

## Domain Index

| Kit | Status | Reqs | ACs | Description |
|-----|--------|------|-----|-------------|
| [cavekit-piston-execution-client.md](cavekit-piston-execution-client.md) | DRAFT | 6 | 20 | Reusable language-agnostic Piston client: opt-in config, execute contract, invocable-name resolution, per-language resources, run_timeout + single cold-start retry, reproducibility metadata. Build first. |
| [cavekit-code-eval-multiple-seam.md](cavekit-code-eval-multiple-seam.md) | DRAFT | 4 | 11 | Route MultiPL-E `eval_string_script` through the client; scoring parity; 19-executor coverage gate; timeout/resource pass-through. |
| [cavekit-code-eval-python-seam.md](cavekit-code-eval-python-seam.md) | DRAFT | 3 | 9 | Route the Python `exec()` engines (HumanEval/MBPP/PAL/beyond) through the client; candidate+test assembly; timeout parity. Sequenced last. |

**Coverage summary:** 3 domain kits · 13 requirements · 40 acceptance criteria.

## Cross-Reference Map

- **piston-execution-client** → code-eval-multiple-seam, code-eval-python-seam
- **code-eval-multiple-seam** → piston-execution-client, code-eval-python-seam
- **code-eval-python-seam** → piston-execution-client, code-eval-multiple-seam

## Dependency Graph

```
                    ┌──> code-eval-multiple-seam  (build second — highest value)
piston-execution-   │
   client ──────────┤
 (build first)      │
                    └──> code-eval-python-seam    (build last — harder shape)
```

- **piston-execution-client** is the shared adapter and has no dependency on the
  two seams; build it first.
- **code-eval-multiple-seam** and **code-eval-python-seam** both depend only on
  the client and are **independent of each other** — they could proceed in
  parallel, but the treasuremap sequences MultiPL-E first (cleaner fit, highest
  isolation win per problem) and the Python `exec()` seam last (candidate+test
  assembly is a design risk needing a spike).
- `[needs-live-endpoint]` acceptance criteria require the theseus Piston endpoint
  to be reachable from `self-ai`; `[needs-human-review]` flags the python-seam
  assembly risk.

## Changelog

- 2026-07-24 — DRAFT created.
