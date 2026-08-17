"""Piston execution client for self.code-eval.

Opt-in adapter that lets the harness execute one program in theseus's live
Piston sandbox instead of the in-container ``safe_subprocess`` / ``exec()``
paths. Everything here is **default off** — when ``ENABLE_PISTON_EXECUTION`` is
unset the harness never imports an HTTP path and behaves byte-identically to
today.

This package is the reusable adapter both execution seams (MultiPL-E and the
Python ``exec()`` engines) will call. It currently ships:

- ``config``    — the code-eval service's own opt-in config surface (T-001).
- ``languages`` — harness-token -> Piston invocable runtime resolution (T-003).
- ``errors``    — the named error contract so failures never masquerade as a
  silent 0-score.
- ``runtimes``  — fetch + cache of ``GET /api/v2/runtimes`` (T-004).
- ``execute``   — the core ``/api/v2/execute`` contract + result parsing (T-004),
  plus the single cold-start retry policy (T-007).
- ``sizing``    — per-language resource budgets and ``run_timeout`` (T-006/T-007).
- ``taxonomy``  — result classification, so an execution failure is always a
  distinct signal and never a silent 0-score (T-005).

Decision grounding:
``context/treasuremaps/2026-07-24-evals-piston-theseus.md``.
"""

from .client import piston_get, piston_json, piston_post, require_piston_enabled
from .config import (
    BACKEND_DIRECT,
    BACKEND_GATEWAY,
    DEFAULT_PISTON_BASE_URL,
    DEFAULT_SESSION_GATEWAY_URL,
    ENABLE_ENV_VAR,
    BASE_URL_ENV_VAR,
    gateway_app_key,
    gateway_priority,
    gateway_url,
    piston_backend,
    piston_base_url,
    piston_enabled,
)
from .errors import (
    GatewayExecutionFailedError,
    GatewayRejectedError,
    MissingExecutorError,
    PistonError,
    PistonNotEnabledError,
    PistonConnectionError,
    PistonTimeoutError,
    UnknownLanguageError,
    UnsupportedEngineError,
)
from .languages import (
    HARNESS_TOKEN_TO_INVOCABLE,
    PACKAGE_ALIASES,
    SUPPORTED_HARNESS_TOKENS,
    build_resolution_table,
    resolve_language,
)
from .runtimes import (
    RUNTIMES_PATH,
    clear_runtimes_cache,
    get_runtimes,
)
from .sizing import (
    COMPILED_INVOCABLES,
    COMPILED_RESOURCES,
    DEFAULT_RESOURCES,
    DEFAULT_RUN_TIMEOUT_MS,
    INTERPRETED_RESOURCES,
    LANGUAGE_RESOURCES,
    LANGUAGE_RUN_TIMEOUTS,
    GATEWAY_COMPILED_RESOURCES,
    GATEWAY_INTERPRETED_RESOURCES,
    GATEWAY_MAX_RUNTIME_MS_CEILING,
    PISTON_RUN_TIMEOUT_CAP_MS,
    gateway_max_runtime_ms_for,
    gateway_resources_for,
    resources_for,
    run_timeout_for,
)
from .execute import (
    EXECUTE_PATH,
    MAX_COLD_START_RETRIES,
    ExecutionResult,
    PistonStage,
    build_execute_payload,
    execute,
    execute_with_cold_start_retry,
    parse_execute_response,
    timeout_result,
)
from .taxonomy import (
    NON_TEST_FAILURE_CLASSES,
    ResultClass,
    Verdict,
    classify,
    classify_exception,
    safe_verdict,
    verdict_for,
    verdict_for_exception,
)
from .gateway import (
    GATEWAY_MAX_RUNTIME_MS_CEILING as GATEWAY_SUBMIT_CEILING_MS,
    SUBMIT_PATH,
    build_submit_payload,
    gateway_execute,
    poll_until_settled,
)
from .multiple_seam import (
    STATUS_PISTON_ERROR,
    STATUS_SIGKILL,
    eval_string_script_piston,
    executor_backed_tokens,
    require_executor,
    result_to_harness_dict,
    run_timeout_ms_for,
    should_route_to_piston,
    status_for,
)
from .python_seam import (
    ANSWER_SENTINEL,
    BEYOND_UNSUPPORTED_REASON,
    FAIL_SENTINEL,
    PASS_SENTINEL,
    SUPPORTED_ENGINES,
    assemble_check_program,
    assemble_pal_program,
    check_correctness_piston,
    interpret_check_result,
    require_supported_engine,
    run_program_piston,
)

__all__ = [
    "DEFAULT_PISTON_BASE_URL",
    "ENABLE_ENV_VAR",
    "BASE_URL_ENV_VAR",
    "BACKEND_DIRECT",
    "BACKEND_GATEWAY",
    "DEFAULT_SESSION_GATEWAY_URL",
    "gateway_app_key",
    "gateway_priority",
    "gateway_url",
    "piston_backend",
    "piston_base_url",
    "piston_enabled",
    "piston_get",
    "piston_json",
    "piston_post",
    "require_piston_enabled",
    "GatewayExecutionFailedError",
    "GatewayRejectedError",
    "MissingExecutorError",
    "PistonError",
    "PistonNotEnabledError",
    "PistonConnectionError",
    "PistonTimeoutError",
    "UnknownLanguageError",
    "UnsupportedEngineError",
    "HARNESS_TOKEN_TO_INVOCABLE",
    "PACKAGE_ALIASES",
    "SUPPORTED_HARNESS_TOKENS",
    "build_resolution_table",
    "resolve_language",
    "RUNTIMES_PATH",
    "clear_runtimes_cache",
    "get_runtimes",
    "COMPILED_INVOCABLES",
    "COMPILED_RESOURCES",
    "DEFAULT_RESOURCES",
    "DEFAULT_RUN_TIMEOUT_MS",
    "INTERPRETED_RESOURCES",
    "LANGUAGE_RESOURCES",
    "LANGUAGE_RUN_TIMEOUTS",
    "PISTON_RUN_TIMEOUT_CAP_MS",
    "GATEWAY_COMPILED_RESOURCES",
    "GATEWAY_INTERPRETED_RESOURCES",
    "GATEWAY_MAX_RUNTIME_MS_CEILING",
    "gateway_max_runtime_ms_for",
    "gateway_resources_for",
    "resources_for",
    "run_timeout_for",
    "EXECUTE_PATH",
    "MAX_COLD_START_RETRIES",
    "ExecutionResult",
    "PistonStage",
    "build_execute_payload",
    "execute",
    "execute_with_cold_start_retry",
    "parse_execute_response",
    "timeout_result",
    "NON_TEST_FAILURE_CLASSES",
    "ResultClass",
    "Verdict",
    "classify",
    "classify_exception",
    "safe_verdict",
    "verdict_for",
    "verdict_for_exception",
    "GATEWAY_SUBMIT_CEILING_MS",
    "SUBMIT_PATH",
    "build_submit_payload",
    "gateway_execute",
    "poll_until_settled",
    "STATUS_PISTON_ERROR",
    "STATUS_SIGKILL",
    "eval_string_script_piston",
    "executor_backed_tokens",
    "require_executor",
    "result_to_harness_dict",
    "run_timeout_ms_for",
    "should_route_to_piston",
    "status_for",
    "ANSWER_SENTINEL",
    "BEYOND_UNSUPPORTED_REASON",
    "FAIL_SENTINEL",
    "PASS_SENTINEL",
    "SUPPORTED_ENGINES",
    "assemble_check_program",
    "assemble_pal_program",
    "check_correctness_piston",
    "interpret_check_result",
    "require_supported_engine",
    "run_program_piston",
]
