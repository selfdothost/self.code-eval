"""
Ticket-based auth for the code-eval control API (self.code-eval / self.ai#25).

self.ai mints short-lived, scoped JWTs before calling into this control
plane (self.ai#25's proposal: self.ai is the yard's internal ticket-granting
service). This module is the validating side, ported directly from
self.llamolotl's api/auth.py (self.llamolotl#12, the first backend wired to
this pattern, merged self.ai!77 + self.llamolotl!13, 2026-07-09) — same JWT
verification, same fail-closed-if-unconfigured behavior, same
SERVICE_AUTH_SECRET/SERVICE_AUTH_AUDIENCE env convention so this service can
share the existing selfai-service-auth secret. Every mutating/control
endpoint on :8094 requires a valid ticket in the `X-Selfai-Ticket` header —
signature, audience, expiry, and scope are all checked.

This is self.code-eval's leg of the rollout described in
context/kits/cavekit-service-mesh-ticket-auth.md (self.ai repo) — R3 names
self.code-eval/self.language-eval as the last (fourth) leg, after
self.llamolotl, self.curator, and the self.transcribe/self.speak pair,
because job-creation inputs here are already sanitized against injection
(_validate_id/_sanitize_string/_sanitize_value/_sanitize_log_line already
guard the subprocess/log layer) — the marginal severity reduction from
ticket auth is real (anyone with network reach can submit/cancel jobs today)
but smaller than self.curator's raw-code-at-rest surface.

Scope taxonomy (mirror of self.ai's minting side in
api/selfai_ui/routers/evaluations.py — keep both lists in sync):
  tasks:read  - list available benchmark tasks/categories
  jobs:read   - list/get jobs, job logs, job live-stream, results, result
                details/generations (results are keyed by job — same
                grouping convention self.llamolotl and self.curator use for
                job outputs, not a separate scope)
  jobs:create - start a new evaluation job
  jobs:write  - cancel/purge a job

NetworkPolicy-level pod-to-pod restriction is a complementary defense-in-
depth layer, explicitly out of scope here (see self.llamolotl#12's own
docstring — same posture carried forward).
"""

import logging
import os
from typing import Callable, List, Optional

import jwt
from fastapi import Header, HTTPException, status

log = logging.getLogger(__name__)

# Shared HMAC secret with self.ai. Empty by default so a misconfigured
# deployment fails closed (every ticket check 503s) instead of silently
# accepting unsigned requests.
SERVICE_AUTH_SECRET = os.environ.get("SERVICE_AUTH_SECRET", "")
# This service's own audience value — tickets minted for any other
# audience are rejected even if otherwise well-formed and correctly signed.
SERVICE_AUTH_AUDIENCE = os.environ.get("SERVICE_AUTH_AUDIENCE", "self.code-eval")
SERVICE_AUTH_ALGORITHM = "HS256"

TICKET_HEADER = "X-Selfai-Ticket"


class TicketError(HTTPException):
    """A ticket validation failure. Carries a plain-English detail message
    (never the raw exception) so we don't leak signing internals on the wire."""

    def __init__(self, detail: str, status_code: int = status.HTTP_401_UNAUTHORIZED):
        super().__init__(status_code=status_code, detail=detail)


def _decode_ticket(token: str) -> dict:
    if not SERVICE_AUTH_SECRET:
        log.error(
            "SERVICE_AUTH_SECRET is not configured — rejecting all service "
            "tickets on the code-eval API until it is set."
        )
        raise TicketError(
            "Service auth is not configured on this node",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    try:
        claims = jwt.decode(
            token,
            SERVICE_AUTH_SECRET,
            algorithms=[SERVICE_AUTH_ALGORITHM],
            audience=SERVICE_AUTH_AUDIENCE,
        )
    except jwt.ExpiredSignatureError:
        raise TicketError("Service ticket expired")
    except jwt.InvalidAudienceError:
        raise TicketError("Service ticket audience mismatch")
    except jwt.InvalidTokenError as e:
        log.warning("Rejected malformed service ticket: %s", e)
        raise TicketError("Invalid service ticket")

    return claims


def _scopes_from_claims(claims: dict) -> List[str]:
    scope = claims.get("scope", "")
    if isinstance(scope, str):
        return scope.split()
    if isinstance(scope, (list, tuple)):
        return list(scope)
    return []


def require_scope(required_scope: str) -> Callable[..., dict]:
    """FastAPI dependency factory.

    Usage: `Depends(require_scope("jobs:create"))` on a route. Validates
    the `X-Selfai-Ticket` header: present, correctly signed, not expired,
    audience == this service, and `required_scope` is among the ticket's
    granted scopes. Returns the decoded claims (available to the route via
    the dependency's return value, though most routes don't need it).
    """

    def _dependency(
        x_selfai_ticket: Optional[str] = Header(default=None, alias=TICKET_HEADER),
    ) -> dict:
        if not x_selfai_ticket:
            raise TicketError(f"Missing {TICKET_HEADER} header")

        claims = _decode_ticket(x_selfai_ticket)

        granted = _scopes_from_claims(claims)
        if required_scope not in granted:
            raise TicketError(
                f"Ticket does not grant required scope '{required_scope}'",
                status.HTTP_403_FORBIDDEN,
            )

        return claims

    return _dependency
