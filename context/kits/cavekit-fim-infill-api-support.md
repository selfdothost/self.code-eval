---
created: "2026-07-18"
last_edited: "2026-07-18"
---

# Cavekit: FIM/Infill Prompt Support in API Generation Mode

## Scope
Enabling `code_eval/api_generation.py` to evaluate infill-shaped tasks
(`ds1000-*-insertion`, `santacoder_fim`, `starcoder_fim`, and any future
`*-fim` task whose `task.get_prompt()` returns `{"prefix", "suffix"}`)
against a remote llama.cpp-backed endpoint (self.llamolotl), instead of
raising `ValueError("API mode does not support infill prompts")`
(`code_eval/api_generation.py:73`).

## Why this is a kit, not a same-day patch
Traced live against the actual deployed stack (self.llamolotl commit
`699bb0e9`, self-code-eval `589`, both running in `self-ai` namespace,
2026-07-18) before writing this. The blocker isn't "figure out the right
prompt string" — llama.cpp already does that per-model, automatically, on
the backend. The blocker is that fixing this properly touches **two repos**
and adds a **second, structurally different request/response protocol**
into a module whose entire contract today is "one generic OpenAI-compatible
endpoint, one request shape, one response shape." That's more than a
contained diff to `api_generation.py`; see Requirements below for the
actual shape of the work.

## Findings (live-verified, not inferred)

### F1: The local HF path's infill support is itself narrow
`code_eval/utils.py` `TokenizedDataset._make_infill_prompt()` (lines
142–155) and `_parse_infill()` (lines 177–199) hardcode a 4-entry
allowlist keyed on `tokenizer.name_or_path`: `facebook/incoder-1B`,
`facebook/incoder-6B`, `bigcode/santacoder`, `bigcode/starcoder` /
`bigcode/starcoderbase`. Any other `model_id` — including every model
currently deployed on self.llamolotl (Qwen2.5-Coder-32B-Instruct,
Qwen3-Coder-Next, GLM-4.5-Air) — hits `raise ValueError(f"Infilling not
yet supported for: {model_id}")` even in **local HF mode**. So "the local
path already knows how to do this for our model families" was the wrong
premise to fix API mode against — it doesn't, for any model we actually
run. Whatever API-mode solution we build should not be scoped down to only
match this narrow local allowlist.

### F2: llama.cpp (self.llama, vendored) has a native, working `/infill` endpoint
Route registered in `tools/server/server.cpp:183`:
`ctx_http.post("/infill", ex_wrapper(routes.post_infill));`. Handler in
`tools/server/server-context.cpp:3465-3541`. It:
- Requires `input_prefix` and `input_suffix` (not `prefix`/`suffix`, not a
  single `prompt` string) — `server-context.cpp:3490-3496`.
- Checks the loaded model's GGUF vocab for FIM tokens
  (`llama_vocab_fim_pre/suf/mid`) and returns
  `ERROR_TYPE_NOT_SUPPORTED` if absent (`server-context.cpp:3469-3481`) —
  i.e. infill support is a property of the loaded GGUF, not something the
  caller has to know in advance per model family.
- Builds the actual model-specific prompt internally via
  `format_prompt_infill()` (`server-common.cpp:1836+`), which embeds the
  FIM tokens the tokenizer's vocab declares — the caller never constructs
  special-token strings itself.
- Is explicitly **not OpenAI-compatible**: `server-context.cpp:3540`
  comment says so verbatim, response shape is `{"content": ..., "prompt":
  ..., "tokens_cached": ...}` — no `choices[]`, no `text`/`message.content`.

Confirmed live via `kubectl exec` into `self-llamolotl-6f5c7c9c66-6h8mh`,
POST to the loaded Qwen3-Coder-Next-UD-Q3_K_XL instance's `/infill`
(both the direct per-model port and the router's `:8080/infill` with a
`model` field both work):
```
POST /infill {"input_prefix":"def add(a, b):\n    ","input_suffix":"\n    return result\n","n_predict":16}
→ 200, content: "result = a + b\n```"
  internal prompt (echoed back): "<|repo_name|>myproject\n<|file_sep|>filename\n<|fim_prefix|>def add(a, b):\n    <|fim_suffix|>\n    return result\n<|fim_middle|>"
```
That's the Qwen FIM token convention (`<|fim_prefix|>` /
`<|fim_suffix|>` / `<|fim_middle|>`, plus optional repo-context tokens
`<|repo_name|>` / `<|file_sep|>`), applied automatically by llama.cpp
because it's declared in the GGUF vocab — self.code-eval does not need to
know or hardcode it.

### F3: self.ai's own API gateway has no `/infill` passthrough
`selfai_ui/routers/llamolotl.py` (in `selfai/self.ai`'s `api/` tier)
proxies `/chat/completions` and `/completions` (→ llamolotl's
`/v1/chat/completions` / `/v1/completions`) but has **no route at all**
for `/infill`. self-code-eval jobs go through this gateway
(`api_endpoint: "http://selfai-api:80/api/completions"` in every job
observed in `/workspace/results/.jobs.json` on the live pod) — it does not
talk to self.llamolotl directly and has no config surface for a
self.llamolotl base URL. `mint_service_ticket()`/`X-Selfai-Ticket` is the
established inter-service auth pattern for self.llamolotl's *control*
port (`:8093`, admin actions) — the inference port (`:8080`, what
`/infill` lives on) is proxied with a plain `Authorization: Bearer`
passthrough today, same as `/completions`/`/chat/completions`; a new
`/infill` route would follow that existing plain-bearer pattern, not the
ticket pattern (self.code-eval is explicitly listed as NOT wired into the
ticket system yet — see `api/selfai_ui/utils/service_auth.py` docstring).

### F4: OpenAI's own API had a `suffix` param once — it's gone, and unrelated
The legacy OpenAI Completions API (`text-davinci-003` era) accepted a
`suffix` field on `/v1/completions` for insert-mode. OpenAI deprecated the
underlying models and effectively retired that field; the current
OpenAI-compatible convention (and what `code_eval/api_generation.py`
targets — "OpenAI-compatible endpoint") has no standard `suffix` param.
llama.cpp does not implement one on `/v1/completions` either — confirmed
by reading `server-context.cpp`'s completions handler: no `suffix` key is
read anywhere outside the dedicated `/infill` handler. So there is no
"just add a `suffix` field to the existing request" shortcut; the FIM path
is a structurally different endpoint, not a request-shape tweak.

## Requirements

### R1: New `/infill` passthrough on self.ai's API gateway
**Description:** Add a proxy route in `selfai_ui/routers/llamolotl.py`
(selfai/self.ai repo, not this repo) mirroring the existing
`/completions` route (same plain-bearer auth, same `LLAMOLOTL_BASE_URLS`
selection, same `model` → `url_idx` resolution via
`get_llamolotl_url()`), forwarding to `{llamolotl_url}/infill`.
**Acceptance Criteria:**
- [ ] `POST {gateway}/api/infill` forwards `input_prefix`/`input_suffix`/
      `input_extra`/`model` to the resolved llamolotl backend's `/infill`
- [ ] Response passed through unmodified (non-OAI shape preserved, not
      coerced into `choices[]`)
- [ ] 501/`ERROR_TYPE_NOT_SUPPORTED` from llamolotl (model has no FIM
      vocab tokens) surfaces as a clear 4xx, not a 500
**Dependencies:** none (independent of self.code-eval; lives in a
different repo/deploy unit)

### R2: New config surface in self-code-eval for infill-capable endpoints
**Description:** `api/main.py`'s `JobCreate` and `main.py`'s CLI args have
no way to express "this endpoint also has an `/infill` sibling I should
use for infill-shaped tasks." Needs either (a) a derived-URL convention
(swap `/completions` suffix for `/infill` on the same base), or (b) an
explicit `infill_endpoint` field, decided deliberately rather than
guessed. Given self-code-eval's `api_endpoint` is meant to stay
provider-agnostic ("OpenAI-compatible endpoint URL" per the `JobCreate`
docstring), prefer (b): infill-capability is opt-in, not inferred from URL
shape, so non-llama.cpp providers aren't silently probed for an endpoint
that doesn't exist for them.
**Acceptance Criteria:**
- [ ] `JobCreate` gains an optional `infill_endpoint` (or equivalent) field
- [ ] `main.py` gains a matching `--infill_endpoint` CLI flag, `None` by
      default (infill tasks still raise clearly if unset, not silently
      fall back to the broken completions path)
- [ ] Existing non-infill task runs are unaffected (field is optional,
      no behavior change when absent)
**Dependencies:** R1 (nothing to point the new field at otherwise)

### R3: Infill request/response translation in `api_generation.py`
**Description:** For prompts shaped `{"prefix", "suffix"}`, when
`args.infill_endpoint` is set, POST `{"input_prefix": prefix,
"input_suffix": suffix, "model": args.model, "n_predict":
args.max_length_generation, ...}` to `args.infill_endpoint` instead of
building a chat/completions request. Parse the llama.cpp-native response
(`content` field) instead of `choices[]`/`text`/`message.content`. If
`args.infill_endpoint` is unset, keep raising the current clear
`ValueError` (don't guess).
**Acceptance Criteria:**
- [ ] Infill-shaped prompts route to the infill request builder, not the
      chat/completions one, only when `infill_endpoint` is configured
- [ ] Request uses `input_prefix`/`input_suffix` (not a synthesized single
      `prompt` string with hand-embedded FIM tokens — llama.cpp does that
      internally per F2, don't duplicate it client-side)
- [ ] Response parsing reads `content`, matching the confirmed-live
      `/infill` response shape (F2), not the OAI `choices[]` shape
- [ ] `postprocess_generation()` / `_parse_infill()` compatibility
      verified: `_parse_infill()` (`code_eval/utils.py:177-199`) still
      keys off `tokenizer.name_or_path`'s hardcoded allowlist (F1) — API
      mode has no local `tokenizer` object, so this needs its own
      postprocessing path, not reuse of the HF-mode one as-is
- [ ] `set(prompt_contents.keys()) == {"prefix", "suffix"}` branch in
      `api_generation.py:72-73` replaced with the new path; error message
      preserved for the `infill_endpoint`-unset case
**Dependencies:** R1, R2

### R4: Decide the FIM-postprocessing story independent of the hardcoded model allowlist
**Description:** F1 established that even local-mode FIM support is
scoped to 4 specific `model_id`s that don't include anything currently
deployed. Since llama.cpp's `/infill` embeds FIM tokens automatically
based on GGUF vocab (F2) regardless of model family, API-mode infill
doesn't need the same hardcoded allowlist to *generate* a working prompt —
but scoring still needs to know how to strip the model's response back to
just the generated code (equivalent of `_parse_infill()`). Decide whether
that's: (a) trivial because `/infill`'s `content` field is already just
the generated span with no prompt echo needed (worth re-confirming
per-model — the F2 test above showed `content` was already just the
completion, not prefix+completion+suffix), or (b) still needs
family-specific stripping for some models.
**Acceptance Criteria:**
- [ ] Confirmed (via live test matching F2's pattern) whether
      `/infill`'s `content` response is clean per-model, or needs stripping,
      across at least the FIM-capable models actually deployed on
      self.llamolotl at decision time
- [ ] `code_eval/tasks/santacoder_fim.py` and `ds1000.py`'s
      `postprocess_generation()` reviewed for whether they still apply
      (they currently assume the HF local-mode full-prompt-echo shape)
**Dependencies:** R3

## Out of Scope
- Extending the local HF-mode `_make_infill_prompt()`/`_parse_infill()`
  allowlist (F1) to cover Qwen/GLM families — separate piece of work, not
  needed for API-mode support since llama.cpp handles token embedding
  server-side.
- Any change to self.llamolotl itself — its `/infill` endpoint already
  works as needed (F2); this kit is entirely about self-code-eval and the
  self.ai API gateway catching up to it.
- Non-llama.cpp OpenAI-compatible providers — none of them expose an
  infill-shaped endpoint under any name; this kit is llama.cpp-specific by
  necessity, and R2's opt-in design is meant to keep that explicit rather
  than papered over.

## Cross-References
- `code_eval/api_generation.py:72-73` — the current hard block this kit
  replaces.
- `code_eval/utils.py:142-199` — the local HF-mode infill path (F1), read
  for context, not reused as-is.
- `selfai/self.ai` repo, `api/selfai_ui/routers/llamolotl.py` — R1's
  target file (different repo).
- `selfai/self.ai` repo, `api/selfai_ui/utils/service_auth.py` — documents
  self-code-eval is explicitly not yet wired into the ticket-auth system;
  R1's new route should follow the existing plain-bearer pattern used by
  `/completions`, not introduce ticket auth as a side effect.
- self.llama (vendored llama.cpp fork) `tools/server/server-context.cpp:3465-3541`
  and `server-common.cpp:1836+` — F2's evidence.

## Changelog
- 2026-07-18: Initial kit, written after live-verifying `/infill` against
  self.llamolotl (commit `699bb0e9`) and the self.ai API gateway's missing
  passthrough. GitLab issue #2 (self.code-eval), item 2, Part A.
