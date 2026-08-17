"""Per-language resource sizing and run_timeout (cavekit R4 / R5, T-006 / T-007).

Replaces the two single global placeholder constants that shipped with the R2
contract (``DEFAULT_RESOURCES`` / ``DEFAULT_RUN_TIMEOUT_MS``) with per-language
lookups:

- **Resources (R4):** every execute request carries ``resources: {cpu, ram,
  appDisk}``, sized per language. Compiled languages (C#, Rust, Java, …) get a
  strictly larger budget than the interpreted baseline on at least one axis,
  because an undersized compiled build is SIGKILLed (exit 137) during the
  *compile* step. :func:`resources_for` resolves a language token / invocable
  name to its budget, falling back to the interpreted baseline.

- **run_timeout (R5):** every request carries a per-language ``run_timeout``,
  bounded by Piston's hard 3000 ms global cap (only raisable if theseus sets
  ``PISTON_RUN_TIMEOUT`` globally — an infra ask, out of scope here). The client
  therefore never *assumes* a per-request raise above the cap:
  :func:`run_timeout_for` clamps every value to :data:`PISTON_RUN_TIMEOUT_CAP_MS`.

The tables are the single source of truth for sizing policy; they are plain data
so a deployment can tune a language without touching the execute contract.
"""

from typing import Dict

# ─── Resource budgets (R4) ──────────────────────────────────────────────────
#
# The interpreted baseline is what a scripting runtime (python, ruby, node, …)
# needs to run one small program. Compiled languages must additionally *build*
# the program inside the same budget, so they get a strictly larger allocation
# on at least one axis — an undersized compiled build SIGKILLs (exit 137) at the
# compile step rather than failing a test.

# Interpreted-language baseline. NOTE: ``DEFAULT_RESOURCES`` keeps this exact
# value for back-compat (it is still the fallback for an unmapped language).
INTERPRETED_RESOURCES: Dict[str, int] = {
    "cpu": 1,
    "ram": 256,
    "appDisk": 256,
}

# Compiled-language budget: strictly larger than the interpreted baseline on
# every axis (ram/appDisk headroom for the build step; a second cpu for the
# compiler). Larger on >=1 axis is the R4 requirement; we give margin on all.
COMPILED_RESOURCES: Dict[str, int] = {
    "cpu": 2,
    "ram": 512,
    "appDisk": 512,
}

# Back-compat alias: the fallback budget for an unmapped language is the
# interpreted baseline. The R2 contract + its tests reference this name.
DEFAULT_RESOURCES: Dict[str, int] = dict(INTERPRETED_RESOURCES)

# Per-language resource budgets, keyed by the Piston *invocable* name (what
# ``resolve_language`` returns) AND the common harness token spelling, so a
# caller can look up by either. Configurable per language (not one global
# constant): edit an entry to tune a single language's budget.
LANGUAGE_RESOURCES: Dict[str, Dict[str, int]] = {
    # ── interpreted ──
    "python": dict(INTERPRETED_RESOURCES),
    "javascript": dict(INTERPRETED_RESOURCES),
    "typescript": dict(INTERPRETED_RESOURCES),
    "ruby": dict(INTERPRETED_RESOURCES),
    "php": dict(INTERPRETED_RESOURCES),
    "perl": dict(INTERPRETED_RESOURCES),
    "lua": dict(INTERPRETED_RESOURCES),
    "bash": dict(INTERPRETED_RESOURCES),
    "r": dict(INTERPRETED_RESOURCES),
    "rscript": dict(INTERPRETED_RESOURCES),
    # ── compiled (strictly larger; undersized builds SIGKILL) ──
    "c": dict(COMPILED_RESOURCES),
    "c++": dict(COMPILED_RESOURCES),
    "csharp": dict(COMPILED_RESOURCES),
    "fsharp": dict(COMPILED_RESOURCES),
    "basic": dict(COMPILED_RESOURCES),
    "rust": dict(COMPILED_RESOURCES),
    "java": dict(COMPILED_RESOURCES),
    "scala": dict(COMPILED_RESOURCES),
    "go": dict(COMPILED_RESOURCES),
    "d": dict(COMPILED_RESOURCES),
    "swift": dict(COMPILED_RESOURCES),
    "julia": dict(COMPILED_RESOURCES),
    "racket": dict(COMPILED_RESOURCES),
}

# Invocable names whose runtime compiles before it runs — used to size an
# unmapped-but-known-compiled language (defensive) and to document intent.
COMPILED_INVOCABLES = frozenset(
    {
        "c",
        "c++",
        "csharp",
        "fsharp",
        "basic",
        "rust",
        "java",
        "scala",
        "go",
        "d",
        "swift",
        "julia",
        "racket",
    }
)


def resources_for(language: str) -> Dict[str, int]:
    """Return the ``{cpu, ram, appDisk}`` budget for a language.

    ``language`` may be a Piston invocable name (``"csharp"``, ``"c++"``) or a
    harness token spelling. Resolution order:

    1. an explicit :data:`LANGUAGE_RESOURCES` entry;
    2. the compiled budget if the name is a known compiled invocable;
    3. the interpreted :data:`DEFAULT_RESOURCES` baseline.

    A fresh dict is returned so a caller mutating it cannot poison the table.
    """
    name = (language or "").strip().lower()
    if name in LANGUAGE_RESOURCES:
        return dict(LANGUAGE_RESOURCES[name])
    if name in COMPILED_INVOCABLES:
        return dict(COMPILED_RESOURCES)
    return dict(DEFAULT_RESOURCES)


# ─── run_timeout (R5) ───────────────────────────────────────────────────────
#
# Piston enforces a hard 3000 ms default cap on run_timeout; a per-request value
# above it is silently clamped server-side, so the client never *assumes* a
# raise. Every per-language value below is <= the cap, and :func:`run_timeout_for`
# clamps defensively.

# Piston's hard global run_timeout cap (ms). Only raisable via theseus setting
# PISTON_RUN_TIMEOUT globally (infra ask, out of scope for this client).
PISTON_RUN_TIMEOUT_CAP_MS = 3000

# Back-compat default / fallback (== the cap). The R2 contract + tests reference
# this name.
DEFAULT_RUN_TIMEOUT_MS = 3000

# Per-language run_timeout (ms), keyed like LANGUAGE_RESOURCES. Compiled
# languages want the full budget (build + run inside one run_timeout); some
# interpreted runtimes are given a tighter bound to fail-fast. Every value is
# <= PISTON_RUN_TIMEOUT_CAP_MS — no per-request raise above the global cap.
LANGUAGE_RUN_TIMEOUTS: Dict[str, int] = {
    # ── interpreted (tighter fail-fast bound) ──
    "python": 2000,
    "javascript": 2000,
    "typescript": 2500,
    "ruby": 2000,
    "php": 2000,
    "perl": 2000,
    "lua": 2000,
    "bash": 2000,
    "r": 2500,
    "rscript": 2500,
    # ── compiled (need build headroom; up to the cap) ──
    "c": 3000,
    "c++": 3000,
    "csharp": 3000,
    "fsharp": 3000,
    "basic": 3000,
    "rust": 3000,
    "java": 3000,
    "scala": 3000,
    "go": 3000,
    "d": 3000,
    "swift": 3000,
    "julia": 3000,
    "racket": 3000,
}


def run_timeout_for(language: str) -> int:
    """Return the per-language ``run_timeout`` (ms), clamped to Piston's cap.

    Falls back to :data:`DEFAULT_RUN_TIMEOUT_MS` for an unmapped language, and
    never returns a value above :data:`PISTON_RUN_TIMEOUT_CAP_MS` — the client
    does not assume a per-request raise above the global cap.

    This is the **direct**-backend ceiling. On the gateway path the equivalent
    knob is ``maxRuntimeMs`` with a far more generous 60 s ceiling; see
    :func:`gateway_max_runtime_ms_for`.
    """
    name = (language or "").strip().lower()
    value = LANGUAGE_RUN_TIMEOUTS.get(name, DEFAULT_RUN_TIMEOUT_MS)
    return min(value, PISTON_RUN_TIMEOUT_CAP_MS)


# ─── session-gateway quota units (the live path) ────────────────────────────
#
# The tables above are in Piston's native scale. session-gateway's `resources`
# are something else entirely: small integers checked against a per-app quota
# (the default is `cpu: 2, ram: 2, appDisk: 4`), NOT megabytes. Sending 256 here
# would blow the quota and be rejected, so the gateway path needs its own table.
#
# The compiled budget is grounded rather than guessed: theseus's own testing
# (selfai/gitlab-profile#22) found `cpu:1, ram:1, appDisk:1` SIGKILLs a dotnet
# build at exit 137, and `cpu:2, ram:2, appDisk:4` runs it clean. So the
# compiled budget is exactly the observed-working one, and it happens to equal
# the whole default quota — which is precisely why we asked for a real
# eval-sized quota on self.theseus/gitlab-profile#10.

# MEASURED against the live gateway 2026-07-27, not chosen from the README:
#
#   php x12 sequential @ cpu1/ram1/appDisk1    -> 11 ok, 1 spurious exit-137
#   php x12 sequential @ cpu0.5/ram0.5/appDisk1 -> 12 ok
#
# so the interpreted baseline is the smaller one: at 1/1/1 a trivial always-
# passing program fails ~8% of the time under nothing more than back-to-back
# submission, which would show up as phantom eval failures.
GATEWAY_INTERPRETED_RESOURCES: Dict[str, float] = {"cpu": 0.5, "ram": 0.5, "appDisk": 1}

# Compiled stays at the full 2/2/4. theseus suggested "roughly double the
# interpreted ask", but that was measured too and it is not enough:
#
#   csharp @ 0.5/0.5/1 -> exit 137
#   csharp @ 1/1/2     -> exit 137 (dies during "Getting ready...")
#   csharp @ 1/2/4     -> exit 137
#   csharp @ 2/2/4     -> admitted (the budget theseus originally verified)
#
# cpu is the binding constraint for a dotnet build, not ram. 2/2/4 is two thirds
# of our whole cpu quota, which is why compiled languages will queue behind each
# other rather than run in parallel — acceptable, and better than phantom
# SIGKILLs.
GATEWAY_COMPILED_RESOURCES: Dict[str, float] = {"cpu": 2, "ram": 2, "appDisk": 4}

# The gateway's own per-session ceiling (ms). Much more generous than Piston's
# raw 3000ms run_timeout cap — these are different ceilings, and only this one
# is ours to set per request.
GATEWAY_MAX_RUNTIME_MS_CEILING = 60000


def gateway_resources_for(language: str) -> Dict[str, float]:
    """Return the session-gateway ``{cpu, ram, appDisk}`` quota units for a language.

    Compiled languages get the larger, empirically-verified budget; everything
    else gets the interpreted baseline. Same compiled/interpreted split as
    :func:`resources_for`, different units — do not mix the two.
    """
    name = (language or "").strip().lower()
    if name in COMPILED_INVOCABLES or name in {"cs", "cpp", "ts"}:
        return dict(GATEWAY_COMPILED_RESOURCES)
    budget = LANGUAGE_RESOURCES.get(name)
    if budget is not None and budget == COMPILED_RESOURCES:
        return dict(GATEWAY_COMPILED_RESOURCES)
    return dict(GATEWAY_INTERPRETED_RESOURCES)


def gateway_max_runtime_ms_for(language: str, timeout_seconds: float = None) -> int:
    """Return the gateway ``maxRuntimeMs`` for a language, clamped to its ceiling.

    Unlike :func:`run_timeout_for` this is NOT squeezed into Piston's 3000 ms
    cap: the gateway's ceiling is 60 s, which is generous enough that the local
    harness's 5–15 s per-problem timeouts pass through intact. The timeout-parity
    caveat that applies to the direct path largely disappears here.
    """
    if timeout_seconds is not None:
        value = int(float(timeout_seconds) * 1000)
    else:
        # Reuse the per-language policy, but unclamped by Piston's tiny cap.
        name = (language or "").strip().lower()
        value = LANGUAGE_RUN_TIMEOUTS.get(name, DEFAULT_RUN_TIMEOUT_MS)
    return max(1, min(value, GATEWAY_MAX_RUNTIME_MS_CEILING))
