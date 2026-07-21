# self.code-eval

**The self.ai stack's catch-all harness for code-focused LLM evaluation.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

---

self.code-eval is a fully vendored, GPLv3-relicensed fork of the
[BigCode Evaluation Harness](https://github.com/bigcode-project/bigcode-evaluation-harness)
(Apache-2.0), previously carried as `self.bigcode-eval`; the Python package is
`code_eval`. **Not affiliated with or endorsed by the BigCode project.** See
[`NOTICE`](NOTICE) for full provenance (fork-point commit, relicensing
rationale) and [`LICENSE`](LICENSE) (GPLv3) — the original Apache-2.0 text is
retained verbatim in `LICENSE.bigcode-apache-2.0`.

Like `self.chat`/`self.llamolotl`/`self.curator`, this is a hard vendor, not a
tracked upstream: no code flows back to BigCode, and upstream changes are
pulled in deliberately, not automatically.

## How self.ai uses this

self.code-eval ships two layers:

- **The evaluation harness** (`main.py` + `code_eval/`) — the vendored
  BigCode framework itself: task definitions, generation, execution, scoring.
  See [Features](#features) below for the supported benchmark list.
- **A FastAPI control plane** (`api/main.py`, port `8094`) — self.ai-specific,
  not part of upstream BigCode. Wraps the harness as a job-queue service:
  submit a benchmark run, stream its logs/live progress, list/inspect/purge
  past results. This is what self.ai's own API server talks to; it is the
  real integration point in production, not the raw CLI usage below.

  | Endpoint | Purpose |
  |---|---|
  | `GET /api/tasks`, `GET /api/tasks/categories` | List available benchmark tasks |
  | `POST /api/jobs` | Submit an evaluation job |
  | `GET /api/jobs`, `GET /api/jobs/{id}` | List / inspect jobs |
  | `GET /api/jobs/{id}/logs`, `GET /api/jobs/{id}/live` | Job logs, live streaming status |
  | `DELETE /api/jobs/{id}`, `DELETE /api/jobs/{id}/purge` | Cancel / purge a job |
  | `GET /api/results`, `GET /api/results/{id}` | List / inspect results |
  | `GET /api/results/{id}/details`, `GET /api/results/{id}/generations` | Result detail, raw generations |
  | `GET /health` | Liveness |

## Build & deploy

Built and published via GitLab CI (kaniko, yard-native — no local Docker
workflow) from the single `Dockerfile` at repo root. That image bundles every
MultiPL-E language runtime directly — all 23 registered `multiple-*` tasks
are runnable, from Node/TypeScript through Clojure, Dart, Elixir, Haskell,
and OCaml — so it covers everything the standard benchmarks and MultiPL-E
both need in one build.

`Dockerfile-multiple` (a thin wrapper that pulled
`ghcr.io/nuprl/multipl-e-evaluation` as its base) predated that consolidation
and was never built by CI — removed.

Gated datasets on the Hub (e.g. `studenteval`) need an HF token to download.
The image doesn't bake one in — set `HF_TOKEN` (or the legacy
`HUGGING_FACE_HUB_TOKEN`) as a runtime env var at deploy time via the yard's
secrets mechanism (ESO/OpenBao), not in the `Dockerfile`. Ungated benchmarks
need no token and are unaffected either way.

## Features

This is a framework for the evaluation of code generation models, inspired by
[EleutherAI/lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness).
Below are the features and tasks of the vendored framework:

- Features:
    - Any autoregressive model available on [Hugging Face hub](https://huggingface.co/) can be used, but code-specific models such as [SantaCoder](https://huggingface.co/bigcode/santacoder), [InCoder](https://huggingface.co/facebook/incoder-6B) and [CodeGen](https://huggingface.co/Salesforce/codegen-16B-mono) are recommended.
    - Multi-GPU text generation via `accelerate`; Dockerized execution for security and reproducibility.

- Tasks:
    - 7 code generation **Python** tasks (with unit tests): [HumanEval](https://huggingface.co/datasets/openai_humaneval), [HumanEval+](https://huggingface.co/datasets/evalplus/humanevalplus), [InstructHumanEval](https://huggingface.co/datasets/codeparrot/instructhumaneval), [APPS](https://huggingface.co/datasets/codeparrot/apps), [MBPP](https://huggingface.co/datasets/mbpp), [MBPP+](https://huggingface.co/datasets/evalplus/mbppplus), and [DS-1000](https://github.com/HKUNLP/DS-1000/) for both completion (left-to-right) and insertion (FIM) mode.
    - [HumanEvalPack](https://huggingface.co/datasets/bigcode/humanevalpack) extends HumanEval to **3** scenarios across **6** languages via human translations, released with [OctoPack](https://arxiv.org/abs/2308.07124).
    - [MultiPL-E](https://github.com/nuprl/MultiPL-E) evaluation suite (HumanEval translated into **18** programming languages).
    - [Recode](https://github.com/amazon-science/recode/tree/main) applied to HumanEval, evaluating code-generation robustness.
    - [Pal](https://github.com/reasoning-machines/pal) Program-aided Language Models evaluation for grade school math: [GSM8K](https://huggingface.co/datasets/gsm8k) and [GSM-HARD](https://huggingface.co/datasets/reasoning-machines/gsm-hard).
    - Code-to-text from [CodeXGLUE](https://huggingface.co/datasets/code_x_glue_ct_code_to_text) (zero-shot & fine-tuning) for **Python, Go, Ruby, Java, JavaScript, PHP**. Documentation translation from [CodeXGLUE](https://huggingface.co/datasets/code_x_glue_tt_text_to_text).
    - [CoNaLa](https://huggingface.co/datasets/neulab/conala) for **Python** code generation (2-shot, BLEU).
    - [Concode](https://huggingface.co/datasets/code_x_glue_tc_text_to_code) for **Java** code generation (2-shot, BLEU).
    - 3 multilingual classification tasks: [Java complexity prediction](https://huggingface.co/datasets/codeparrot/codecomplex), [Java code equivalence](https://huggingface.co/datasets/code_x_glue_cc_clone_detection_big_clone_bench), [C code defect prediction](https://huggingface.co/datasets/code_x_glue_cc_defect_detection).
    - [SantaCoder-FIM](https://huggingface.co/datasets/bigcode/santacoder-fim-task) for FIM evaluation on **Python** using Exact Match ([SantaCoder paper](https://arxiv.org/abs/2301.03988)): `StarCoderFIM` (default FIM tokens) and `SantaCoderFIM` (SantaCoder FIM tokens).
    - [Mercury](https://huggingface.co/datasets/Elfsong/Mercury) for evaluating computational efficiency of **Python** code generation.

More detail on each task: [`docs/README.md`](docs/README.md).

## Setup

This repo is the harness — no separate clone needed. From a checkout:

```bash
pip install -e .
```

To run the `DS-1000` benchmark, additional constraints must be resolved:
```
# python version must be 3.7.10
pip install -e ".[ds1000]" # installs all additional dependencies except PyTorch
# torch==1.12.1 required. Download version with relevant GPU support etc., e.g.,
pip install torch==1.12.1+cu116 --extra-index-url https://download.pytorch.org/whl/cu116

# to suppress any tensorflow optimization warnings,
# precede call to "accelerate launch" with "TF_CPP_MIN_LOG_LEVEL=3"

# on some systems, tensorflow will attempt to allocate all GPU memory
# to its process at import which will raise a CUDA out-of-memory error
# setting "export TF_FORCE_GPU_ALLOW_GROWTH=true" resolves this
```
Also make sure you have `git-lfs` installed and are logged in the Hub:
```
huggingface-cli login
```

We use [`accelerate`](https://huggingface.co/docs/accelerate/index) to generate code/text in parallel across multiple GPUs:
```bash
accelerate config
```

Evaluation-only mode works on CPU. For large models, specify `--precision` directly rather than via `accelerate config` to keep only one copy of the model in memory. `--load_in_8bit`/`--load_in_4bit` are available with `bitsandbytes` installed and matching `transformers`/`accelerate` versions.

MultiPL-E's execution step needs extra per-language dependencies — the vendored `Dockerfile` (see [Build & deploy](#build--deploy)) already bundles all of them.

## Usage

Generate solutions to code benchmarks with your model, evaluate (and execute) the solutions, or both — by default both generation and evaluation run. For task detail: [`docs/README.md`](docs/README.md).

### Generation and evaluation

```bash
accelerate launch  main.py \
  --model <MODEL_NAME> \
  --tasks <TASK_NAME> \
  --limit <NUMBER_PROBLEMS> \
  --max_length_generation <MAX_LENGTH> \
  --temperature <TEMPERATURE> \
  --do_sample True \
  --n_samples 100 \
  --batch_size 10 \
  --precision <PRECISION> \
  --allow_code_execution \
  --save_generations
```
* `limit` — number of problems to solve; omit to run the full benchmark.
* `allow_code_execution` — executes generated code; off by default, read the displayed warning before enabling.
* Some models with custom code on the HF hub (e.g. [SantaCoder](https://huggingface.co/bigcode/santacoder)) require `--trust_remote_code`; private models need `--use_auth_token`.
* `save_generations` saves post-processed generations to `save_generations_path` (default `generations.json`); `--save_references` saves references too.
* `max_length_generation` is the max token length including input. Default 512; GSM8K/GSM-Hard's 8-shot prompts (per [PAL](https://github.com/reasoning-machines/pal)) take ~1500 tokens, so use `2048` for those tasks.

BLEU-scored tasks (`codexglue_code_to_text-<LANGUAGE>`, `conala`, `concode`) generate one candidate per problem — use `n_samples=1`, `batch_size=1` (`batch_size` must never exceed `n_samples`). APPS: `n_samples=1` for strict/average accuracy, `n_samples>1` for pass@k.

### Generation only

`--generation_only` saves solutions to `save_generation_path` without executing/evaluating — useful when generation and execution need to happen on different machines (e.g. GPU box for generation, a separate CPU/sandboxed worker for execution).

### Evaluation only

Point `--load_generations_path` at an existing generations JSON to evaluate it (you may need to reconfigure `accelerate` for multiple CPUs). `--model` here is documentation-only. Add `--n_samples` to match what was used during generation.

```bash
accelerate launch  main.py   --tasks mbpp  --allow_code_execution  --load_generations_path generations.json  --model incoder-temperature-08
```

## Implementing new tasks

[`docs/guide.md`](docs/guide.md). Contribution guidelines: [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Documentation

Full benchmark and usage documentation: [`docs/README.md`](docs/README.md).

## Remarks

* Data-parallel evaluation across multiple GPUs via `accelerate` assumes the model fits on one GPU.

## Acknowledgements

Thanks to EleutherAI for [lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness), which inspired this framework, and to the BigCode project for the original harness this is forked from.

## Cite as

```
@misc{bigcode-evaluation-harness,
  author       = {Ben Allal, Loubna and
                  Muennighoff, Niklas and
                  Kumar Umapathi, Logesh and
                  Lipkin, Ben and
                  von Werra, Leandro},
  title = {A framework for the evaluation of code generation models},
  publisher = {GitHub},
  journal = {GitHub repository},
  howpublished = {\url{https://github.com/bigcode-project/bigcode-evaluation-harness}},
  year = 2022,
}
```
