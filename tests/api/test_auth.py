"""
self.ai#25 (last leg): tests for the service-ticket auth layer on the
code-eval control API (:8094). Ported from self.llamolotl's own
api/tests/test_auth.py (self.llamolotl#12) via self.curator's
tests/api/test_auth.py (self.curator#5) — same structure, same coverage
shape, adapted to self.code-eval's scope taxonomy and route surface.

Uses a plain TestClient with SERVICE_AUTH_SECRET configured but no default
ticket attached, so each test controls exactly what ticket — if any — is
sent.
"""

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SERVICE_AUTH_AUDIENCE, TEST_SERVICE_AUTH_SECRET, mint_test_ticket


@pytest.fixture
def auth_client(temp_workspace):
    """TestClient with SERVICE_AUTH_SECRET configured but NO default ticket
    attached — each test attaches whatever ticket it wants to exercise."""
    import auth as auth_module
    from main import app

    original_secret = auth_module.SERVICE_AUTH_SECRET
    auth_module.SERVICE_AUTH_SECRET = TEST_SERVICE_AUTH_SECRET

    c = TestClient(app)

    yield c

    auth_module.SERVICE_AUTH_SECRET = original_secret


class TestMissingOrMalformedTicket:
    def test_no_ticket_header_is_rejected(self, auth_client):
        resp = auth_client.get("/api/jobs")
        assert resp.status_code == 401
        assert "X-Selfai-Ticket" in resp.json()["detail"]

    def test_malformed_ticket_is_rejected(self, auth_client):
        resp = auth_client.get("/api/jobs", headers={"X-Selfai-Ticket": "not-a-jwt"})
        assert resp.status_code == 401
        assert "Invalid service ticket" in resp.json()["detail"]

    def test_empty_ticket_header_is_rejected(self, auth_client):
        resp = auth_client.get("/api/jobs", headers={"X-Selfai-Ticket": ""})
        assert resp.status_code == 401


class TestExpiryAndAudience:
    def test_expired_ticket_is_rejected(self, auth_client):
        now = int(time.time())
        expired = jwt.encode(
            {
                "iss": "self.ai",
                "aud": TEST_SERVICE_AUTH_AUDIENCE,
                "scope": "jobs:read",
                "iat": now - 600,
                "exp": now - 60,
            },
            TEST_SERVICE_AUTH_SECRET,
            algorithm="HS256",
        )
        resp = auth_client.get("/api/jobs", headers={"X-Selfai-Ticket": expired})
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_wrong_audience_is_rejected(self, auth_client):
        ticket = mint_test_ticket(scope="jobs:read", audience="self.language-eval")
        resp = auth_client.get("/api/jobs", headers={"X-Selfai-Ticket": ticket})
        assert resp.status_code == 401
        assert "audience" in resp.json()["detail"].lower()

    def test_wrong_signing_secret_is_rejected(self, auth_client):
        ticket = mint_test_ticket(secret="not-the-real-secret")
        resp = auth_client.get("/api/jobs", headers={"X-Selfai-Ticket": ticket})
        assert resp.status_code == 401
        assert "invalid" in resp.json()["detail"].lower()


class TestScopeEnforcement:
    def test_correct_scope_is_accepted(self, auth_client):
        ticket = mint_test_ticket(scope="jobs:read")
        resp = auth_client.get("/api/jobs", headers={"X-Selfai-Ticket": ticket})
        assert resp.status_code == 200

    def test_missing_scope_is_rejected_with_403(self, auth_client):
        # Valid ticket, correct audience, but wrong/insufficient scope.
        ticket = mint_test_ticket(scope="tasks:read")
        resp = auth_client.get("/api/jobs", headers={"X-Selfai-Ticket": ticket})
        assert resp.status_code == 403
        assert "jobs:read" in resp.json()["detail"]

    def test_one_of_several_scopes_is_sufficient(self, auth_client):
        ticket = mint_test_ticket(scope="tasks:read jobs:read jobs:create")
        resp = auth_client.get("/api/jobs", headers={"X-Selfai-Ticket": ticket})
        assert resp.status_code == 200

    def test_read_scope_does_not_grant_write(self, auth_client):
        """A jobs:read-scoped ticket must not be usable against a
        jobs:write endpoint — scopes are per-capability, not hierarchical."""
        ticket = mint_test_ticket(scope="jobs:read")
        resp = auth_client.delete(
            "/api/jobs/does-not-matter",
            headers={"X-Selfai-Ticket": ticket},
        )
        assert resp.status_code == 403

    def test_read_scope_does_not_grant_create(self, auth_client):
        ticket = mint_test_ticket(scope="jobs:read")
        resp = auth_client.post(
            "/api/jobs",
            json={
                "tasks": "humaneval",
                "api_endpoint": "http://selfai-api:80/api/completions",
                "model": "test-model",
            },
            headers={"X-Selfai-Ticket": ticket},
        )
        assert resp.status_code == 403


class TestNoSecretConfigured:
    def test_missing_server_secret_fails_closed_with_503(self, temp_workspace):
        """If SERVICE_AUTH_SECRET isn't set on this node at all, every ticket
        check must fail closed (503), never silently accept the request."""
        import auth as auth_module
        from main import app

        original_secret = auth_module.SERVICE_AUTH_SECRET
        auth_module.SERVICE_AUTH_SECRET = ""
        try:
            c = TestClient(app)
            ticket = mint_test_ticket()
            resp = c.get("/api/jobs", headers={"X-Selfai-Ticket": ticket})
            assert resp.status_code == 503
        finally:
            auth_module.SERVICE_AUTH_SECRET = original_secret


class TestHealthEndpointStaysOpen:
    """Probes hit this with no ticket; it must never require one."""

    def test_health_requires_no_ticket(self, auth_client):
        resp = auth_client.get("/health")
        assert resp.status_code == 200


class TestGatedAcrossEndpointGroups:
    """Spot-check that each scope group (tasks/jobs) actually enforces
    auth end-to-end, not just the one endpoint used above."""

    def test_tasks_list_requires_ticket(self, auth_client):
        resp = auth_client.get("/api/tasks")
        assert resp.status_code == 401

    def test_jobs_create_requires_ticket(self, auth_client):
        resp = auth_client.post(
            "/api/jobs",
            json={
                "tasks": "humaneval",
                "api_endpoint": "http://selfai-api:80/api/completions",
                "model": "test-model",
            },
        )
        assert resp.status_code == 401

    def test_jobs_create_with_correct_scope_passes_auth_layer(self, auth_client):
        """Scope check passes; a 4xx below (if any) comes from business-logic
        validation, proving auth let the request through rather than
        blocking it."""
        ticket = mint_test_ticket(scope="jobs:create")
        resp = auth_client.post(
            "/api/jobs",
            json={
                "tasks": "no-such-task-pattern-*",
                "api_endpoint": "http://selfai-api:80/api/completions",
                "model": "test-model",
            },
            headers={"X-Selfai-Ticket": ticket},
        )
        assert resp.status_code != 401
        assert resp.status_code != 403

    def test_results_list_requires_ticket(self, auth_client):
        resp = auth_client.get("/api/results")
        assert resp.status_code == 401

    def test_job_purge_requires_ticket(self, auth_client):
        resp = auth_client.delete("/api/jobs/does-not-matter/purge")
        assert resp.status_code == 401
