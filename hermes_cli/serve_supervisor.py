"""Zero-downtime Blue-Green Front-Door Supervisor for ``hermes serve``.

Architecture
------------
Hermes Desktop (Electron) spawns one ``hermes serve --isolated --host 127.0.0.1 --port 0``
process and connects a persistent WebSocket to ``/api/ws`` plus HTTP calls and secondary
WebSockets (``/api/pty``, ``/api/logs``, etc.). Because Electron does not support in-band
code reloads without tearing down active streams, long-running multi-session workloads
historically starved single-process recycling indefinitely.

This module acts as a tiny (~25 MB RSS, <150 ms startup, zero ``agent.*`` / ``tui_gateway.*``
imports) Front-Door Supervisor:

1. Owns the stable client-facing TCP port and prints the exact ``HERMES_BACKEND_READY`` /
   ``HERMES_DASHBOARD_READY`` stdout sentinels expected by Desktop.
2. Spawns immutable worker generations (``worker-gen-1``, ``worker-gen-2``, ...) on
   internal loopback ephemeral ports via ``--worker-gen N --worker-token-fd <fd>``.
3. Multiplexes ``/api/ws`` JSON-RPC 2.0 frames across worker generations:
   - In-flight turns stay pinned to their originating worker generation until completion.
   - Server-initiated requests (``srq-*`` approvals / clarifications) route back strictly
     to the originating worker generation.
   - Idle sessions and brand-new turns hand off transparently to the newest healthy
     worker generation on the very next turn, without dropping Electron's WebSocket.
4. Proxies all HTTP routes (including SSE streaming) and secondary WebSockets.
5. Exposes ``POST /api/supervisor/rollover`` (authenticated) + ``SIGHUP`` to spawn a
   fresh worker generation on disk HEAD, health-verify it, atomically flip new traffic,
   and SIGTERM draining workers as soon as their active turns finish.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

_log = logging.getLogger("hermes.serve_supervisor")

_READY_PORT_RE = re.compile(r"HERMES_(?:BACKEND|DASHBOARD)_READY\s+port=(\d+)")
_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
})

# JSON-RPC methods that start or mutate an active turn on a specific session.
_TURN_START_METHODS = frozenset({
    "prompt.send",
    "prompt.submit",
    "prompt.retry",
    "session.prompt",
    "session.send",
    "sessions.prompt",
    "sessions.submit_turn",
    "sessions.send",
    "sessions.retry",
    "sessions.edit_resend",
    "sessions.resume_turn",
})

# Session-scoped methods that must hit the worker running an in-flight turn if active,
# otherwise they hand off cleanly to the active (newest) worker generation.
_SESSION_SCOPED_PREFIXES = (
    "session.",
    "sessions.",
    "prompt.",
    "message.",
    "turns.",
    "turn.",
    "approvals.",
    "approval.",
    "clarify.",
    "permissions.",
    "subagents.",
)

_GLOBAL_SESSION_METHODS = frozenset({
    "session.list",
    "session.active_list",
    "session.create",
    "session.most_recent",
    "session.events.stats",
    "session.foreign.list",
    "session.foreign.preview",
    "session.foreign.import",
    "sessions.list",
    "sessions.create",
    "sessions.search",
    "sessions.stats",
    "sessions.summary",
    "sessions.recent",
    "sessions.active",
    "sessions.count",
    "sessions.export_all",
})

_TERMINAL_TURN_METHODS = frozenset({
    "message.complete",
    "error",
    "sessions.turn_complete",
    "sessions.turn_done",
    "sessions.turn_ended",
    "sessions.turn_error",
    "sessions.turn_cancelled",
    "sessions.turn_aborted",
    "sessions.idle",
    "turn.complete",
    "turn.done",
    "turn.ended",
    "turn.error",
    "turn.cancelled",
})

_TERMINAL_EVENT_TYPES = frozenset({
    "turn_complete",
    "turn_done",
    "turn_ended",
    "turn_error",
    "turn_cancelled",
    "turn_aborted",
    "done",
    "idle",
    "completed",
    "cancelled",
    "aborted",
})

_ACTIVE_TURN_METHODS = frozenset({
    "message.start",
    "sessions.turn_started",
    "sessions.turn_running",
    "turn.started",
    "turn.running",
})

_SESSION_PATH_RE = re.compile(r"^/api/sessions/([^/]+)(?:/|$)")


def _extract_session_id(params: Any) -> Optional[str]:
    """Extract session ID from JSON-RPC params when present."""
    if not isinstance(params, dict):
        return None
    for key in ("session_id", "sessionId", "sid", "session"):
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Some session.* RPCs use `id` for session id
    val = params.get("id")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _is_session_scoped_method(method: str) -> bool:
    if not method or method in _GLOBAL_SESSION_METHODS:
        return False
    return any(method.startswith(pfx) for pfx in _SESSION_SCOPED_PREFIXES)


def _is_terminal_notification(method: str, params: Any) -> bool:
    if method in _TERMINAL_TURN_METHODS:
        return True
    if method == "session.info" and isinstance(params, dict) and params.get("running") is False:
        return True
    if isinstance(params, dict):
        ev = str(params.get("type") or params.get("event") or params.get("status") or "").lower()
        if ev in _TERMINAL_EVENT_TYPES:
            return True
    return False


def _is_turn_start_notification(method: str, params: Any) -> bool:
    if method in _ACTIVE_TURN_METHODS:
        return True
    if method == "session.info" and isinstance(params, dict) and params.get("running") is True:
        return True
    if isinstance(params, dict):
        ev = str(params.get("type") or params.get("event") or params.get("status") or "").lower()
        if ev in ("turn_started", "running", "streaming", "in_progress"):
            return True
    return False


def _write_machine_sentinel_line(line: str) -> None:
    """Write READY sentinel directly to fd 1 so Desktop parsers always see it."""
    payload = (line + "\n").encode("utf-8")
    try:
        os.write(1, payload)
    except OSError:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def _read_disk_head_sha() -> str:
    """Best-effort short commit SHA of the checkout on disk."""
    try:
        repo = Path(__file__).resolve().parent.parent
        res = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short=10", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception:
        pass
    return ""


@dataclass
class WorkerInstance:
    """A single immutable worker generation spawned by the supervisor."""

    gen: int
    pid: int
    port: int
    proc: subprocess.Popen
    started_at: float = field(default_factory=time.time)
    commit: str = ""
    draining: bool = False
    draining_since: Optional[float] = None
    retired: bool = False
    stderr_lines: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_base_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def to_status_dict(self, active_sessions: list[str]) -> dict[str, Any]:
        now = time.time()
        return {
            "gen": self.gen,
            "pid": self.pid,
            "port": self.port,
            "alive": self.is_alive(),
            "commit": self.commit,
            "draining": self.draining,
            "retired": self.retired,
            "age_s": round(now - self.started_at, 1),
            "draining_age_s": round(now - self.draining_since, 1) if self.draining_since else None,
            "active_sessions": sorted(active_sessions),
            "active_turns": len(active_sessions),
        }


class ClientWsMux:
    """Per-client ``/api/ws`` JSON-RPC 2.0 multiplexer across worker generations."""

    def __init__(
        self,
        supervisor: ServeSupervisor,
        client_ws: WebSocket,
        query_string: str,
        extra_headers: dict[str, str],
    ) -> None:
        self.supervisor = supervisor
        self.client_ws = client_ws
        self.query_string = query_string
        self.extra_headers = extra_headers
        self._send_lock = asyncio.Lock()
        self._closed = False

        # gen -> websockets client connection
        self._upstreams: dict[int, Any] = {}
        self._reader_tasks: dict[int, asyncio.Task] = {}
        self._connect_locks: dict[int, asyncio.Lock] = {}

        # Active turn pins: session_id -> worker gen
        self.active_turn_pins: dict[str, int] = {}
        # Server-initiated request routing: srq_id -> worker gen
        self.pending_srq: dict[Any, int] = {}
        # Pending client->worker request IDs: req_id -> worker gen
        self.pending_reqs: dict[Any, int] = {}
        # Cached initial hello frame to replay transparently to newly rolled-over workers
        self._hello_frame: Optional[dict[str, Any]] = None
        self._synthetic_hello_ids: set[str] = set()

    def active_sessions_for_gen(self, gen: int) -> set[str]:
        return {sid for sid, g in self.active_turn_pins.items() if g == gen}

    def has_inflight_work_for_gen(self, gen: int) -> bool:
        if any(g == gen for g in self.active_turn_pins.values()):
            return True
        if any(g == gen for g in self.pending_srq.values()):
            return True
        if any(g == gen for g in self.pending_reqs.values()):
            return True
        return False

    async def send_to_client(self, text: str) -> None:
        if self._closed:
            return
        async with self._send_lock:
            if not self._closed:
                await self.client_ws.send_text(text)

    async def ensure_upstream(self, worker: WorkerInstance) -> Any:
        existing = self._upstreams.get(worker.gen)
        if existing is not None:
            return existing
        lock = self._connect_locks.setdefault(worker.gen, asyncio.Lock())
        async with lock:
            existing = self._upstreams.get(worker.gen)
            if existing is not None:
                return existing
            url = f"{worker.ws_base_url}/api/ws"
            if self.query_string:
                url = f"{url}?{self.query_string}"
            headers = dict(self.extra_headers)
            if self.supervisor.session_token and "x-hermes-session-token" not in {
                k.lower() for k in headers
            }:
                headers["X-Hermes-Session-Token"] = self.supervisor.session_token
            ws = await websockets.connect(
                url,
                additional_headers=headers,
                max_size=64 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            )
            self._upstreams[worker.gen] = ws
            self._reader_tasks[worker.gen] = asyncio.create_task(
                self._pump_upstream(worker, ws),
                name=f"ws-mux-upstream-gen-{worker.gen}",
            )
            # Replay client hello to newly opened worker after rollover so it has
            # the client's protocol version / profile capabilities.
            if self._hello_frame is not None and worker.gen != self.supervisor.initial_gen:
                syn_id = f"__sup_hello_{worker.gen}_{secrets.token_hex(4)}"
                self._synthetic_hello_ids.add(syn_id)
                replay = dict(self._hello_frame)
                replay["id"] = syn_id
                await ws.send(json.dumps(replay))
            return ws

    async def detach_worker(self, gen: int) -> None:
        ws = self._upstreams.pop(gen, None)
        task = self._reader_tasks.pop(gen, None)
        if task is not None and not task.done():
            task.cancel()
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        # Clean up any leftover pins referencing this departed worker
        for sid in [s for s, g in self.active_turn_pins.items() if g == gen]:
            self.active_turn_pins.pop(sid, None)
        for rid in [r for r, g in self.pending_srq.items() if g == gen]:
            self.pending_srq.pop(rid, None)
        for rid in [r for r, g in self.pending_reqs.items() if g == gen]:
            self.pending_reqs.pop(rid, None)

    async def _pump_upstream(self, worker: WorkerInstance, ws: Any) -> None:
        try:
            async for raw in ws:
                if self._closed:
                    break
                if isinstance(raw, bytes):
                    async with self._send_lock:
                        if not self._closed:
                            await self.client_ws.send_bytes(raw)
                    continue
                await self._handle_upstream_text(worker, raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.debug("Upstream WS gen=%s closed: %s", worker.gen, exc)
        finally:
            self._upstreams.pop(worker.gen, None)
            # If the ACTIVE worker died unexpectedly and we have no replacement,
            # close client WS cleanly so Electron reconnects or triggers recovery.
            if (
                not self._closed
                and self.supervisor.active_worker is worker
                and not worker.draining
                and not worker.is_alive()
            ):
                _log.warning("Active worker gen=%s disconnected unexpectedly", worker.gen)
                self._closed = True
                try:
                    await self.client_ws.close(code=1011, reason="active worker disconnected")
                except Exception:
                    pass

    async def _handle_upstream_text(self, worker: WorkerInstance, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            await self.send_to_client(raw)
            return

        if not isinstance(msg, dict):
            await self.send_to_client(raw)
            return

        has_method = "method" in msg and isinstance(msg.get("method"), str)
        has_id = "id" in msg and msg.get("id") is not None

        # 1. Server-initiated request (e.g. srq-123 approval / clarify prompt)
        if has_method and has_id:
            self.pending_srq[msg["id"]] = worker.gen
            await self.send_to_client(raw)
            return

        # 2. Response to a client request (or synthetic hello replay)
        if has_id and not has_method:
            rid = msg["id"]
            if isinstance(rid, str) and rid in self._synthetic_hello_ids:
                self._synthetic_hello_ids.discard(rid)
                return
            self.pending_reqs.pop(rid, None)
            # If a turn-start RPC returned an immediate JSON-RPC error, release turn pin
            if "error" in msg:
                # Best-effort: error payload may include sessionId
                err_data = msg.get("error", {}).get("data") if isinstance(msg.get("error"), dict) else None
                sid = _extract_session_id(err_data)
                if sid and self.active_turn_pins.get(sid) == worker.gen:
                    self.active_turn_pins.pop(sid, None)
            await self.send_to_client(raw)
            return

        # 3. Server-initiated notification (streaming delta, turn lifecycle, broadcast)
        if has_method and not has_id:
            method = msg["method"]
            params = msg.get("params")
            sid = _extract_session_id(params)
            if sid:
                if _is_terminal_notification(method, params):
                    if self.active_turn_pins.get(sid) == worker.gen:
                        self.active_turn_pins.pop(sid, None)
                elif _is_turn_start_notification(method, params):
                    self.active_turn_pins[sid] = worker.gen
            # Suppress redundant global/unpinned broadcasts from draining workers
            if worker.draining:
                if sid:
                    # Always forward notifications for sessions that ran on this draining worker
                    # (including the terminal notification that just cleared the pin!).
                    await self.send_to_client(raw)
                    return
                # Drop global broadcast ticks from draining workers (active worker emits them)
                return
            await self.send_to_client(raw)
            return

        await self.send_to_client(raw)

    def _choose_worker_for_client_frame(self, msg: dict[str, Any]) -> Optional[WorkerInstance]:
        has_method = "method" in msg and isinstance(msg.get("method"), str)
        has_id = "id" in msg and msg.get("id") is not None

        # 1. Client response to a server-initiated request (srq-* approval / clarify)
        if has_id and not has_method:
            rid = msg["id"]
            origin_gen = self.pending_srq.pop(rid, None)
            if origin_gen is not None:
                w = self.supervisor.get_worker(origin_gen)
                if w is not None and w.is_alive():
                    return w
            return self.supervisor.active_worker

        # 2. Client JSON-RPC request or notification
        method = msg.get("method", "") if has_method else ""
        params = msg.get("params")

        if method == "hello" and has_id:
            self._hello_frame = dict(msg)

        if _is_session_scoped_method(method):
            sid = _extract_session_id(params)
            if sid:
                pinned_gen = self.active_turn_pins.get(sid)
                if pinned_gen is not None:
                    pinned_worker = self.supervisor.get_worker(pinned_gen)
                    if pinned_worker is not None and pinned_worker.is_alive():
                        return pinned_worker
                    self.active_turn_pins.pop(sid, None)
                # Idle session or new turn -> hand off cleanly to active (newest) worker!
                active = self.supervisor.active_worker
                if active is not None and method in _TURN_START_METHODS:
                    self.active_turn_pins[sid] = active.gen
                return active

        return self.supervisor.active_worker

    async def run(self) -> None:
        # Eagerly establish upstream connection to active worker on WS accept
        active = self.supervisor.active_worker
        if active is None or not active.is_alive():
            await self.client_ws.close(code=1013, reason="no active worker")
            return
        try:
            await self.ensure_upstream(active)
        except Exception as exc:
            _log.warning("Failed to connect upstream /api/ws to gen=%s: %s", active.gen, exc)
            await self.client_ws.close(code=1011, reason="upstream connect failed")
            return

        try:
            while not self._closed:
                message = await self.client_ws.receive()
                mtype = message.get("type")
                if mtype == "websocket.disconnect":
                    break
                if "bytes" in message and message["bytes"] is not None:
                    target = self.supervisor.active_worker
                    if target is not None:
                        up = await self.ensure_upstream(target)
                        await up.send(message["bytes"])
                    continue
                raw = message.get("text")
                if raw is None:
                    continue
                await self._handle_client_text(raw)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.debug("Client /api/ws loop ended: %s", exc)
        finally:
            await self.close()

    async def _handle_client_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            target = self.supervisor.active_worker
            if target is not None:
                up = await self.ensure_upstream(target)
                await up.send(raw)
            return

        if not isinstance(msg, dict):
            target = self.supervisor.active_worker
            if target is not None:
                up = await self.ensure_upstream(target)
                await up.send(raw)
            return

        target = self._choose_worker_for_client_frame(msg)
        if target is None or not target.is_alive():
            if "id" in msg and msg["id"] is not None:
                err = {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32002, "message": "No active Hermes worker available"},
                }
                await self.send_to_client(json.dumps(err))
            return

        if "method" in msg and "id" in msg and msg["id"] is not None:
            self.pending_reqs[msg["id"]] = target.gen

        try:
            up = await self.ensure_upstream(target)
            await up.send(raw)
        except Exception as exc:
            _log.warning("Forwarding client frame to gen=%s failed: %s", target.gen, exc)
            if "id" in msg and msg["id"] is not None:
                self.pending_reqs.pop(msg["id"], None)
                err = {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32003, "message": f"Worker gen {target.gen} forward failed: {exc}"},
                }
                await self.send_to_client(json.dumps(err))

    async def close(self) -> None:
        self._closed = True
        for gen in list(self._upstreams.keys()):
            await self.detach_worker(gen)


class ServeSupervisor:
    """Thin Blue-Green Front-Door Supervisor managing immutable worker generations."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        session_token: Optional[str] = None,
        ssh_owner_nonce: Optional[str] = None,
        ssh_lock_path: Optional[Path] = None,
        isolated: bool = True,
        open_profile: str = "",
        worker_cmd_builder: Optional[Callable[[int, int], list[str]]] = None,
        worker_ready_timeout_s: float = 45.0,
        drain_poll_s: float = 1.0,
        drain_settle_s: float = 1.0,
        max_draining_workers: int = 3,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.bound_port: int = port
        # Always hold a single consistent session token across all worker generations.
        self.session_token: str = session_token or secrets.token_hex(32)
        self.ssh_owner_nonce = ssh_owner_nonce
        self.ssh_lock_path = ssh_lock_path
        self.isolated = isolated
        self.open_profile = open_profile
        self.worker_cmd_builder = worker_cmd_builder
        self.worker_ready_timeout_s = worker_ready_timeout_s
        self.drain_poll_s = drain_poll_s
        self.drain_settle_s = drain_settle_s
        self.max_draining_workers = max_draining_workers

        self.started_at = time.time()
        self.initial_gen = 1
        self._next_gen = 1
        self._workers: dict[int, WorkerInstance] = {}
        self.active_worker: Optional[WorkerInstance] = None
        self._rollover_lock = asyncio.Lock()
        self._rollover_in_progress = False
        self._rollover_history: list[dict[str, Any]] = []
        self._clients: set[ClientWsMux] = set()
        self._secondary_ws_counts: dict[int, int] = {}
        self._http_client: Optional[httpx.AsyncClient] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._shutting_down = False
        self._idle_since: dict[int, float] = {}

    def get_worker(self, gen: int) -> Optional[WorkerInstance]:
        return self._workers.get(gen)

    def _resolve_worker_executable(self) -> list[str]:
        """Prefer ~/.hermes/bin/hermes so rolled-over workers pick up fresh venv/runtime."""
        override = os.environ.get("HERMES_SUPERVISOR_WORKER_CMD")
        if override:
            return override.split()
        hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        hermes_bin = hermes_home / "bin" / "hermes"
        if hermes_bin.is_file() and os.access(hermes_bin, os.X_OK):
            return [str(hermes_bin)]
        which_bin = shutil.which("hermes")
        if which_bin:
            return [which_bin]
        return [sys.executable, "-m", "hermes_cli.main"]

    def _build_worker_cmd(self, gen: int, token_r_fd: int) -> list[str]:
        if self.worker_cmd_builder is not None:
            return self.worker_cmd_builder(gen, token_r_fd)
        cmd = self._resolve_worker_executable()
        cmd.extend([
            "serve",
            "--isolated",
            "--no-supervisor",
            "--host", "127.0.0.1",
            "--port", "0",
            "--worker-gen", str(gen),
            "--worker-token-fd", str(token_r_fd),
        ])
        if self.open_profile:
            cmd.extend(["--open-profile", self.open_profile])
        if self.ssh_owner_nonce:
            cmd.extend(["--ssh-owner-nonce", self.ssh_owner_nonce])
        return cmd

    def _spawn_worker_proc(self, cmd: list[str], env: dict[str, str], r_fd: int) -> subprocess.Popen:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            pass_fds=(r_fd,),
            close_fds=True,
        )

    async def spawn_worker(self) -> WorkerInstance:
        """Spawn a new immutable worker generation and wait until /api/health returns 200."""
        gen = self._next_gen
        self._next_gen += 1

        r_fd, w_fd = os.pipe()
        os.set_inheritable(r_fd, True)
        cmd = self._build_worker_cmd(gen, r_fd)
        env = dict(os.environ)
        env["HERMES_SUPERVISOR_PID"] = str(os.getpid())
        env["HERMES_SUPERVISOR_GEN"] = str(gen)
        # Ensure worker stdout flushes READY sentinel immediately.
        env["PYTHONUNBUFFERED"] = "1"

        try:
            proc = self._spawn_worker_proc(cmd, env, r_fd)
        except Exception:
            os.close(r_fd)
            os.close(w_fd)
            raise

        # Close read end in parent; write token + close write end immediately.
        os.close(r_fd)
        try:
            os.write(w_fd, self.session_token.encode("utf-8"))
        finally:
            os.close(w_fd)

        worker_port: Optional[int] = None
        stderr_buf: list[str] = []

        async def _read_ready_port() -> int:
            assert proc.stdout is not None
            loop = asyncio.get_running_loop()
            while True:
                line_bytes = await loop.run_in_executor(None, proc.stdout.readline)
                if not line_bytes:
                    raise RuntimeError(
                        f"worker-gen-{gen} (pid={proc.pid}) exited before READY (rc={proc.poll()})"
                    )
                line = line_bytes.decode("utf-8", errors="replace").strip()
                m = _READY_PORT_RE.search(line)
                if m:
                    return int(m.group(1))

        async def _drain_stderr_bg() -> None:
            if proc.stderr is None:
                return
            loop = asyncio.get_running_loop()
            while True:
                line_bytes = await loop.run_in_executor(None, proc.stderr.readline)
                if not line_bytes:
                    break
                text = line_bytes.decode("utf-8", errors="replace").rstrip()
                if text:
                    stderr_buf.append(text)
                    if len(stderr_buf) > 50:
                        del stderr_buf[: len(stderr_buf) - 50]

        async def _drain_stdout_bg() -> None:
            if proc.stdout is None:
                return
            loop = asyncio.get_running_loop()
            while True:
                line_bytes = await loop.run_in_executor(None, proc.stdout.readline)
                if not line_bytes:
                    break

        stderr_task = asyncio.create_task(_drain_stderr_bg(), name=f"worker-stderr-{gen}")
        try:
            worker_port = await asyncio.wait_for(_read_ready_port(), timeout=self.worker_ready_timeout_s)
            # Keep draining stdout in background so child pipe never blocks
            asyncio.create_task(_drain_stdout_bg(), name=f"worker-stdout-{gen}")
        except Exception as exc:
            await self._terminate_proc(proc)
            stderr_task.cancel()
            tail = " | ".join(stderr_buf[-5:])
            raise RuntimeError(f"worker-gen-{gen} failed to become ready: {exc} (stderr={tail})") from exc

        # Verify /api/health returns 200 on the spawned worker
        commit = ""
        deadline = time.monotonic() + min(15.0, self.worker_ready_timeout_s)
        health_ok = False
        last_err = ""
        async with httpx.AsyncClient(timeout=3.0) as probe:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    resp = await probe.get(
                        f"http://127.0.0.1:{worker_port}/api/health",
                        headers={"X-Hermes-Session-Token": self.session_token},
                    )
                    if resp.status_code == 200:
                        health_ok = True
                        try:
                            data = resp.json()
                            commit = str(data.get("commit") or "")
                        except Exception:
                            pass
                        break
                    last_err = f"HTTP {resp.status_code}"
                except Exception as exc:
                    last_err = str(exc)
                await asyncio.sleep(0.15)

        if not health_ok:
            await self._terminate_proc(proc)
            stderr_task.cancel()
            tail = " | ".join(stderr_buf[-5:])
            raise RuntimeError(
                f"worker-gen-{gen} (port={worker_port}) failed /api/health probe: {last_err} (stderr={tail})"
            )

        worker = WorkerInstance(
            gen=gen,
            pid=proc.pid,
            port=worker_port,
            proc=proc,
            commit=commit or _read_disk_head_sha(),
            stderr_lines=stderr_buf,
        )
        self._workers[gen] = worker
        _log.info("Spawned worker-gen-%s pid=%s port=%s commit=%s", gen, proc.pid, worker_port, worker.commit)
        return worker

    async def _terminate_proc(self, proc: subprocess.Popen, grace_s: float = 10.0) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            return
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.1)
        try:
            proc.kill()
        except OSError:
            pass

    def _active_sessions_for_gen(self, gen: int) -> list[str]:
        sids: set[str] = set()
        for c in list(self._clients):
            sids.update(c.active_sessions_for_gen(gen))
        return sorted(sids)

    def _has_inflight_work(self, gen: int) -> bool:
        if self._secondary_ws_counts.get(gen, 0) > 0:
            return True
        return any(c.has_inflight_work_for_gen(gen) for c in list(self._clients))

    async def rollover(self, reason: str = "api") -> dict[str, Any]:
        """Spawn a new worker generation, health-verify it, atomically flip, and drain old."""
        async with self._rollover_lock:
            self._rollover_in_progress = True
            t0 = time.monotonic()
            old_worker = self.active_worker
            try:
                new_worker = await self.spawn_worker()
            except Exception as exc:
                self._rollover_in_progress = False
                rec = {
                    "ts": round(time.time(), 2),
                    "reason": reason,
                    "ok": False,
                    "error": str(exc),
                    "active_gen": old_worker.gen if old_worker else None,
                }
                self._rollover_history.append(rec)
                if len(self._rollover_history) > 20:
                    del self._rollover_history[: len(self._rollover_history) - 20]
                raise

            # Atomic cutover: all idle sessions + new turns + global RPCs immediately hit new_worker
            self.active_worker = new_worker
            if old_worker is not None and old_worker.gen != new_worker.gen:
                old_worker.draining = True
                old_worker.draining_since = time.time()

            elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)
            rec = {
                "ts": round(time.time(), 2),
                "reason": reason,
                "ok": True,
                "old_gen": old_worker.gen if old_worker else None,
                "old_pid": old_worker.pid if old_worker else None,
                "new_gen": new_worker.gen,
                "new_pid": new_worker.pid,
                "new_port": new_worker.port,
                "new_commit": new_worker.commit,
                "elapsed_ms": elapsed_ms,
                "draining_active_turns": len(self._active_sessions_for_gen(old_worker.gen)) if old_worker else 0,
            }
            self._rollover_history.append(rec)
            if len(self._rollover_history) > 20:
                del self._rollover_history[: len(self._rollover_history) - 20]
            self._rollover_in_progress = False
            # Immediately reap idle draining workers if settle is 0, else drain loop handles it
            await self._drain_sweep_once()
            return rec

    async def _retire_worker(self, worker: WorkerInstance) -> None:
        if worker.retired:
            return
        worker.retired = True
        for c in list(self._clients):
            await c.detach_worker(worker.gen)
        await self._terminate_proc(worker.proc)
        self._workers.pop(worker.gen, None)
        self._idle_since.pop(worker.gen, None)
        _log.info("Retired drained worker-gen-%s (pid=%s)", worker.gen, worker.pid)

    async def _drain_sweep_once(self) -> None:
        now = time.monotonic()
        draining = [w for w in list(self._workers.values()) if w.draining and not w.retired]
        for w in draining:
            if not w.is_alive():
                await self._retire_worker(w)
                continue
            if self._has_inflight_work(w.gen):
                self._idle_since.pop(w.gen, None)
                continue
            first_idle = self._idle_since.setdefault(w.gen, now)
            if (now - first_idle) >= self.drain_settle_s:
                await self._retire_worker(w)

    async def _drain_loop(self) -> None:
        try:
            while not self._shutting_down:
                await asyncio.sleep(self.drain_poll_s)
                await self._drain_sweep_once()
        except asyncio.CancelledError:
            raise

    def _is_authenticated(self, request: Request) -> bool:
        """Verify caller holds the supervisor's session token."""
        hdr = request.headers.get("x-hermes-session-token") or ""
        if not hdr:
            auth = request.headers.get("authorization") or ""
            if auth.lower().startswith("bearer "):
                hdr = auth[7:].strip()
        if not hdr:
            hdr = request.query_params.get("token") or ""
        return bool(hdr) and secrets.compare_digest(hdr, self.session_token)

    def status_payload(self) -> dict[str, Any]:
        active = self.active_worker
        draining = [
            w.to_status_dict(self._active_sessions_for_gen(w.gen))
            for w in sorted(self._workers.values(), key=lambda x: x.gen)
            if w.draining and not w.retired
        ]
        all_pins: dict[str, int] = {}
        for c in list(self._clients):
            all_pins.update(c.active_turn_pins)
        return {
            "pid": os.getpid(),
            "host": self.host,
            "port": self.bound_port,
            "uptime_s": round(time.time() - self.started_at, 1),
            "disk_head": _read_disk_head_sha(),
            "active_gen": active.gen if active else None,
            "active_worker_pid": active.pid if active else None,
            "active_worker_port": active.port if active else None,
            "active_worker_commit": active.commit if active else "",
            "draining_workers": draining,
            "active_turn_pins": all_pins,
            "connected_ws_clients": len(self._clients),
            "rollover_in_progress": self._rollover_in_progress,
            "rollover_history": self._rollover_history[-5:],
        }

    def _worker_for_http_path(self, path: str) -> Optional[WorkerInstance]:
        """Route session-specific HTTP calls to the pinned draining worker if a turn is active."""
        m = _SESSION_PATH_RE.match(path)
        if m:
            sid = m.group(1)
            for c in list(self._clients):
                gen = c.active_turn_pins.get(sid)
                if gen is not None:
                    w = self.get_worker(gen)
                    if w is not None and w.is_alive():
                        return w
        return self.active_worker

    async def _handle_health(self, request: Request) -> Response:
        active = self.active_worker
        sup_meta = self.status_payload()
        if active is None or not active.is_alive():
            return JSONResponse(
                {"status": "degraded", "commit": "", "supervisor": sup_meta},
                status_code=503,
            )
        assert self._http_client is not None
        try:
            resp = await self._http_client.get(
                f"{active.base_url}/api/health",
                headers={"X-Hermes-Session-Token": self.session_token},
                timeout=5.0,
            )
            data = resp.json()
            if isinstance(data, dict):
                if data.get("commit"):
                    active.commit = str(data["commit"])
                data["supervisor"] = {
                    "pid": os.getpid(),
                    "active_gen": active.gen,
                    "active_worker_pid": active.pid,
                    "active_worker_commit": active.commit,
                    "draining_count": len(sup_meta["draining_workers"]),
                    "draining_workers": sup_meta["draining_workers"],
                    "rollover_in_progress": self._rollover_in_progress,
                }
                return JSONResponse(data, status_code=resp.status_code)
        except Exception as exc:
            return JSONResponse(
                {"status": "degraded", "error": str(exc), "commit": active.commit, "supervisor": sup_meta},
                status_code=503,
            )
        return JSONResponse({"status": "ok", "commit": active.commit, "supervisor": sup_meta})

    async def _handle_supervisor_status(self, request: Request) -> Response:
        if not self._is_authenticated(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(self.status_payload())

    async def _handle_supervisor_rollover(self, request: Request) -> Response:
        if not self._is_authenticated(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        reason = request.query_params.get("reason") or "http_api"
        try:
            rec = await self.rollover(reason=reason)
            return JSONResponse({"ok": True, "rollover": rec, "supervisor": self.status_payload()})
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "supervisor": self.status_payload()},
                status_code=502,
            )

    async def _handle_health_idle(self, request: Request) -> Response:
        live = [w for w in self._workers.values() if w.is_alive() and not w.retired]
        total_turns = sum(len(self._active_sessions_for_gen(w.gen)) for w in live)
        worker_idle = True
        assert self._http_client is not None
        for w in live:
            try:
                r = await self._http_client.get(
                    f"{w.base_url}/api/health/idle",
                    headers={"X-Hermes-Session-Token": self.session_token},
                    timeout=3.0,
                )
                if r.status_code == 200:
                    d = r.json()
                    if isinstance(d, dict) and d.get("idle") is False:
                        worker_idle = False
            except Exception:
                pass
        idle = (total_turns == 0) and worker_idle and not self._rollover_in_progress
        return JSONResponse({
            "idle": idle,
            "active_turns": total_turns,
            "workers": len(live),
            "draining": sum(1 for w in live if w.draining),
        })

    async def _handle_plugins_activate_fanout(self, request: Request) -> Response:
        """Fan out plugin hot-reload activation to ALL live workers (active + draining)."""
        body = await request.body()
        live = [w for w in self._workers.values() if w.is_alive() and not w.retired]
        if not live:
            return JSONResponse({"error": "no live workers"}, status_code=503)
        assert self._http_client is not None
        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP_HEADERS and k.lower() != "host"
        }
        results: list[dict[str, Any]] = []
        status = 200
        for w in live:
            try:
                r = await self._http_client.post(
                    f"{w.base_url}{request.url.path}",
                    content=body,
                    headers={**fwd_headers, "host": f"127.0.0.1:{w.port}"},
                    timeout=15.0,
                )
                if r.status_code >= 400 and w is self.active_worker:
                    status = r.status_code
                try:
                    payload = r.json()
                except Exception:
                    payload = r.text
                results.append({"gen": w.gen, "pid": w.pid, "status": r.status_code, "body": payload})
            except Exception as exc:
                results.append({"gen": w.gen, "pid": w.pid, "error": str(exc)})
                if w is self.active_worker:
                    status = 502
        return JSONResponse({"fanout": results}, status_code=status)

    async def _handle_http_proxy(self, request: Request) -> Response:
        worker = self._worker_for_http_path(request.url.path)
        if worker is None or not worker.is_alive():
            return JSONResponse({"error": "no active worker"}, status_code=503)
        assert self._http_client is not None

        target_url = f"{worker.base_url}{request.url.path}"
        if request.url.query:
            target_url = f"{target_url}?{request.url.query}"

        fwd_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP_HEADERS and k.lower() != "host"
        }
        fwd_headers["host"] = f"127.0.0.1:{worker.port}"

        body = await request.body()
        req = self._http_client.build_request(
            method=request.method,
            url=target_url,
            headers=fwd_headers,
            content=body if body else None,
        )
        try:
            upstream = await self._http_client.send(req, stream=True)
        except Exception as exc:
            return JSONResponse({"error": f"upstream proxy error: {exc}"}, status_code=502)

        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _HOP_BY_HOP_HEADERS
        }

        async def _stream_body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            _stream_body(),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
        )

    async def _handle_api_ws(self, websocket: WebSocket) -> None:
        await websocket.accept()
        extra_headers: dict[str, str] = {}
        for k in ("x-hermes-session-token", "authorization", "cookie", "origin"):
            val = websocket.headers.get(k)
            if val:
                extra_headers[k] = val
        qs = urlencode(list(websocket.query_params.multi_items())) if websocket.query_params else ""
        mux = ClientWsMux(self, websocket, qs, extra_headers)
        self._clients.add(mux)
        try:
            await mux.run()
        finally:
            self._clients.discard(mux)

    async def _handle_generic_ws_tunnel(self, websocket: WebSocket) -> None:
        worker = self.active_worker
        if worker is None or not worker.is_alive():
            await websocket.close(code=1013, reason="no active worker")
            return
        await websocket.accept()
        extra_headers: dict[str, str] = {}
        for k in ("x-hermes-session-token", "authorization", "cookie", "origin"):
            val = websocket.headers.get(k)
            if val:
                extra_headers[k] = val
        if "x-hermes-session-token" not in {k.lower() for k in extra_headers}:
            extra_headers["X-Hermes-Session-Token"] = self.session_token
        path = websocket.url.path
        qs = urlencode(list(websocket.query_params.multi_items())) if websocket.query_params else ""
        url = f"{worker.ws_base_url}{path}" + (f"?{qs}" if qs else "")

        self._secondary_ws_counts[worker.gen] = self._secondary_ws_counts.get(worker.gen, 0) + 1
        try:
            async with websockets.connect(
                url,
                additional_headers=extra_headers,
                max_size=64 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            ) as up:
                async def _c2u() -> None:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if msg.get("bytes") is not None:
                            await up.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await up.send(msg["text"])

                async def _u2c() -> None:
                    async for data in up:
                        if isinstance(data, bytes):
                            await websocket.send_bytes(data)
                        else:
                            await websocket.send_text(data)

                t1 = asyncio.create_task(_c2u())
                t2 = asyncio.create_task(_u2c())
                done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
                for d in done:
                    try:
                        d.result()
                    except (WebSocketDisconnect, asyncio.CancelledError, websockets.ConnectionClosed):
                        pass
        except Exception as exc:
            _log.debug("Secondary WS tunnel %s ended: %s", path, exc)
            try:
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            cur = self._secondary_ws_counts.get(worker.gen, 1) - 1
            if cur <= 0:
                self._secondary_ws_counts.pop(worker.gen, None)
            else:
                self._secondary_ws_counts[worker.gen] = cur

    def build_app(self) -> Starlette:
        routes = [
            Route("/api/health", self._handle_health, methods=["GET"]),
            Route("/api/health/idle", self._handle_health_idle, methods=["GET"]),
            Route("/api/supervisor/status", self._handle_supervisor_status, methods=["GET"]),
            Route("/api/supervisor/rollover", self._handle_supervisor_rollover, methods=["POST"]),
            Route(
                "/api/dashboard/agent-plugins/activate",
                self._handle_plugins_activate_fanout,
                methods=["POST"],
            ),
            WebSocketRoute("/api/ws", self._handle_api_ws),
            WebSocketRoute("/{path:path}", self._handle_generic_ws_tunnel),
            Route(
                "/{path:path}",
                self._handle_http_proxy,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            ),
        ]
        return Starlette(routes=routes)

    async def start_runtime(self) -> None:
        self._http_client = httpx.AsyncClient(timeout=None, follow_redirects=False)
        first = await self.spawn_worker()
        self.active_worker = first
        self._drain_task = asyncio.create_task(self._drain_loop(), name="serve-supervisor-drain")

    async def shutdown_runtime(self) -> None:
        self._shutting_down = True
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
        for c in list(self._clients):
            await c.close()
        for w in list(self._workers.values()):
            await self._retire_worker(w)
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None


def _read_bound_port(server: uvicorn.Server, fallback: int) -> int:
    for s in getattr(server, "servers", None) or ():
        for sock in getattr(s, "sockets", None) or ():
            try:
                return int(sock.getsockname()[1])
            except Exception:
                pass
    return fallback


def _publish_supervisor_rendezvous(host: str, port: int, token: str, isolated: bool) -> None:
    """Publish supervisor port into Desktop/host rendezvous so hot-reload/CLI find it."""
    try:
        from gateway import host_rendezvous as hr
        from hermes_cli.process_identity import is_desktop_owned_backend

        desktop_child = is_desktop_owned_backend()
        if isolated and not desktop_child:
            return
        role = hr.ROLE_DESKTOP_SERVE if desktop_child else hr.ROLE_SERVE
        outcome, _err = hr.claim_host_lock(role)
        if outcome is hr.HostLockOutcome.ACQUIRED:
            hr.publish_record(role, host=host, port=port, profiles=hr.served_profiles(), token=token)
            hr.cleanup_on_exit(role)
    except Exception as exc:
        _log.debug("Supervisor rendezvous publish skipped: %s", exc)


def _register_supervisor_identity(host: str, port: int, open_profile: str, isolated: bool) -> None:
    try:
        from hermes_cli.process_identity import attach_self_to_kill_on_close_job, register_self

        register_self(
            "serve",
            detail={
                "host": host,
                "port": port,
                "profile": open_profile or "",
                "isolated": isolated,
                "supervisor": True,
            },
        )
        attach_self_to_kill_on_close_job()
    except Exception as exc:
        _log.debug("Supervisor process identity registration skipped: %s", exc)


def run_serve_supervisor(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    session_token: Optional[str] = None,
    ssh_owner_nonce: Optional[str] = None,
    ssh_lock_path: Optional[Path] = None,
    isolated: bool = True,
    open_profile: str = "",
) -> None:
    """Entry point invoked by ``hermes serve --isolated`` when supervisor mode is active."""
    sup = ServeSupervisor(
        host=host,
        port=port,
        session_token=session_token,
        ssh_owner_nonce=ssh_owner_nonce,
        ssh_lock_path=ssh_lock_path,
        isolated=isolated,
        open_profile=open_profile,
    )
    starlette_app = sup.build_app()
    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        ws_max_size=64 * 1024 * 1024,
    )
    server = uvicorn.Server(config)

    async def _run() -> None:
        await sup.start_runtime()
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)

        loop = asyncio.get_running_loop()
        if hasattr(signal, "SIGHUP"):
            try:
                loop.add_signal_handler(
                    signal.SIGHUP,
                    lambda: asyncio.create_task(sup.rollover(reason="sighup")),
                )
            except (NotImplementedError, RuntimeError):
                pass

        with server.capture_signals():
            await server.startup()
            if server.should_exit:
                await sup.shutdown_runtime()
                return

            actual_port = _read_bound_port(server, fallback=port)
            sup.bound_port = actual_port

            # Start Desktop parent-death watchdog (no-op if HERMES_PARENT_PID is unset)
            try:
                from hermes_cli.web_server_lifecycle import _start_parent_death_watchdog
                _start_parent_death_watchdog()
            except Exception:
                pass

            # Owner supersession watchdog for SSH-isolated lockfiles
            if ssh_lock_path and ssh_owner_nonce:
                try:
                    from hermes_cli.web_server_owner_exit import start_owner_watchdog
                    start_owner_watchdog(server, lock_path=ssh_lock_path, nonce=ssh_owner_nonce)
                except Exception:
                    pass

            _register_supervisor_identity(host, actual_port, open_profile, isolated)
            _publish_supervisor_rendezvous(host, actual_port, sup.session_token, isolated)

            # Announce exact Desktop-expected sentinels on fd 1
            _write_machine_sentinel_line(f"HERMES_BACKEND_READY port={actual_port}")
            _write_machine_sentinel_line(f"HERMES_DASHBOARD_READY port={actual_port}")
            print(
                f"  Hermes Front-Door Supervisor listening on {host}:{actual_port} "
                f"(active worker-gen-1 pid={sup.active_worker.pid if sup.active_worker else '?'})",
                flush=True,
            )

            try:
                await server.main_loop()
            finally:
                if server.started:
                    await server.shutdown()
                await sup.shutdown_runtime()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
