"""Tests for persistent Project Services (hermes_cli.project_services + projects.services.* RPCs)."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import project_services as psvc
from hermes_cli import projects_db as pdb
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


def _cleanup_pgid(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pid, signal.SIGKILL)


@pytest.fixture
def isolated_home(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


def _wait_for_log(conn, service_id: str, worktree: str, needle: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        last = psvc.read_service_logs(conn, service_id, worktree_path=worktree)
        if needle in last.get("content", ""):
            return last
        time.sleep(0.05)
    return last


def test_tier2_project_key_works_with_foreign_keys_enabled(isolated_home, tmp_path):
    """Tier-2/Tier-3 project keys (not rows in `projects`) must save without FK violation."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    with pdb.connect_closing() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        svc = psvc.save_service(
            conn,
            project_key="repo:/discovered/not-in-projects-table",
            name="Web",
            command="echo ok",
            worktree_path=str(worktree),
        )
        assert svc["project_key"] == "repo:/discovered/not-in-projects-table"
        assert svc["port_mode"] == "auto"
        listed = psvc.list_services(conn, "repo:/discovered/not-in-projects-table", worktree_path=str(worktree))
        assert len(listed) == 1
        assert listed[0]["id"] == svc["id"]


def test_save_validation_rejects_escape_and_missing_fixed_port(isolated_home):
    with pdb.connect_closing() as conn:
        with pytest.raises(ValueError, match="cwd_rel") as exc1:
            psvc.save_service(conn, project_key="p1", name="Bad", command="echo 1", cwd_rel="../outside")
        assert ".." in str(exc1.value)
        with pytest.raises(ValueError, match="cwd_rel") as exc2:
            psvc.save_service(conn, project_key="p1", name="Bad2", command="echo 1", cwd_rel="/etc")
        assert "relative" in str(exc2.value)
        with pytest.raises(ValueError, match="fixed_port") as exc3:
            psvc.save_service(conn, project_key="p1", name="Bad3", command="echo 1", port_mode="fixed")
        assert "fixed_port" in str(exc3.value)


def test_service_lifecycle_detached_logs_port_reconcile_and_stop(isolated_home, tmp_path):
    worktree = tmp_path / "wt-alpha"
    worktree.mkdir()
    cmd = f"{sys.executable} -u -c 'import os, time; print(\"READY_PORT=\" + os.environ.get(\"PORT\", \"\"), flush=True); time.sleep(60)'"

    with pdb.connect_closing() as conn:
        svc = psvc.save_service(
            conn,
            project_key="proj-lifecycle",
            name="Dev Server",
            command=cmd,
            port_mode="auto",
            worktree_path=str(worktree),
        )
        started = psvc.start_service(conn, svc["id"], str(worktree))
        pid = started["pid"]
        port = started["port"]
        try:
            assert started["status"] == "running"
            assert isinstance(pid, int) and pid > 0
            assert isinstance(port, int) and 24000 <= port < 25000
            # Detached process group: pgid == pid
            assert os.getpgid(pid) == pid
            # Log file resides under $HERMES_HOME/logs/services/
            log_path = Path(started["run"]["log_path"])
            assert log_path.parent == isolated_home / "logs" / "services"
            logs = _wait_for_log(conn, svc["id"], str(worktree), f"READY_PORT={port}")
            assert f"READY_PORT={port}" in logs["content"]
            assert logs["truncated"] is False

            # Running summary for worktree includes this service
            running = psvc.list_running_services_for_worktree(conn, str(worktree))
            assert len(running) == 1
            assert running[0]["id"] == svc["id"]
            assert running[0]["port"] == port

            # Restart yields a new PID and terminates the old one
            restarted = psvc.restart_service(conn, svc["id"], str(worktree))
            new_pid = restarted["pid"]
            assert restarted["status"] == "running"
            assert new_pid != pid
            pid = new_pid

            # Stop by worktree stops the service cleanly
            stopped_count = psvc.stop_services_for_worktree(conn, str(worktree))
            assert stopped_count == 1
            after = psvc.list_services(conn, "proj-lifecycle", worktree_path=str(worktree))
            assert after[0]["status"] == "stopped"
            assert psvc.list_running_services_for_worktree(conn, str(worktree)) == []
        finally:
            _cleanup_pgid(pid)


def test_external_death_reconciles_to_exited_and_log_tail_is_capped(isolated_home, tmp_path):
    worktree = tmp_path / "wt-beta"
    worktree.mkdir()
    cmd = f"{sys.executable} -u -c 'import time; print(\"X\" * 80000, flush=True); time.sleep(60)'"

    with pdb.connect_closing() as conn:
        svc = psvc.save_service(
            conn,
            project_key="proj-reconcile",
            name="Noisy",
            command=cmd,
            port_mode="none",
            worktree_path=str(worktree),
        )
        started = psvc.start_service(conn, svc["id"], str(worktree))
        pid = started["pid"]
        try:
            logs = _wait_for_log(conn, svc["id"], str(worktree), "XXXX")
            assert logs["truncated"] is True
            assert len(logs["content"].encode("utf-8")) <= psvc.SERVICE_LOG_TAIL_DEFAULT

            # External kill -> reconciliation flips status to exited
            os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not psvc._pid_alive_matches(pid, started["run"].get("create_time")):
                    break
                time.sleep(0.05)

            listed = psvc.list_services(conn, "proj-reconcile", worktree_path=str(worktree))
            assert listed[0]["status"] == "exited"
            assert psvc.list_running_services_for_worktree(conn, str(worktree)) == []
        finally:
            _cleanup_pgid(pid)


def test_multi_service_auto_ports_do_not_collide(isolated_home, tmp_path):
    worktree = tmp_path / "wt-multi"
    worktree.mkdir()
    cmd = f"{sys.executable} -u -c 'import time; time.sleep(60)'"
    pids = []

    with pdb.connect_closing() as conn:
        s1 = psvc.save_service(conn, project_key="proj-multi", name="Web", command=cmd)
        s2 = psvc.save_service(conn, project_key="proj-multi", name="API", command=cmd)
        try:
            r1 = psvc.start_service(conn, s1["id"], str(worktree))
            pids.append(r1["pid"])
            r2 = psvc.start_service(conn, s2["id"], str(worktree))
            pids.append(r2["pid"])
            assert r1["port"] != r2["port"]
            assert 24000 <= r1["port"] < 25000
            assert 24000 <= r2["port"] < 25000
        finally:
            psvc.stop_services_for_worktree(conn, str(worktree))
            for pid in pids:
                _cleanup_pgid(pid)


def test_tui_gateway_rpc_registration_and_roundtrip(isolated_home, tmp_path):
    from tui_gateway import server
    from tui_gateway.contracts import METHODS

    expected = [
        "projects.services.list",
        "projects.services.save",
        "projects.services.delete",
        "projects.services.start",
        "projects.services.stop",
        "projects.services.restart",
        "projects.services.logs",
    ]
    for name in expected:
        assert name in server._methods, f"missing handler {name}"
        assert name in server._LONG_HANDLERS, f"missing _LONG_HANDLERS entry {name}"
        assert name in METHODS, f"missing contract {name}"

    worktree = tmp_path / "wt-rpc"
    worktree.mkdir()

    def call(method: str, params: dict) -> dict:
        resp = server._methods[method](1, params)
        assert "error" not in resp, resp.get("error")
        return resp["result"]

    saved = call(
        "projects.services.save",
        {
            "project_key": "proj-rpc",
            "name": "Echo",
            "command": f"{sys.executable} -u -c 'import time; print(\"RPC_OK\", flush=True); time.sleep(60)'",
            "port_mode": "auto",
            "worktree_path": str(worktree),
        },
    )["service"]
    try:
        started = call(
            "projects.services.start",
            {"service_id": saved["id"], "worktree_path": str(worktree)},
        )["service"]
        assert started["status"] == "running"

        listed = call(
            "projects.services.list",
            {"project_key": "proj-rpc", "worktree_path": str(worktree)},
        )
        assert len(listed["services"]) == 1
        assert len(listed["running_for_worktree"]) == 1

        stopped = call(
            "projects.services.stop",
            {"service_id": saved["id"], "worktree_path": str(worktree)},
        )
        assert stopped["stopped"] == 1

        deleted = call("projects.services.delete", {"service_id": saved["id"]})
        assert deleted["deleted"] == saved["id"]
    finally:
        call("projects.services.stop", {"worktree_path": str(worktree)})
