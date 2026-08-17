"""Tests for harness-token -> Piston invocable resolution (cavekit R3 / T-003).

Covers:
- A resolution table maps each supported harness token -> invocable name + version.
- The mapping is derived from / validated against a GET /api/v2/runtimes payload,
  not a static packages.txt.
- gcc -> c/c++ and dotnet -> csharp/fsharp/basic aliasing resolves to the
  *invocable* name (never the package name).
- An unresolved token raises a clear, named UnknownLanguageError before any
  request is built.
"""

import pytest

from code_eval.piston import (
    PACKAGE_ALIASES,
    SUPPORTED_HARNESS_TOKENS,
    UnknownLanguageError,
    build_resolution_table,
    resolve_language,
)


# A stand-in for `GET /api/v2/runtimes`. `language` is the *invocable* name;
# `runtime` is the package family (gcc / dotnet) that must NEVER be invoked.
RUNTIMES = [
    {"language": "python", "version": "3.10.0", "aliases": ["py", "py3", "python3"]},
    {"language": "c++", "version": "10.2.0", "aliases": ["cpp", "g++"], "runtime": "gcc"},
    {"language": "c", "version": "10.2.0", "aliases": ["gcc"], "runtime": "gcc"},
    {"language": "csharp", "version": "6.12.0", "aliases": ["cs"], "runtime": "dotnet"},
    {"language": "fsharp", "version": "6.12.0", "aliases": ["fs", "f#"], "runtime": "dotnet"},
    {"language": "basic", "version": "6.12.0", "aliases": ["vb"], "runtime": "dotnet"},
    {"language": "d", "version": "10.2.1", "aliases": ["dlang"]},
    {"language": "go", "version": "1.16.2", "aliases": ["golang"]},
    {"language": "java", "version": "15.0.2", "aliases": []},
    {"language": "javascript", "version": "18.15.0", "aliases": ["node", "js"]},
    {"language": "julia", "version": "1.8.5", "aliases": []},
    {"language": "lua", "version": "5.4.4", "aliases": []},
    {"language": "perl", "version": "5.36.0", "aliases": ["pl"]},
    {"language": "php", "version": "8.2.3", "aliases": []},
    {"language": "r", "version": "4.1.1", "aliases": ["rscript"]},
    {"language": "racket", "version": "8.3.0", "aliases": ["rkt"]},
    {"language": "ruby", "version": "3.0.1", "aliases": ["rb"]},
    {"language": "rust", "version": "1.68.2", "aliases": ["rs"]},
    {"language": "scala", "version": "3.2.2", "aliases": []},
    {"language": "swift", "version": "5.3.3", "aliases": []},
    {"language": "bash", "version": "5.2.0", "aliases": ["sh"]},
    {"language": "typescript", "version": "5.0.3", "aliases": ["ts"]},
    # The five whose executors landed with the multiple-runtimes merge.
    {"language": "clojure", "version": "1.10.3", "aliases": ["clj"]},
    {"language": "dart", "version": "2.19.6", "aliases": []},
    {"language": "elixir", "version": "1.11.3", "aliases": ["exs"]},
    {"language": "haskell", "version": "9.0.1", "aliases": ["ghc", "hs"]},
    {"language": "ocaml", "version": "4.12.0", "aliases": ["ml"]},
]


# ─── Supported-token coverage + full resolution table ───────────────────


def test_twenty_four_supported_harness_tokens():
    # Was 19; clojure/dart/elixir/haskell/ocaml gained eval_*.py executors with
    # the multiple-runtimes merge, so the kit's "executor-less five" is stale.
    assert len(SUPPORTED_HARNESS_TOKENS) == 24
    assert set(SUPPORTED_HARNESS_TOKENS) == {
        "cpp", "cs", "d", "go", "java", "javascript", "julia", "lua", "perl",
        "php", "python", "r", "racket", "ruby", "rust", "scala", "sh", "swift", "ts",
        "clojure", "dart", "elixir", "haskell", "ocaml",
    }


def test_resolution_table_maps_every_token_to_name_and_version():
    table = build_resolution_table(RUNTIMES)
    assert set(table) == set(SUPPORTED_HARNESS_TOKENS)
    for token, (name, version) in table.items():
        assert isinstance(name, str) and name
        assert isinstance(version, str) and version
        # never the package name
        assert name not in PACKAGE_ALIASES


@pytest.mark.parametrize(
    "token,expected_name,expected_version",
    [
        ("python", "python", "3.10.0"),
        ("cpp", "c++", "10.2.0"),
        ("cs", "csharp", "6.12.0"),
        ("sh", "bash", "5.2.0"),   # sh -> bash invocable
        ("ts", "typescript", "5.0.3"),
        ("r", "r", "4.1.1"),
        ("javascript", "javascript", "18.15.0"),
    ],
)
def test_resolve_known_tokens(token, expected_name, expected_version):
    assert resolve_language(token, RUNTIMES) == (expected_name, expected_version)


def test_resolve_accepts_file_ext_aliases():
    # containerized_eval keys programs by short spellings too (rb, rs, js).
    assert resolve_language("rb", RUNTIMES) == ("ruby", "3.0.1")
    assert resolve_language("rs", RUNTIMES) == ("rust", "1.68.2")
    assert resolve_language("js", RUNTIMES) == ("javascript", "18.15.0")


# ─── gcc / dotnet aliasing (AC: resolve to the invocable name) ──────────


def test_package_alias_map_documents_gcc_and_dotnet():
    assert PACKAGE_ALIASES["gcc"] == ("c", "c++")
    assert PACKAGE_ALIASES["dotnet"] == ("csharp", "fsharp", "basic")


def test_gcc_and_dotnet_invocables_resolve_to_the_invocable_name():
    # Every invocable a package provides resolves to an invocable runtime name,
    # and its resolved name is never the package name itself.
    for package, invocables in PACKAGE_ALIASES.items():
        for invocable in invocables:
            name, version = resolve_language(invocable, RUNTIMES)
            assert name == invocable
            assert version
            assert name != package


def test_bare_package_name_is_rejected_not_invoked():
    # The package name (gcc / dotnet) is NOT an invocable runtime; resolving it
    # raises before any request, and the message points at the real invocables.
    for package in PACKAGE_ALIASES:
        with pytest.raises(UnknownLanguageError) as excinfo:
            resolve_language(package, RUNTIMES)
        assert "package" in str(excinfo.value).lower()


# ─── Unresolved token -> clear, named error, no guessed runtime ─────────


def test_unknown_token_raises_named_error():
    with pytest.raises(UnknownLanguageError):
        resolve_language("cobol", RUNTIMES)


def test_empty_token_raises_named_error():
    with pytest.raises(UnknownLanguageError):
        resolve_language("   ", RUNTIMES)


def test_token_absent_from_runtimes_raises_not_guesses():
    # Validated against the live runtimes payload, not packages.txt: if julia
    # isn't in the payload, resolution fails loudly rather than guessing.
    runtimes_without_julia = [rt for rt in RUNTIMES if rt["language"] != "julia"]
    with pytest.raises(UnknownLanguageError):
        resolve_language("julia", runtimes_without_julia)
    # And it's dropped from the table rather than fabricated.
    table = build_resolution_table(runtimes_without_julia)
    assert "julia" not in table
