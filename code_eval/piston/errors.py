"""Named error contract for the Piston execution client.

Every Piston-side failure raises one of these so it can never be silently
collapsed into a 0-score that is indistinguishable from a genuine failing test
(cavekit piston-execution-client R1 / R3). The later execute-contract tier
(T-004/T-005) raises :class:`PistonConnectionError` for transport failures; the
language resolver (T-003) raises :class:`UnknownLanguageError` *before* any
execute request is built.
"""


class PistonError(Exception):
    """Base class for every Piston-client error.

    Callers that want "any Piston failure, but never a silent 0" catch this.
    """


class PistonNotEnabledError(PistonError):
    """Raised if a Piston code path is entered while the feature flag is off.

    The routing seams (T-011 / T-015) check :func:`piston.config.piston_enabled`
    *before* calling the client; this is the belt-and-suspenders guard that
    guarantees no HTTP request is issued to ``PISTON_BASE_URL`` when the flag is
    unset. It surfaces loudly rather than returning a score.
    """


class PistonConnectionError(PistonError):
    """Raised when the Piston endpoint is unreachable or returns a bad response.

    Distinct from a normal non-zero program exit: a connection failure means we
    never got a verdict, so it must not be scored as a failing test. The real
    execute contract (T-004/T-005) raises this from the HTTP layer.
    """


class PistonTimeoutError(PistonError):
    """Raised when an execute request times out at the HTTP transport layer.

    Kept a *sibling* of :class:`PistonConnectionError` (not a subclass) so a
    transport timeout — the retriable cold-start signal — is distinguishable
    from a plain unreachable endpoint. The single-retry policy (T-007) catches
    exactly this to retry once, and a timeout that persists after the retry is
    turned into a timeout-marked :class:`ExecutionResult`, never a silent 0.
    """


class UnknownLanguageError(PistonError):
    """Raised when a harness language token cannot be resolved to a Piston
    invocable runtime.

    Raised *before* any execute request is built, so a wrong or guessed runtime
    is never sent to Piston (cavekit R3).
    """


class GatewayRejectedError(PistonError):
    """Raised when session-gateway rejects a submission before any backend runs.

    A rejection (``missing-key``, ``unknown-key``, ``quota-not-configured``,
    ``invalid-piston-request``, ``max-runtime-exceeds-ceiling``, …) means the
    program never executed, so it must never be scored. ``reason`` is the
    gateway's own machine-readable code, kept as a field so a caller can branch
    on it — a quota problem is operationally different from a malformed request,
    even though both are non-pass.
    """

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(f"session-gateway rejected the submission [{reason}]: {message}")


class GatewayExecutionFailedError(PistonError):
    """Raised when session-gateway settles a submission as ``failed``.

    A ``failed`` record means the program never produced a verdict — every
    attempt failed transiently, or a non-retryable error stopped it (an unknown
    runtime being the common one). The gateway keeps this strictly separate from
    a program that ran and exited non-zero, which comes back as a *success*
    carrying a result. Preserving that split is what stops "this language isn't
    installed" from being scored as "the candidate failed its tests".

    ``kind`` is the gateway's own ``transient`` / ``non-transient`` label.
    """

    def __init__(self, kind: str, message: str = ""):
        self.kind = kind
        super().__init__(f"session-gateway execution failed [{kind}]: {message}")


class UnsupportedEngineError(PistonError):
    """Raised when an eval engine has no shape this seam can route to Piston.

    The "beyond" engine is the live case: it is not a one-shot program and needs
    packages Piston's stock python runtime does not carry. Raising is deliberate
    — falling back to the in-process path would keep executing untrusted
    generated code in-container while the operator believes the sandbox is on.
    """


class MissingExecutorError(PistonError):
    """Raised when a MultiPL-E language has no ``eval_*.py`` executor in the harness.

    The executor gate (multiple-seam kit R3): a language the harness registers
    but cannot actually score must fail loudly here rather than record a 0 as if
    the candidate had been executed and failed. Distinct from
    :class:`UnknownLanguageError`, which means *Piston* has no runtime for a
    language the harness can otherwise score.
    """
