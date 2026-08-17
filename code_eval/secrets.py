"""Keeping credentials out of the artifacts a run leaves behind (#11).

A job's ``api_key`` used to be persisted verbatim to ``results.json``, the job
log, ``.jobs.json``, and the jobs API, and passed to the eval subprocess in
argv where any ``ps`` could read it. The natural key to hand a job is the
broadest one in the system — the admin key the harness needs to reach
``/api/completions`` — so the highest-value credential ended up in the artifact
people copy around by hand to compare runs.

A fingerprint rather than a blunt ``"***"``: telling whether two runs used the
same credential is a real diagnostic question ("did this run use the key I think
it did?"), and it can be answered without carrying the secret. A truncated
SHA-256 answers it; last-4 of the key itself would leak real key material for
no extra benefit.
"""

import hashlib
from typing import Any, Dict, Mapping

#: Keys whose values must never reach an artifact. Matched case-insensitively
#: against the exact field name — deliberately not a substring match, so a field
#: legitimately called something like `api_key_required` is not mangled.
SECRET_FIELDS = frozenset({"api_key", "hf_token", "use_auth_token", "password", "token"})

#: How much of the digest to keep. Enough to distinguish keys in practice,
#: far too little to attack the preimage of a high-entropy secret.
_FINGERPRINT_CHARS = 12


def fingerprint(value: Any) -> str:
    """A stable, non-reversible label for a secret.

    Empty/absent values are reported as such rather than fingerprinted, because
    "no key was used" and "some key was used" is exactly the distinction a
    reader of the artifact needs first.
    """
    if value is None:
        return "none"
    text = str(value)
    if not text:
        return "none"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_FINGERPRINT_CHARS]
    return f"sha256:{digest}"


def redact_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy ``config`` with every secret field replaced by its fingerprint.

    Returns a new dict; the caller's mapping is never mutated, so redacting for
    an artifact cannot accidentally strip the value the running process still
    needs.
    """
    redacted: Dict[str, Any] = {}
    for key, value in config.items():
        if key.lower() in SECRET_FIELDS and value is not None:
            # `use_auth_token` is a bool in practice; a bool is not a secret and
            # fingerprinting it would destroy readable information for nothing.
            redacted[key] = value if isinstance(value, bool) else fingerprint(value)
        else:
            redacted[key] = value
    return redacted
