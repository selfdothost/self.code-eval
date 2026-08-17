"""code-eval's own opt-in Piston config surface (cavekit R1 / T-001).

This is a **parallel** knob pair to the FastAPI backend's Tools-scoped
``ENABLE_PISTON_EXECUTION`` / ``PISTON_BASE_URL`` — it deliberately does NOT
reuse the backend's ``selfai_ui/config.py`` values. self.code-eval is a separate
service with its own env-read style (plain ``os.getenv`` / ``os.environ.get``,
same as ``api/main.py`` and the existing ``CODE_EVAL_*`` vars), so we mirror
that here rather than importing a heavyweight settings object.

Both vars default to the "off / in-container unchanged" posture:

- ``ENABLE_PISTON_EXECUTION`` (default ``False``) — when unset, the harness
  never issues an HTTP request to Piston and behaves byte-identically to today.
- ``PISTON_BASE_URL`` (default the live theseus endpoint) — only consulted when
  the flag is on.

The values are read at call time (not import time) so tests and the Flux
manifest can set them via the environment without re-importing the module.
"""

import os

# ─── Env var names (single source of truth) ─────────────────────────────

ENABLE_ENV_VAR = "ENABLE_PISTON_EXECUTION"
BASE_URL_ENV_VAR = "PISTON_BASE_URL"
BACKEND_ENV_VAR = "PISTON_BACKEND"
GATEWAY_URL_ENV_VAR = "SESSION_GATEWAY_URL"
GATEWAY_APP_KEY_ENV_VAR = "SESSION_GATEWAY_APP_KEY"
GATEWAY_PRIORITY_ENV_VAR = "SESSION_GATEWAY_PRIORITY"

# ─── Defaults ───────────────────────────────────────────────────────────

# Piston's own in-cluster ClusterIP. NOTE: two spellings of this name are in
# circulation — selfai/gitlab-profile#22's opening post says
# `piston.self-theseus...`, session-gateway's README says
# `piston.self-theseus-piston...`. Asked which is current on
# self.theseus/gitlab-profile#9. Only consulted on the `direct` backend, which is
# not the live integration path (see BACKEND_* below), so the ambiguity does not
# block the gateway path.
DEFAULT_PISTON_BASE_URL = "http://piston.self-theseus.svc.cluster.local:2000"
DEFAULT_ENABLE_PISTON_EXECUTION = False

# ─── Backend selection ──────────────────────────────────────────────────
#
# Two transports, because the integration path moved after this client's spec
# was written:
#
# - ``gateway`` (**default, the live path**) — self.theseus's session-gateway,
#   which fronts Piston with app-key auth, per-app quota and an admission queue.
#   This is what `selfai/gitlab-profile#22`'s later comments direct callers at.
# - ``direct`` — Piston's own ``/api/v2/execute``, which is what
#   `self.code-eval#5` and #22's *opening post* specified. Kept because it is a
#   genuinely simpler contract to test against and may still be reachable
#   in-cluster, but it is NOT the supported path.
#
# See :mod:`.gateway` for what the gateway path costs us (no per-request version
# pin, no signal field, no distinguishable compile stage).
BACKEND_GATEWAY = "gateway"
BACKEND_DIRECT = "direct"
DEFAULT_PISTON_BACKEND = BACKEND_GATEWAY

# session-gateway's internal Service (the mgmt-network VIP is the same
# endpoint). Port 80.
DEFAULT_SESSION_GATEWAY_URL = "http://session-gateway.self-theseus.svc.cluster.local"

# Admission priority for eval submissions. `low` deliberately: an eval run is a
# long tail of hundreds of executes and must never preempt interactive work.
# `critical` is reserved for mgmtAccess requests and is not ours to take.
DEFAULT_SESSION_GATEWAY_PRIORITY = "low"
VALID_PRIORITIES = frozenset({"critical", "normal", "low"})

# Truthy spellings accepted for the enable flag, mirroring common env-flag usage.
_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var using the service's plain ``os.getenv`` style."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def piston_enabled() -> bool:
    """Return whether Piston-backed execution is opted in for this service.

    Default is ``False`` — when the env var is unset, execution stays entirely
    in-container.
    """
    return _env_bool(ENABLE_ENV_VAR, DEFAULT_ENABLE_PISTON_EXECUTION)


def piston_base_url() -> str:
    """Return the Piston base URL for this service.

    Falls back to the live theseus endpoint when unset. An empty/whitespace
    override falls back to the default rather than yielding a malformed URL.
    """
    raw = os.getenv(BASE_URL_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_PISTON_BASE_URL
    return raw.strip().rstrip("/")


def piston_backend() -> str:
    """Return the selected transport: ``"gateway"`` (default) or ``"direct"``.

    An unrecognised value falls back to the default rather than raising here —
    the transport layer surfaces a clear error at call time, which keeps config
    reading total and testable.
    """
    raw = (os.getenv(BACKEND_ENV_VAR) or "").strip().lower()
    if raw in (BACKEND_GATEWAY, BACKEND_DIRECT):
        return raw
    return DEFAULT_PISTON_BACKEND


def gateway_url() -> str:
    """Return the session-gateway base URL."""
    raw = os.getenv(GATEWAY_URL_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_SESSION_GATEWAY_URL
    return raw.strip().rstrip("/")


def gateway_app_key() -> str:
    """Return the session-gateway application key, or ``""`` when unset.

    Deliberately returns empty rather than raising: the transport turns a
    missing key into a named, actionable error at the point of use, so a
    misconfigured deployment fails loudly with context instead of at import.
    """
    return (os.getenv(GATEWAY_APP_KEY_ENV_VAR) or "").strip()


def gateway_priority() -> str:
    """Return the admission priority for eval submissions (default ``"low"``)."""
    raw = (os.getenv(GATEWAY_PRIORITY_ENV_VAR) or "").strip().lower()
    if raw in VALID_PRIORITIES:
        return raw
    return DEFAULT_SESSION_GATEWAY_PRIORITY
