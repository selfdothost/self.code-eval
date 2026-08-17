"""Harness language token -> Piston invocable runtime resolution (cavekit R3 / T-003).

Piston's *invocable* runtime name is NOT the package name in ``packages.txt``:
the ``gcc`` package provides the invocables ``c`` / ``c++``; the ``dotnet``
package provides ``csharp`` / ``fsharp`` / ``basic``. So the mapping here is
resolved and validated against a live ``GET /api/v2/runtimes`` response — never
against a static package list.

Each of the **24** executor-backed harness languages (the ones with an
``eval_*.py`` in ``multiple_metrics/``) maps to an ordered tuple of *candidate*
invocable names/aliases.

.. note::
   The grounding treasuremap and the client kit were written against a
   19-executor snapshot and call clojure/dart/elixir/haskell/ocaml
   "executor-less". That premise is **stale**: those five executors landed on
   ``main`` in "multiple: add clj/dart/elixir/hs/ml runtimes + eval scripts",
   so ``multiple_metrics/`` now ships 24 ``eval_*.py`` files. The kit anticipated
   this — it asks the set be validated against the live executor files rather
   than hard-coded — so the table below is the validated 24, not the kit's 19.
   Whether Piston can actually *run* each of them is a separate question,
   answered per-deployment by ``GET /api/v2/runtimes``: a language with no live
   runtime raises :class:`UnknownLanguageError` rather than being guessed.

:func:`resolve_language` picks the first candidate that
actually appears in the runtimes payload (matched against a runtime's
``language`` or its ``aliases``) and returns that runtime's canonical invocable
name plus its concrete version. An unresolved token raises
:class:`UnknownLanguageError` *before* any execute request is built.
"""

from typing import Dict, List, Mapping, Sequence, Tuple

from .errors import UnknownLanguageError

# ─── Harness token -> candidate Piston invocable names ──────────────────
#
# Keyed by the canonical harness language token. Values are ordered candidate
# invocable names (or aliases) to look for in the runtimes payload; the first
# one present wins. Multiple candidates make resolution robust to the exact
# invocable spelling a given Piston deployment exposes (e.g. R as ``rscript``
# vs ``r``, shell as ``bash`` vs ``sh``) — the runtimes payload, not this
# table, is the source of truth.
HARNESS_TOKEN_TO_INVOCABLE: Dict[str, Tuple[str, ...]] = {
    "cpp": ("c++", "cpp"),
    "cs": ("csharp", "cs"),
    "d": ("d", "dlang"),
    "go": ("go", "golang"),
    "java": ("java",),
    "javascript": ("javascript", "js", "node"),
    "julia": ("julia",),
    "lua": ("lua",),
    "perl": ("perl", "pl"),
    "php": ("php",),
    "python": ("python", "python3", "py"),
    "r": ("rscript", "r"),
    "racket": ("racket",),
    "ruby": ("ruby", "rb"),
    "rust": ("rust", "rs"),
    "scala": ("scala",),
    "sh": ("bash", "sh"),
    "swift": ("swift",),
    "ts": ("typescript", "ts"),
    # ── the five that landed with the multiple-runtimes merge ──
    "clojure": ("clojure", "clj"),
    "dart": ("dart",),
    "elixir": ("elixir", "exs"),
    "haskell": ("haskell", "ghc", "hs"),
    "ocaml": ("ocaml", "ml"),
}

# ─── File-extension / short aliases the harness also uses as a token ─────
#
# containerized_eval's EVALUATORS dict keys programs by several spellings
# (e.g. "rb", "js", "rs"). Normalize those to the canonical token above so the
# resolver accepts whatever the seam hands it.
_TOKEN_ALIASES: Dict[str, str] = {
    "c++": "cpp",
    "csharp": "cs",
    "dlang": "d",
    "golang": "go",
    "js": "javascript",
    "node": "javascript",
    "py": "python",
    "python3": "python",
    "pl": "perl",
    "rb": "ruby",
    "rs": "rust",
    "rkt": "racket",
    "rscript": "r",
    "bash": "sh",
    "ts": "ts",
    "typescript": "ts",
    "clj": "clojure",
    "exs": "elixir",
    "hs": "haskell",
    "ghc": "haskell",
    "ml": "ocaml",
    # containerized_eval also keys some programs by their converter filename.
    "notypes.py": "python",
    "humaneval_to_dlang.py": "d",
    "humaneval_to_r.py": "r",
    "go_test.go": "go",
    "jl": "julia",
}

# ─── Package name -> invocable names it provides (the gcc/dotnet aliasing) ─
#
# Documents that a *package* name must never be sent to Piston as a runtime.
# These are the aliasing cases called out in the treasuremap: the resolver
# rejects the bare package name and points the caller at the real invocables.
PACKAGE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "gcc": ("c", "c++"),
    "dotnet": ("csharp", "fsharp", "basic"),
}

# The 24 executor-backed harness languages (validated against the live
# multiple_metrics/eval_*.py set — see the module note on the stale 19). Exposed
# for coverage assertions.
SUPPORTED_HARNESS_TOKENS: Tuple[str, ...] = tuple(sorted(HARNESS_TOKEN_TO_INVOCABLE))


def _candidates_for(token: str) -> Tuple[str, Tuple[str, ...]]:
    """Return ``(canonical_token, candidate_invocable_names)`` for a raw token.

    Raises :class:`UnknownLanguageError` for a bare package name (gcc/dotnet)
    or any token with no known invocable mapping — before any request is built.
    """
    norm = token.strip().lower()
    if not norm:
        raise UnknownLanguageError("Empty language token cannot be resolved to a Piston runtime.")

    if norm in PACKAGE_ALIASES:
        invocables = ", ".join(PACKAGE_ALIASES[norm])
        raise UnknownLanguageError(
            f"{token!r} is a Piston *package* name, not an invocable runtime. "
            f"Use one of its invocable runtimes instead: {invocables}."
        )

    canonical = _TOKEN_ALIASES.get(norm, norm)
    if canonical in HARNESS_TOKEN_TO_INVOCABLE:
        return canonical, HARNESS_TOKEN_TO_INVOCABLE[canonical]

    # A raw invocable name (e.g. "fsharp", "basic", "c") that isn't one of the
    # 24 harness tokens: pass it through as its own sole candidate so it can
    # still be validated against the runtimes payload.
    return canonical, (canonical,)


def _index_runtimes(runtimes: Sequence[Mapping]) -> Dict[str, Mapping]:
    """Build a lookup from every invocable spelling -> its runtime entry.

    Indexes each runtime's ``language`` plus every entry in its ``aliases`` so a
    candidate can be matched against either.
    """
    index: Dict[str, Mapping] = {}
    for rt in runtimes:
        language = rt.get("language")
        if not language:
            continue
        names: List[str] = [language]
        names.extend(rt.get("aliases", []) or [])
        for name in names:
            index.setdefault(str(name).strip().lower(), rt)
    return index


def resolve_language(token: str, runtimes: Sequence[Mapping]) -> Tuple[str, str]:
    """Resolve a harness language token to a Piston ``(invocable_name, version)``.

    ``runtimes`` is the parsed JSON list from ``GET /api/v2/runtimes``. The
    returned name is the runtime's canonical ``language`` (its invocable name),
    never the package name.

    Raises :class:`UnknownLanguageError` if the token maps to no runtime present
    in ``runtimes`` — so no execute request is ever built with a guessed runtime.
    """
    canonical, candidates = _candidates_for(token)
    index = _index_runtimes(runtimes)

    for candidate in candidates:
        rt = index.get(candidate.strip().lower())
        if rt is not None:
            return str(rt["language"]), str(rt["version"])

    available = ", ".join(sorted(index)) or "(none)"
    raise UnknownLanguageError(
        f"Language token {token!r} (candidates: {', '.join(candidates)}) has no "
        f"matching invocable runtime in GET /api/v2/runtimes. Available: {available}."
    )


def build_resolution_table(
    runtimes: Sequence[Mapping],
) -> Dict[str, Tuple[str, str]]:
    """Resolve every supported harness token against ``runtimes``.

    Returns ``{token: (invocable_name, version)}`` for the tokens that have a
    matching runtime. Tokens with no live runtime (e.g. a broken/absent package)
    are simply omitted rather than guessed — callers that need a specific
    language should use :func:`resolve_language` and handle
    :class:`UnknownLanguageError`.
    """
    table: Dict[str, Tuple[str, str]] = {}
    for token in SUPPORTED_HARNESS_TOKENS:
        try:
            table[token] = resolve_language(token, runtimes)
        except UnknownLanguageError:
            continue
    return table
