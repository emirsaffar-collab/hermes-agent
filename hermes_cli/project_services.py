"""Persistent per-project background service runner.

Services are stored in ``projects.db`` (``project_services`` / ``project_service_runs``) and
executed as detached process groups redirected directly to log files under
``$HERMES_HOME/logs/services/``.

By design this module does NOT use ``ProcessRegistry`` (which kills jobs on chat ``/stop``
and does not survive worker rollover) and does NOT use ``process_identity.spawn_owned()``
(which binds child lifetime to the spawning worker). Services outlive agent turns, ``/stop``,
and supervisor worker rollovers, and are reconciled on read/control via ``(pid, create_time,
boot_start)`` incarnation checks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli.process_identity import (
    _pid_alive_matches,
    _process_create_time,
    _verified_boot_start,
)
from hermes_cli.sqlite_util import write_txn
from hermes_cli.worktree_ops import (
    WORKTREE_PORT_BASE,
    WORKTREE_PORT_PROBE_SLOTS,
    WORKTREE_PORT_SPAN,
    _is_tcp_port_available,
    canonical_worktree_path,
    fnv1a_32,
)
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

SERVICE_LOG_TAIL_DEFAULT = 16 * 1024
SERVICE_LOG_TAIL_MAX = 64 * 1024
SERVICE_STOP_GRACE_SECONDS = 3.0
SERVICE_KEEP_OLD_LOGS = 5
VALID_PORT_MODES = frozenset({"auto", "fixed", "none"})
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def services_log_dir() -> Path:
    """Directory holding persistent service stdout/stderr logs."""
    log_dir = get_hermes_home() / "logs" / "services"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _now() -> int:
    return int(time.time())


def _normalize_worktree_key(worktree_path: str) -> str:
    raw = str(worktree_path or "").strip()
    if not raw or "\0" in raw:
        raise ValueError("worktree_path is required")
    return canonical_worktree_path(raw)


def _validate_cwd_rel(worktree_path: str, cwd_rel: str) -> Path:
    """Resolve *cwd_rel* strictly within *worktree_path* (must exist and be a directory)."""
    wt_raw = str(worktree_path or "").strip()
    if not wt_raw or "\0" in wt_raw:
        raise ValueError("worktree_path is required")
    wt_root = Path(os.path.expanduser(wt_raw)).resolve()
    if not wt_root.is_dir():
        raise ValueError(f"worktree directory does not exist: {wt_raw}")

    rel = str(cwd_rel or "").strip()
    if not rel:
        return wt_root
    if "\0" in rel or rel.startswith(("/", "\\", "~")) or _WINDOWS_DRIVE_RE.match(rel):
        raise ValueError(f"cwd_rel must be a relative path inside the worktree: {cwd_rel!r}")
    norm_rel = rel.replace("\\", "/")
    parts = [seg for seg in norm_rel.split("/") if seg]
    if ".." in parts:
        raise ValueError(f"cwd_rel must not traverse outside the worktree: {cwd_rel!r}")

    target = (wt_root / norm_rel).resolve()
    try:
        target.relative_to(wt_root)
    except ValueError as exc:
        raise ValueError(f"cwd_rel escapes worktree root: {cwd_rel!r}") from exc
    if not target.is_dir():
        raise ValueError(f"service working directory does not exist: {target}")
    return target


def _validate_service_fields(
    *,
    project_key: str,
    name: str,
    command: str,
    cwd_rel: str = "",
    port_mode: str = "auto",
    fixed_port: Optional[int] = None,
    env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    pk = str(project_key or "").strip()
    if not pk or "\0" in pk:
        raise ValueError("project_key is required")
    nm = str(name or "").strip()
    if not nm or len(nm) > 120:
        raise ValueError("service name must be 1-120 characters")
    cmd = str(command or "").strip()
    if not cmd or "\0" in cmd:
        raise ValueError("service command is required")
    rel = str(cwd_rel or "").strip()
    if "\0" in rel or rel.startswith(("/", "\\", "~")) or _WINDOWS_DRIVE_RE.match(rel):
        raise ValueError("cwd_rel must be relative to the worktree root")
    if ".." in [seg for seg in rel.replace("\\", "/").split("/") if seg]:
        raise ValueError("cwd_rel must not contain '..'")

    mode = str(port_mode or "auto").strip().lower()
    if mode not in VALID_PORT_MODES:
        raise ValueError(f"port_mode must be one of {sorted(VALID_PORT_MODES)}")

    parsed_port: Optional[int] = None
    if mode == "fixed":
        if fixed_port is None or isinstance(fixed_port, bool):
            raise ValueError("fixed_port is required when port_mode='fixed'")
        try:
            parsed_port = int(fixed_port)
        except (TypeError, ValueError) as exc:
            raise ValueError("fixed_port must be an integer in 1..65535") from exc
        if not (1 <= parsed_port <= 65535):
            raise ValueError("fixed_port must be in 1..65535")

    clean_env: dict[str, str] = {}
    if env is not None:
        if not isinstance(env, dict):
            raise ValueError("env must be a string-to-string mapping")
        for k, v in env.items():
            ks, vs = str(k).strip(), str(v)
            if not ks or "\0" in ks or "=" in ks or "\0" in vs:
                raise ValueError(f"invalid environment variable entry: {k!r}")
            clean_env[ks] = vs

    return {
        "project_key": pk,
        "name": nm,
        "command": cmd,
        "cwd_rel": rel,
        "port_mode": mode,
        "fixed_port": parsed_port,
        "env_json": json.dumps(clean_env, sort_keys=True),
    }


def _preferred_service_port(canonical_wt: str, service_id: str) -> int:
    key = f"{canonical_wt}::{service_id}"
    return WORKTREE_PORT_BASE + (fnv1a_32(key) % WORKTREE_PORT_SPAN)


def _allocate_service_port_in_txn(
    conn: sqlite3.Connection,
    canonical_wt: str,
    service_id: str,
    port_mode: str,
    fixed_port: Optional[int],
    *,
    probe_bind: bool = True,
) -> Optional[int]:
    """Choose a non-colliding port inside an active ``BEGIN IMMEDIATE`` transaction.

    Never writes to ``<gitdir>/hermes-worktree-meta.json`` so multiple services in the
    same worktree get distinct deterministic ports without clobbering worktree metadata.
    """
    if port_mode == "none":
        return None
    if port_mode == "fixed":
        return fixed_port

    reserved = {
        int(row["port"])
        for row in conn.execute(
            "SELECT port FROM project_service_runs WHERE status = 'running' AND port IS NOT NULL"
        ).fetchall()
    }
    base_offset = fnv1a_32(f"{canonical_wt}::{service_id}") % WORKTREE_PORT_SPAN
    preferred = WORKTREE_PORT_BASE + base_offset
    for slot in range(WORKTREE_PORT_PROBE_SLOTS):
        candidate = WORKTREE_PORT_BASE + ((base_offset + slot) % WORKTREE_PORT_SPAN)
        if candidate in reserved:
            continue
        if not probe_bind or _is_tcp_port_available(candidate):
            return candidate
    # Fallback scan beyond probe slots for an unreserved port before giving up.
    for slot in range(WORKTREE_PORT_PROBE_SLOTS, WORKTREE_PORT_SPAN):
        candidate = WORKTREE_PORT_BASE + ((base_offset + slot) % WORKTREE_PORT_SPAN)
        if candidate not in reserved and (not probe_bind or _is_tcp_port_available(candidate)):
            return candidate
    return preferred


def _is_run_alive(row: sqlite3.Row) -> bool:
    pid = row["pid"]
    if not isinstance(pid, int) or pid <= 0:
        return False
    alive = _pid_alive_matches(pid, row["create_time"], boot=row["boot_start"])
    if alive is not None:
        return alive
    # Fallback if psutil is unavailable: POSIX kill(pid, 0) probe.
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reconcile_runs(
    conn: sqlite3.Connection,
    *,
    service_id: Optional[str] = None,
    worktree_path: Optional[str] = None,
) -> int:
    """Verify running rows against live OS incarnations and mark dead ones ``exited``."""
    clauses = ["status = 'running'"]
    params: list[Any] = []
    if service_id:
        clauses.append("service_id = ?")
        params.append(service_id)
    if worktree_path:
        clauses.append("worktree_path = ?")
        params.append(_normalize_worktree_key(worktree_path))
    rows = conn.execute(
        f"SELECT run_id, pid, create_time, boot_start FROM project_service_runs WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    dead_ids = [row["run_id"] for row in rows if not _is_run_alive(row)]
    if not dead_ids:
        return 0
    now = _now()
    with write_txn(conn):
        for run_id in dead_ids:
            conn.execute(
                "UPDATE project_service_runs SET status = 'exited', stopped_at = ? "
                "WHERE run_id = ? AND status = 'running'",
                (now, run_id),
            )
    return len(dead_ids)


def _row_to_run_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "service_id": row["service_id"],
        "worktree_path": row["worktree_path"],
        "pid": row["pid"],
        "pgid": row["pgid"],
        "port": row["port"],
        "log_path": row["log_path"],
        "status": row["status"],
        "started_at": row["started_at"],
        "stopped_at": row["stopped_at"],
    }


def _row_to_service_dict(row: sqlite3.Row, active_run: Optional[sqlite3.Row]) -> dict[str, Any]:
    try:
        env = json.loads(row["env_json"] or "{}")
        if not isinstance(env, dict):
            env = {}
    except Exception:
        env = {}
    run_dict = _row_to_run_dict(active_run) if active_run is not None else None
    return {
        "id": row["id"],
        "project_key": row["project_key"],
        "name": row["name"],
        "command": row["command"],
        "cwd_rel": row["cwd_rel"] or "",
        "port_mode": row["port_mode"],
        "fixed_port": row["fixed_port"],
        "env": env,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status": run_dict["status"] if run_dict else "stopped",
        "port": run_dict["port"] if run_dict else row["fixed_port"],
        "pid": run_dict["pid"] if run_dict and run_dict["status"] == "running" else None,
        "run": run_dict,
    }


def _latest_run_for_service(
    conn: sqlite3.Connection,
    service_id: str,
    worktree_path: Optional[str] = None,
) -> Optional[sqlite3.Row]:
    if worktree_path:
        wt_key = _normalize_worktree_key(worktree_path)
        return conn.execute(
            "SELECT * FROM project_service_runs WHERE service_id = ? AND worktree_path = ? "
            "ORDER BY CASE WHEN status = 'running' THEN 0 ELSE 1 END, started_at DESC, rowid DESC LIMIT 1",
            (service_id, wt_key),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM project_service_runs WHERE service_id = ? "
        "ORDER BY CASE WHEN status = 'running' THEN 0 ELSE 1 END, started_at DESC, rowid DESC LIMIT 1",
        (service_id,),
    ).fetchone()


def list_services(
    conn: sqlite3.Connection,
    project_key: str,
    *,
    worktree_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List configured services for *project_key* with live-reconciled status."""
    pk = str(project_key or "").strip()
    if not pk:
        return []
    reconcile_runs(conn, worktree_path=worktree_path)
    rows = conn.execute(
        "SELECT * FROM project_services WHERE project_key = ? ORDER BY created_at ASC, name ASC",
        (pk,),
    ).fetchall()
    return [_row_to_service_dict(row, _latest_run_for_service(conn, row["id"], worktree_path)) for row in rows]


def list_running_services_for_worktree(
    conn: sqlite3.Connection,
    worktree_path: str,
) -> list[dict[str, Any]]:
    """List every currently running service bound to *worktree_path* (for worktree close guard)."""
    wt_key = _normalize_worktree_key(worktree_path)
    reconcile_runs(conn, worktree_path=wt_key)
    rows = conn.execute(
        "SELECT s.id, s.name, s.project_key, r.run_id, r.pid, r.port "
        "FROM project_service_runs r "
        "JOIN project_services s ON s.id = r.service_id "
        "WHERE r.worktree_path = ? AND r.status = 'running' "
        "ORDER BY r.started_at ASC",
        (wt_key,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "project_key": r["project_key"],
            "run_id": r["run_id"],
            "pid": r["pid"],
            "port": r["port"],
        }
        for r in rows
    ]


def save_service(
    conn: sqlite3.Connection,
    *,
    project_key: str,
    name: str,
    command: str,
    service_id: Optional[str] = None,
    cwd_rel: str = "",
    port_mode: str = "auto",
    fixed_port: Optional[int] = None,
    env: Optional[dict[str, str]] = None,
    worktree_path: Optional[str] = None,
) -> dict[str, Any]:
    """Create or update a persistent service definition in ``project_services``."""
    fields = _validate_service_fields(
        project_key=project_key,
        name=name,
        command=command,
        cwd_rel=cwd_rel,
        port_mode=port_mode,
        fixed_port=fixed_port,
        env=env,
    )
    now = _now()
    sid = str(service_id or "").strip() or f"svc_{secrets.token_hex(8)}"
    with write_txn(conn):
        existing = conn.execute("SELECT id FROM project_services WHERE id = ?", (sid,)).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE project_services SET project_key = ?, name = ?, command = ?, cwd_rel = ?, "
                "port_mode = ?, fixed_port = ?, env_json = ?, updated_at = ? WHERE id = ?",
                (
                    fields["project_key"],
                    fields["name"],
                    fields["command"],
                    fields["cwd_rel"],
                    fields["port_mode"],
                    fields["fixed_port"],
                    fields["env_json"],
                    now,
                    sid,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO project_services "
                "(id, project_key, name, command, cwd_rel, port_mode, fixed_port, env_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    fields["project_key"],
                    fields["name"],
                    fields["command"],
                    fields["cwd_rel"],
                    fields["port_mode"],
                    fields["fixed_port"],
                    fields["env_json"],
                    now,
                    now,
                ),
            )
    row = conn.execute("SELECT * FROM project_services WHERE id = ?", (sid,)).fetchone()
    assert row is not None
    return _row_to_service_dict(row, _latest_run_for_service(conn, sid, worktree_path))


def _signal_run(row: sqlite3.Row, sig: int) -> None:
    """Deliver *sig* to the service process group (falling back to pid) after incarnation verification."""
    pid = row["pid"]
    pgid = row["pgid"]
    if not isinstance(pid, int) or pid <= 0:
        return
    if not _is_run_alive(row):
        return
    if os.name != "nt":
        target_pgid = pgid if isinstance(pgid, int) and pgid > 0 else pid
        try:
            os.killpg(target_pgid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _stop_run_row(conn: sqlite3.Connection, row: sqlite3.Row, *, grace_s: float = SERVICE_STOP_GRACE_SECONDS) -> bool:
    """Stop one running service incarnation cleanly (SIGTERM -> grace -> SIGKILL)."""
    was_alive = _is_run_alive(row)
    if was_alive:
        _signal_run(row, signal.SIGTERM)
        deadline = time.monotonic() + max(0.05, grace_s)
        while time.monotonic() < deadline:
            if not _is_run_alive(row):
                break
            time.sleep(0.05)
        if _is_run_alive(row):
            kill_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
            _signal_run(row, kill_sig)
            kill_deadline = time.monotonic() + 1.0
            while time.monotonic() < kill_deadline and _is_run_alive(row):
                time.sleep(0.05)
    now = _now()
    with write_txn(conn):
        conn.execute(
            "UPDATE project_service_runs SET status = 'stopped', stopped_at = ? WHERE run_id = ?",
            (now, row["run_id"]),
        )
    return was_alive


def stop_service(
    conn: sqlite3.Connection,
    service_id: str,
    *,
    worktree_path: Optional[str] = None,
    grace_s: float = SERVICE_STOP_GRACE_SECONDS,
) -> dict[str, Any]:
    """Stop active run(s) of *service_id* (scoped to *worktree_path* when provided)."""
    svc = conn.execute("SELECT * FROM project_services WHERE id = ?", (service_id,)).fetchone()
    if svc is None:
        raise ValueError(f"no such service: {service_id}")
    reconcile_runs(conn, service_id=service_id, worktree_path=worktree_path)

    clauses = ["service_id = ?", "status = 'running'"]
    params: list[Any] = [service_id]
    if worktree_path:
        clauses.append("worktree_path = ?")
        params.append(_normalize_worktree_key(worktree_path))
    rows = conn.execute(
        f"SELECT * FROM project_service_runs WHERE {' AND '.join(clauses)}",
        params,
    ).fetchall()
    stopped_count = 0
    for row in rows:
        _stop_run_row(conn, row, grace_s=grace_s)
        stopped_count += 1
    return {
        "stopped": stopped_count,
        "service": _row_to_service_dict(svc, _latest_run_for_service(conn, service_id, worktree_path)),
    }


def stop_services_for_worktree(
    conn: sqlite3.Connection,
    worktree_path: str,
    *,
    grace_s: float = SERVICE_STOP_GRACE_SECONDS,
) -> int:
    """Stop every active service bound to *worktree_path* (used before worktree removal)."""
    wt_key = _normalize_worktree_key(worktree_path)
    reconcile_runs(conn, worktree_path=wt_key)
    rows = conn.execute(
        "SELECT * FROM project_service_runs WHERE worktree_path = ? AND status = 'running'",
        (wt_key,),
    ).fetchall()
    for row in rows:
        _stop_run_row(conn, row, grace_s=grace_s)
    return len(rows)


def delete_service(
    conn: sqlite3.Connection,
    service_id: str,
    *,
    grace_s: float = SERVICE_STOP_GRACE_SECONDS,
) -> dict[str, Any]:
    """Stop every active run of *service_id* across all worktrees, then delete its row."""
    svc = conn.execute("SELECT id, project_key FROM project_services WHERE id = ?", (service_id,)).fetchone()
    if svc is None:
        raise ValueError(f"no such service: {service_id}")
    reconcile_runs(conn, service_id=service_id)
    running = conn.execute(
        "SELECT * FROM project_service_runs WHERE service_id = ? AND status = 'running'",
        (service_id,),
    ).fetchall()
    for row in running:
        _stop_run_row(conn, row, grace_s=grace_s)
    # Collect old log paths so we can prune them cleanly after deleting rows.
    log_paths = [
        r["log_path"]
        for r in conn.execute(
            "SELECT log_path FROM project_service_runs WHERE service_id = ?", (service_id,)
        ).fetchall()
        if r["log_path"]
    ]
    with write_txn(conn):
        conn.execute("DELETE FROM project_services WHERE id = ?", (service_id,))
    for lp in log_paths:
        try:
            Path(lp).unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": service_id, "project_key": svc["project_key"], "stopped": len(running)}


def _prune_old_service_logs(conn: sqlite3.Connection, service_id: str) -> None:
    """Keep only the newest ``SERVICE_KEEP_OLD_LOGS`` stopped/exited log files per service."""
    old_rows = conn.execute(
        "SELECT run_id, log_path FROM project_service_runs "
        "WHERE service_id = ? AND status != 'running' "
        "ORDER BY started_at DESC, rowid DESC LIMIT -1 OFFSET ?",
        (service_id, SERVICE_KEEP_OLD_LOGS),
    ).fetchall()
    if not old_rows:
        return
    for row in old_rows:
        try:
            if row["log_path"]:
                Path(row["log_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    with write_txn(conn):
        for row in old_rows:
            conn.execute("DELETE FROM project_service_runs WHERE run_id = ?", (row["run_id"],))


def start_service(
    conn: sqlite3.Connection,
    service_id: str,
    worktree_path: str,
    *,
    probe_bind: bool = True,
) -> dict[str, Any]:
    """Start *service_id* in *worktree_path* as an independent detached process group.

    Idempotent when the service is already live for *(service_id, worktree_path)*.
    """
    svc = conn.execute("SELECT * FROM project_services WHERE id = ?", (service_id,)).fetchone()
    if svc is None:
        raise ValueError(f"no such service: {service_id}")

    wt_key = _normalize_worktree_key(worktree_path)
    resolved_cwd = _validate_cwd_rel(worktree_path, svc["cwd_rel"] or "")
    reconcile_runs(conn, service_id=service_id, worktree_path=wt_key)

    existing_live = conn.execute(
        "SELECT * FROM project_service_runs WHERE service_id = ? AND worktree_path = ? AND status = 'running' "
        "ORDER BY started_at DESC LIMIT 1",
        (service_id, wt_key),
    ).fetchone()
    if existing_live is not None and _is_run_alive(existing_live):
        return _row_to_service_dict(svc, existing_live)

    run_id = f"run_{secrets.token_hex(8)}"
    log_path = services_log_dir() / f"{run_id}.log"
    now = _now()

    # Reserve port + run slot atomically inside BEGIN IMMEDIATE before spawning.
    with write_txn(conn):
        port = _allocate_service_port_in_txn(
            conn,
            wt_key,
            service_id,
            svc["port_mode"],
            svc["fixed_port"],
            probe_bind=probe_bind,
        )
        conn.execute(
            "INSERT INTO project_service_runs "
            "(run_id, service_id, worktree_path, pid, pgid, create_time, boot_start, port, log_path, status, started_at, stopped_at) "
            "VALUES (?, ?, ?, 0, NULL, NULL, NULL, ?, ?, 'running', ?, NULL)",
            (run_id, service_id, wt_key, port, str(log_path), now),
        )

    env = dict(os.environ)
    try:
        custom_env = json.loads(svc["env_json"] or "{}")
        if isinstance(custom_env, dict):
            for k, v in custom_env.items():
                env[str(k)] = str(v)
    except Exception:
        pass
    if port is not None:
        env["PORT"] = str(port)
        env["HERMES_SERVICE_PORT"] = str(port)

    argv = ["cmd", "/c", svc["command"]] if os.name == "nt" else ["/bin/sh", "-c", svc["command"]]
    popen_kwargs: dict[str, Any] = {
        "cwd": str(resolved_cwd),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        log_fh = open(log_path, "ab")
        try:
            if os.name != "nt":
                try:
                    os.chmod(log_path, 0o600)
                except OSError:
                    pass
            proc = subprocess.Popen(argv, stdout=log_fh, stderr=subprocess.STDOUT, **popen_kwargs)
        finally:
            log_fh.close()
    except Exception as exc:
        with write_txn(conn):
            conn.execute(
                "UPDATE project_service_runs SET status = 'exited', stopped_at = ? WHERE run_id = ?",
                (_now(), run_id),
            )
        raise RuntimeError(f"failed to start service {svc['name']!r}: {exc}") from exc

    pid = int(proc.pid)
    pgid: Optional[int] = None
    if os.name != "nt":
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid
    create_time = _process_create_time(pid)
    b_start = _verified_boot_start(pid, create_time)

    with write_txn(conn):
        conn.execute(
            "UPDATE project_service_runs SET pid = ?, pgid = ?, create_time = ?, boot_start = ? WHERE run_id = ?",
            (pid, pgid, create_time, b_start, run_id),
        )
    _prune_old_service_logs(conn, service_id)
    run_row = conn.execute("SELECT * FROM project_service_runs WHERE run_id = ?", (run_id,)).fetchone()
    return _row_to_service_dict(svc, run_row)


def restart_service(
    conn: sqlite3.Connection,
    service_id: str,
    worktree_path: str,
    *,
    grace_s: float = SERVICE_STOP_GRACE_SECONDS,
    probe_bind: bool = True,
) -> dict[str, Any]:
    """Stop any running instance for *(service_id, worktree_path)* and start a fresh one."""
    stop_service(conn, service_id, worktree_path=worktree_path, grace_s=grace_s)
    return start_service(conn, service_id, worktree_path, probe_bind=probe_bind)


def read_service_logs(
    conn: sqlite3.Connection,
    service_id: str,
    *,
    worktree_path: Optional[str] = None,
    tail_bytes: int = SERVICE_LOG_TAIL_DEFAULT,
) -> dict[str, Any]:
    """Read a bounded UTF-8 tail (<= 64 KiB) from the service's latest run log file."""
    svc = conn.execute("SELECT id FROM project_services WHERE id = ?", (service_id,)).fetchone()
    if svc is None:
        raise ValueError(f"no such service: {service_id}")
    reconcile_runs(conn, service_id=service_id, worktree_path=worktree_path)
    run_row = _latest_run_for_service(conn, service_id, worktree_path)
    if run_row is None:
        return {"service_id": service_id, "run_id": None, "status": "stopped", "content": "", "truncated": False, "byte_size": 0}

    cap = max(256, min(int(tail_bytes or SERVICE_LOG_TAIL_DEFAULT), SERVICE_LOG_TAIL_MAX))
    log_file = Path(run_row["log_path"])
    if not log_file.is_file():
        return {
            "service_id": service_id,
            "run_id": run_row["run_id"],
            "status": run_row["status"],
            "content": "",
            "truncated": False,
            "byte_size": 0,
        }
    size = log_file.stat().st_size
    truncated = size > cap
    with open(log_file, "rb") as fh:
        if truncated:
            fh.seek(size - cap)
        raw = fh.read(cap)
    return {
        "service_id": service_id,
        "run_id": run_row["run_id"],
        "status": run_row["status"],
        "content": raw.decode("utf-8", errors="replace"),
        "truncated": truncated,
        "byte_size": size,
    }
