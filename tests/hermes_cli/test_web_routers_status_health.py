"""``GET /api/health`` ``commit`` contract (review G4).

The public health probe reports the backend's BOOT ``commit`` so an attaching client can refuse a
backend whose boot commit differs from its checkout: a ``serve`` that outlived ``hermes update``
is never re-adopted. The field must always be present
(``null`` off git) and must come from ``get_version_info().commit``.
"""

from __future__ import annotations

import pytest

from hermes_cli.version_info import VersionInfo


def _info(commit: str | None) -> VersionInfo:
    return VersionInfo(base_version="2026.10.1", derived_version="2026.10.1", distance=0,
                       commit=commit, branch="main", source="git")


@pytest.mark.parametrize("commit", ["0fbe50a9d88aa7e1c0b5f3d2e4a6b8c9d0e1f2a3", None])
def test_health_reports_the_boot_commit_from_version_info(monkeypatch, commit):
    from starlette.testclient import TestClient

    from hermes_cli.web_routers import status
    import hermes_cli.web_server as ws

    monkeypatch.setattr(status, "get_version_info", lambda: _info(commit))

    response = TestClient(ws.app).get("/api/health")  # public: no session token

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "commit" in body, "attaching clients key the boot-identity decision on this field"
    assert body["commit"] == commit


def test_health_boot_commit_survives_checkout_sha_and_refresh_reads(monkeypatch):
    """read_checkout_sha() and get_code_identity(refresh=True) must never poison GET /api/health."""
    from starlette.testclient import TestClient

    from hermes_cli import version_info as vi
    import hermes_cli.web_server as ws

    boot_sha = "001cf05f46000000000000000000000000000001"
    moved_sha = "cfc7c9919c000000000000000000000000000002"
    current = {"sha": boot_sha}

    vi._reset_version_info_cache()
    monkeypatch.setattr(vi, "_compute_version_info", lambda: _info(current["sha"]))
    monkeypatch.setattr(vi, "installed_commit_sha", lambda: current["sha"])
    try:
        client = TestClient(ws.app)
        assert client.get("/api/health").json()["commit"] == boot_sha

        # Simulate git pull / install-stamp moving HEAD on disk under the running server.
        current["sha"] = moved_sha
        assert vi.read_checkout_sha() == moved_sha
        assert vi.get_code_identity(refresh=True)["sha"] == moved_sha
        # Process-boot /api/health MUST still report boot_sha, not moved_sha!
        assert client.get("/api/health").json()["commit"] == boot_sha
    finally:
        vi._reset_version_info_cache()
