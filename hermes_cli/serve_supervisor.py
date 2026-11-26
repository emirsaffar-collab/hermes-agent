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

# v2: ingress WS auth (zero unauth token injection), real tui_gateway event/params.type
# envelope parsing, client.capabilities replay, cross-gen session rehydration,
# enforced max_draining_workers=1, and authenticated busy_ledger drain checks.
SUPERVISOR_PROTOCOL_VERSION = 2

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
    "prompt.background",
    "prompt.btw",
    "prompt.enqueue",
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
    "subagent.",
    "subagents.",
    "process.",
    "shell.",
    "cli.",
    "command.",
    "slash.",
    "rollback.",
    "handoff.",
    "terminal.",
    "agents.",
)

# Priority when merging ``session.active_list`` rows across active + draining workers
# (higher wins so an active in-flight turn on a draining worker is never shadowed by an
# idle row on the newest worker).
_ACTIVE_LIST_STATUS_RANK: dict[str, int] = {
    "working": 4,
    "waiting": 3,
    "starting": 2,
    "idle": 1,
}

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
    "message.complete",
    "error",
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

_ACTIVE_EVENT_TYPES = frozenset({
    "message.start",
    "turn_started",
    "running",
    "streaming",
    "in_progress",
})

_SESSION_PATH_RE = re.compile(r"^/api/sessions/([^/]+)(?:/|$)")
_SESSION_LIFECYCLE_METHODS = frozenset({"session.create", "session.resume", "sessions.create"})
_SESSION_CLOSE_METHODS = frozenset({"session.close", "sessions.close"})


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
        if method == "event" and ev == "session.info":
            if params.get("running") is False:
                return True
            payload = params.get("payload")
            if isinstance(payload, dict) and payload.get("running") is False:
                return True
    return False


def _is_turn_start_notification(method: str, params: Any) -> bool:
    if method in _ACTIVE_TURN_METHODS:
        return True
    if method == "session.info" and isinstance(params, dict) and params.get("running") is True:
        return True
    if isinstance(params, dict):
        ev = str(params.get("type") or params.get("event") or params.get("status") or "").lower()
        if ev in _ACTIVE_EVENT_TYPES:
            return True
        if method == "event" and ev == "session.info":
            if params.get("running") is True:
                return True
            payload = params.get("payload")
            if isinstance(payload, dict) and payload.get("running") is True:
                return True
    return False


def _extract_request_cancel_id(method: str, params: Any) -> Optional[str]:
    """Return cancelled srq-* request id if frame is a request.cancel notification."""
    if not isinstance(params, dict):
        return None
    if method == "request.cancel":
        rid = params.get("id")
        return str(rid) if rid is not None else None
    if method == "event" and str(params.get("type") or "").lower() == "request.cancel":
        payload = params.get("payload")
        if isinstance(payload, dict) and payload.get("id") is not None:
            return str(payload["id"])
        if params.get("id") is not None:
            return str(params["id"])
    return None


def _strip_single_use_ticket_qs(query_string: str) -> str:
    """Remove single-use ?ticket= parameter before connecting to a rolled-over worker."""
    if not query_string:
        return ""
    from urllib.parse import parse_qsl
    pairs = [(k, v) for k, v in parse_qsl(query_string, keep_blank_values=True) if k.lower() != "ticket"]
    return urlencode(pairs)


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
        subprotocols: Optional[list[str]] = None,
    ) -> None:
        self.supervisor = supervisor
        self.client_ws = client_ws
        self.query_string = query_string
        self.extra_headers = extra_headers
        self.subprotocols = list(subprotocols or [])
        self._send_lock = asyncio.Lock()
        self._accepted = asyncio.Event()
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
        # Pending request metadata: req_id -> (method, params)
        self._pending_req_meta: dict[Any, tuple[str, dict[str, Any]]] = {}
        # Cached handshake frames to replay transparently to newly rolled-over workers
        self._hello_frame: Optional[dict[str, Any]] = None
        self._capabilities_frames: list[dict[str, Any]] = []
        self._synthetic_init_ids: set[str] = set()
        self._synthetic_futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Set True ONLY after initial upstream worker accepts the client's real credentials.
        self._client_authenticated: bool = False
        # Cross-gen session tracking and bidirectional runtime-sid aliasing:
        # client_sid -> {"stored_key": str, "cwd": Optional[str], "profile": Optional[str], "known_gens": set[int]}
        self._session_meta: dict[str, dict[str, Any]] = {}
        self._sid_to_worker: dict[tuple[str, int], str] = {}
        self._sid_from_worker: dict[tuple[str, int], str] = {}
        self._rehydrate_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._bg_tasks: set[asyncio.Task] = set()

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
        await self._accepted.wait()
        if self._closed:
            return
        async with self._send_lock:
            if not self._closed:
                await self.client_ws.send_text(text)

    def _to_client_sid(self, worker_sid: Optional[str], gen: int) -> Optional[str]:
        if not worker_sid:
            return worker_sid
        return self._sid_from_worker.get((worker_sid, gen), worker_sid)

    def _to_worker_sid(self, client_sid: str, gen: int) -> str:
        return self._sid_to_worker.get((client_sid, gen), client_sid)

    @staticmethod
    def _rewrite_sid_in_dict(obj: Any, old_sid: str, new_sid: str) -> bool:
        """Replace matching runtime session_id fields in-place; return True if mutated."""
        if not isinstance(obj, dict) or old_sid == new_sid:
            return False
        changed = False
        for key in ("session_id", "sessionId", "sid"):
            if obj.get(key) == old_sid:
                obj[key] = new_sid
                changed = True
        return changed

    async def _send_synthetic_rpc(self, worker: WorkerInstance, up: Any, method: str, params: dict[str, Any], timeout_s: float = 15.0) -> Optional[dict[str, Any]]:
        syn_id = f"__sup_syn_{worker.gen}_{secrets.token_hex(6)}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._synthetic_futures[syn_id] = fut
        try:
            await up.send(json.dumps({"jsonrpc": "2.0", "id": syn_id, "method": method, "params": params}))
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except Exception as exc:
            _log.debug("Synthetic RPC %s to gen=%s failed: %s", method, worker.gen, exc)
            return None
        finally:
            self._synthetic_futures.pop(syn_id, None)

    async def _ensure_session_on_worker(self, client_sid: str, worker: WorkerInstance) -> None:
        """Transparently rehydrate an idle session onto a new worker generation after rollover."""
        meta = self._session_meta.get(client_sid)
        if not meta or worker.gen in meta["known_gens"]:
            return
        lock = self._rehydrate_locks.setdefault((client_sid, worker.gen), asyncio.Lock())
        async with lock:
            if worker.gen in meta["known_gens"]:
                return
            up = await self.ensure_upstream(worker)
            stored_key = meta.get("stored_key") or ""
            resp: Optional[dict[str, Any]] = None
            if stored_key:
                resume_params: dict[str, Any] = {
                    "session_id": stored_key,
                    "omit_messages": True,
                }
                if meta.get("profile"):
                    resume_params["profile"] = meta["profile"]
                resp = await self._send_synthetic_rpc(worker, up, "session.resume", resume_params)
            new_worker_sid: Optional[str] = None
            if isinstance(resp, dict) and isinstance(resp.get("result"), dict):
                cand = resp["result"].get("session_id")
                if isinstance(cand, str) and cand.strip():
                    new_worker_sid = cand.strip()
            if not new_worker_sid:
                # Fallback for brand-new empty tabs not yet persisted to state.db
                create_params: dict[str, Any] = {}
                if meta.get("cwd"):
                    create_params["cwd"] = meta["cwd"]
                if meta.get("profile"):
                    create_params["profile"] = meta["profile"]
                resp2 = await self._send_synthetic_rpc(worker, up, "session.create", create_params)
                if isinstance(resp2, dict) and isinstance(resp2.get("result"), dict):
                    cand2 = resp2["result"].get("session_id")
                    if isinstance(cand2, str) and cand2.strip():
                        new_worker_sid = cand2.strip()
                        sk2 = resp2["result"].get("stored_session_id") or resp2["result"].get("session_key")
                        if isinstance(sk2, str) and sk2.strip():
                            meta["stored_key"] = sk2.strip()
            if new_worker_sid:
                self._sid_to_worker[(client_sid, worker.gen)] = new_worker_sid
                self._sid_from_worker[(new_worker_sid, worker.gen)] = client_sid
                meta["known_gens"].add(worker.gen)
                _log.debug(
                    "Rehydrated session %s -> worker_sid=%s on gen=%s (stored_key=%s)",
                    client_sid, new_worker_sid, worker.gen, stored_key or "-",
                )

    async def ensure_upstream(self, worker: WorkerInstance) -> Any:
        existing = self._upstreams.get(worker.gen)
        if existing is not None:
            return existing
        lock = self._connect_locks.setdefault(worker.gen, asyncio.Lock())
        async with lock:
            existing = self._upstreams.get(worker.gen)
            if existing is not None:
                return existing
            is_rollover_upstream = self._client_authenticated
            qs = _strip_single_use_ticket_qs(self.query_string) if is_rollover_upstream else self.query_string
            url = f"{worker.ws_base_url}/api/ws"
            if qs:
                url = f"{url}?{qs}"
            headers = dict(self.extra_headers)
            subprotos = list(self.subprotocols)
            # SECURITY (P0-1): NEVER inject self.supervisor.session_token on the initial
            # unauthenticated handshake. Only attach the supervisor token when opening a
            # secondary upstream to a rolled-over worker AFTER the initial worker already
            # authenticated this client connection.
            if is_rollover_upstream:
                headers = {
                    k: v for k, v in headers.items()
                    if k.lower() != "x-hermes-gateway-ticket"
                }
                subprotos = [
                    p for p in subprotos
                    if not p.startswith("hermes-gateway-ticket.")
                ]
                if self.supervisor.session_token and "x-hermes-session-token" not in {
                    k.lower() for k in headers
                }:
                    headers["X-Hermes-Session-Token"] = self.supervisor.session_token
            connect_kwargs: dict[str, Any] = {
                "additional_headers": headers,
                "max_size": 64 * 1024 * 1024,
                "ping_interval": 20,
                "ping_timeout": 20,
                "close_timeout": 5,
            }
            if subprotos:
                connect_kwargs["subprotocols"] = subprotos
            ws = await websockets.connect(url, **connect_kwargs)
            self._client_authenticated = True
            self._upstreams[worker.gen] = ws
            self._reader_tasks[worker.gen] = asyncio.create_task(
                self._pump_upstream(worker, ws),
                name=f"ws-mux-upstream-gen-{worker.gen}",
            )
            # Replay client capabilities (real tui_gateway handshake) + legacy hello to newly
            # rolled-over workers so gen-(N+1) accepts server_requests (approvals/clarify/vault).
            if is_rollover_upstream:
                for idx, cap_frame in enumerate(list(self._capabilities_frames)):
                    syn_id = f"__sup_cap_{worker.gen}_{idx}_{secrets.token_hex(4)}"
                    self._synthetic_init_ids.add(syn_id)
                    replay = dict(cap_frame)
                    replay["id"] = syn_id
                    await ws.send(json.dumps(replay))
                if self._hello_frame is not None:
                    syn_id = f"__sup_hello_{worker.gen}_{secrets.token_hex(4)}"
                    self._synthetic_init_ids.add(syn_id)
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
        # Clean up any leftover pins and aliases referencing this departed worker
        interrupted_sids = [s for s, g in self.active_turn_pins.items() if g == gen]
        for sid in interrupted_sids:
            self.active_turn_pins.pop(sid, None)
        for rid in [r for r, g in self.pending_srq.items() if g == gen]:
            self.pending_srq.pop(rid, None)
        for rid in [r for r, g in self.pending_reqs.items() if g == gen]:
            self.pending_reqs.pop(rid, None)
            self._pending_req_meta.pop(rid, None)
        for k in [k for k in self._sid_to_worker if k[1] == gen]:
            self._sid_to_worker.pop(k, None)
        for k in [k for k in self._sid_from_worker if k[1] == gen]:
            self._sid_from_worker.pop(k, None)
        for meta in self._session_meta.values():
            meta["known_gens"].discard(gen)
        # If a draining worker was forcibly retired (e.g. drain_max_s exceeded) while turns
        # were still pinned on it, emit an explicit terminal error event so Desktop settles
        # cleanly instead of waiting 45s and painting "Hermes ended this turn without a reply."
        if interrupted_sids and not self._closed:
            for sid in interrupted_sids:
                err_frame = {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "error",
                        "session_id": sid,
                        "payload": {
                            "message": (
                                f"Backend worker generation {gen} retired while this turn was running; "
                                "send or retry to continue on the active worker."
                            ),
                        },
                    },
                }
                try:
                    await self.send_to_client(json.dumps(err_frame))
                except Exception:
                    break

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
            params = msg.get("params")
            worker_sid = _extract_session_id(params)
            client_sid = self._to_client_sid(worker_sid, worker.gen)
            if worker_sid and client_sid and client_sid != worker_sid:
                self._rewrite_sid_in_dict(params, worker_sid, client_sid)
                raw = json.dumps(msg)
            await self.send_to_client(raw)
            return

        # 2. Response to a client request (or synthetic init/rehydration RPC)
        if has_id and not has_method:
            rid = msg["id"]
            if isinstance(rid, str):
                fut = self._synthetic_futures.pop(rid, None)
                if fut is not None:
                    if not fut.done():
                        fut.set_result(msg)
                    return
                if rid in self._synthetic_init_ids:
                    self._synthetic_init_ids.discard(rid)
                    return
            self.pending_reqs.pop(rid, None)
            req_meta = self._pending_req_meta.pop(rid, None)
            # Track session.create / session.resume metadata so idle sessions can be
            # transparently rehydrated onto gen-(N+1) after rollover.
            if req_meta is not None and isinstance(msg.get("result"), dict):
                req_method, req_params = req_meta
                res = msg["result"]
                if req_method in _SESSION_LIFECYCLE_METHODS:
                    created_sid = res.get("session_id")
                    stored_key = res.get("stored_session_id") or res.get("session_key") or res.get("resumed") or ""
                    if isinstance(created_sid, str) and created_sid.strip():
                        csid = created_sid.strip()
                        meta = self._session_meta.setdefault(
                            csid,
                            {
                                "stored_key": "",
                                "cwd": req_params.get("cwd") if isinstance(req_params, dict) else None,
                                "profile": req_params.get("profile") if isinstance(req_params, dict) else None,
                                "known_gens": set(),
                            },
                        )
                        if isinstance(stored_key, str) and stored_key.strip():
                            meta["stored_key"] = stored_key.strip()
                        meta["known_gens"].add(worker.gen)
                        if res.get("running") is True:
                            self.active_turn_pins[csid] = worker.gen
                elif req_method in _SESSION_CLOSE_METHODS:
                    closed_sid = _extract_session_id(req_params)
                    if closed_sid:
                        self._session_meta.pop(closed_sid, None)
            # Translate worker_sid back to client_sid in result payload if aliased
            if isinstance(msg.get("result"), dict):
                res_sid = _extract_session_id(msg["result"])
                client_res_sid = self._to_client_sid(res_sid, worker.gen)
                if res_sid and client_res_sid and client_res_sid != res_sid:
                    self._rewrite_sid_in_dict(msg["result"], res_sid, client_res_sid)
                    raw = json.dumps(msg)
            # If a turn-start RPC returned an immediate JSON-RPC error, release turn pin
            if "error" in msg:
                # Best-effort: error payload may include sessionId; also check req_meta
                err_data = msg.get("error", {}).get("data") if isinstance(msg.get("error"), dict) else None
                sid = self._to_client_sid(_extract_session_id(err_data), worker.gen)
                if not sid and req_meta is not None:
                    sid = _extract_session_id(req_meta[1])
                if sid and self.active_turn_pins.get(sid) == worker.gen:
                    self.active_turn_pins.pop(sid, None)
            await self.send_to_client(raw)
            return

        # 3. Server-initiated notification (streaming delta, turn lifecycle, broadcast)
        if has_method and not has_id:
            method = msg["method"]
            params = msg.get("params")
            cancel_rid = _extract_request_cancel_id(method, params)
            if cancel_rid is not None:
                self.pending_srq.pop(cancel_rid, None)
            worker_sid = _extract_session_id(params)
            sid = self._to_client_sid(worker_sid, worker.gen)
            if worker_sid and sid and sid != worker_sid:
                self._rewrite_sid_in_dict(params, worker_sid, sid)
                raw = json.dumps(msg)
            if sid:
                if _is_terminal_notification(method, params):
                    if self.active_turn_pins.get(sid) == worker.gen:
                        self.active_turn_pins.pop(sid, None)
                elif _is_turn_start_notification(method, params):
                    self.active_turn_pins[sid] = worker.gen
            # Suppress redundant global/unpinned broadcasts from draining workers
            if worker.draining:
                if sid or cancel_rid is not None:
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
        elif method == "client.capabilities" and has_id:
            # Record every client.capabilities call in order (Desktop may send
            # server_requests and desktop_local in separate calls).
            self._capabilities_frames.append(dict(msg))

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
        # Establish authenticated upstream connection BEFORE accepting client WS
        # so unauthenticated / invalid-credential clients are rejected pre-accept.
        active = self.supervisor.active_worker
        if active is None or not active.is_alive():
            await self.client_ws.close(code=1013, reason="no active worker")
            return
        try:
            up = await self.ensure_upstream(active)
        except Exception as exc:
            _log.warning("Failed to connect upstream /api/ws to gen=%s: %s", active.gen, exc)
            code = 4401 if ("401" in str(exc) or "403" in str(exc) or "4401" in str(exc)) else 1011
            try:
                await self.client_ws.close(code=code, reason="upstream connect failed")
            except Exception:
                pass
            return

        negotiated_subprotocol = getattr(up, "subprotocol", None)
        await self.client_ws.accept(subprotocol=negotiated_subprotocol)
        self._accepted.set()

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

        method = msg.get("method", "") if isinstance(msg.get("method"), str) else ""
        params = msg.get("params")

        # Fan out ``session.active_list`` across the active worker AND any connected live
        # draining workers, rewriting worker_sid -> client_sid on every row so Desktop's
        # 45s silent-turn check never misclassifies a live draining or rehydrated turn as
        # "Hermes ended this turn without a reply."
        if method == "session.active_list" and "id" in msg and msg["id"] is not None:
            bg = asyncio.create_task(self._handle_active_list_rpc(msg))
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
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

        client_sid = _extract_session_id(params) if _is_session_scoped_method(method) else None

        if "method" in msg and "id" in msg and msg["id"] is not None:
            self.pending_reqs[msg["id"]] = target.gen
            self._pending_req_meta[msg["id"]] = (method, dict(params) if isinstance(params, dict) else {})

        try:
            up = await self.ensure_upstream(target)
            # Transparently rehydrate idle session on rolled-over worker before forwarding
            if client_sid and client_sid in self._session_meta:
                await self._ensure_session_on_worker(client_sid, target)
                worker_sid = self._to_worker_sid(client_sid, target.gen)
                if worker_sid != client_sid and isinstance(params, dict):
                    fwd_msg = dict(msg)
                    fwd_params = dict(params)
                    self._rewrite_sid_in_dict(fwd_params, client_sid, worker_sid)
                    fwd_msg["params"] = fwd_params
                    raw = json.dumps(fwd_msg)
            await up.send(raw)
        except Exception as exc:
            _log.warning("Forwarding client frame to gen=%s failed: %s", target.gen, exc)
            if "id" in msg and msg["id"] is not None:
                self.pending_reqs.pop(msg["id"], None)
                self._pending_req_meta.pop(msg["id"], None)
                err = {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32003, "message": f"Worker gen {target.gen} forward failed: {exc}"},
                }
                await self.send_to_client(json.dumps(err))

    async def _handle_active_list_rpc(self, msg: dict[str, Any]) -> None:
        """Fan out ``session.active_list`` across active + connected draining workers,
        rewriting aliased worker runtime IDs back to client-visible IDs and merging by
        highest live-status priority."""
        req_id = msg["id"]
        raw_params = msg.get("params")
        params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}
        client_current = str(params.get("current_session_id") or "")

        active = self.supervisor.active_worker
        workers: list[WorkerInstance] = []
        seen_gens: set[int] = set()
        if active is not None and active.is_alive():
            workers.append(active)
            seen_gens.add(active.gen)
        # Include every other live, non-retired worker (draining generations) that this
        # client is already connected to or has pinned turns on.
        for gen, w in sorted(self.supervisor._workers.items(), reverse=True):
            if gen in seen_gens or w.retired or not w.is_alive():
                continue
            if gen in self._upstreams or any(g == gen for g in self.active_turn_pins.values()):
                workers.append(w)
                seen_gens.add(gen)

        if not workers:
            err = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32002, "message": "No active Hermes worker available"},
            }
            await self.send_to_client(json.dumps(err))
            return

        async def _query_one(w: WorkerInstance) -> tuple[WorkerInstance, Optional[dict[str, Any]]]:
            try:
                up = await self.ensure_upstream(w)
            except Exception as exc:
                _log.debug("session.active_list upstream connect to gen=%s failed: %s", w.gen, exc)
                return w, None
            w_params = dict(params)
            if client_current:
                w_params["current_session_id"] = self._to_worker_sid(client_current, w.gen)
            resp = await self._send_synthetic_rpc(w, up, "session.active_list", w_params, timeout_s=10.0)
            return w, resp

        results = await asyncio.gather(*(_query_one(w) for w in workers), return_exceptions=True)

        merged: dict[str, tuple[tuple[int, int, int], dict[str, Any]]] = {}
        order: list[str] = []
        any_ok = False
        first_error: Optional[dict[str, Any]] = None

        for item in results:
            if isinstance(item, BaseException):
                continue
            w, resp = item
            if not isinstance(resp, dict):
                continue
            if "error" in resp:
                if first_error is None:
                    first_error = resp["error"]
                continue
            res = resp.get("result")
            if not isinstance(res, dict) or not isinstance(res.get("sessions"), list):
                continue
            any_ok = True
            for raw_row in res["sessions"]:
                if not isinstance(raw_row, dict):
                    continue
                row = dict(raw_row)
                raw_id = row.get("id")
                if isinstance(raw_id, str) and raw_id.strip():
                    client_sid = self._to_client_sid(raw_id.strip(), w.gen) or raw_id.strip()
                    row["id"] = client_sid
                else:
                    client_sid = str(raw_id or "")
                if client_current:
                    row["current"] = client_sid == client_current
                status_rank = _ACTIVE_LIST_STATUS_RANK.get(str(row.get("status") or "").lower(), 0)
                pin_rank = 1 if (client_sid and self.active_turn_pins.get(client_sid) == w.gen) else 0
                active_rank = 1 if (active is not None and w.gen == active.gen) else 0
                score = (status_rank, pin_rank, active_rank)
                dedup_key = client_sid or f"__anon_{w.gen}_{len(order)}"
                prev = merged.get(dedup_key)
                if prev is None:
                    order.append(dedup_key)
                    merged[dedup_key] = (score, row)
                elif score > prev[0]:
                    merged[dedup_key] = (score, row)

        if self._closed:
            return
        if not any_ok:
            err_body = first_error if isinstance(first_error, dict) else {
                "code": -32003,
                "message": "session.active_list failed on all workers",
            }
            await self.send_to_client(json.dumps({"jsonrpc": "2.0", "id": req_id, "error": err_body}))
            return

        sessions_out = [merged[k][1] for k in order]
        await self.send_to_client(json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"sessions": sessions_out},
        }))

    async def close(self) -> None:
        self._closed = True
        self._accepted.set()
        for t in list(self._bg_tasks):
            if not t.done():
                t.cancel()
        self._bg_tasks.clear()
        for gen in list(self._upstreams.keys()):
            await self.detach_worker(gen)


class RolloverCapExceededError(RuntimeError):
    """Raised when rollover is blocked because max_draining_workers is already reached."""


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
        max_draining_workers: int = 1,
        drain_max_s: Optional[float] = None,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.bound_port: int = port
        # Always hold a single consistent session token across all worker generations.
        # Honor HERMES_DASHBOARD_SESSION_TOKEN (including file:<path>) when no explicit
        # session_token was passed (P0-2: launchd ai.hermes.serve :9119 token parity).
        if session_token:
            self.session_token = session_token
        elif os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN"):
            from hermes_cli.web_server_session_token import resolve_session_token
            self.session_token = resolve_session_token()
        else:
            self.session_token = secrets.token_hex(32)
        self.ssh_owner_nonce = ssh_owner_nonce
        self.ssh_lock_path = ssh_lock_path
        self.isolated = isolated
        self.open_profile = open_profile
        self.worker_cmd_builder = worker_cmd_builder
        self.worker_ready_timeout_s = worker_ready_timeout_s
        self.drain_poll_s = drain_poll_s
        self.drain_settle_s = drain_settle_s
        self.max_draining_workers = max(1, int(max_draining_workers))
        if drain_max_s is None:
            raw_max = os.environ.get("HERMES_SUPERVISOR_DRAIN_MAX_S", "1800")
            try:
                drain_max_s = max(5.0, float(raw_max))
            except ValueError:
                drain_max_s = 1800.0
        self.drain_max_s = float(drain_max_s)

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

    async def _worker_is_idle(self, worker: WorkerInstance) -> bool:
        """Query the worker's own /api/health/idle (busy_ledger + background jobs).

        Returns:
          - True if worker reports idle=True, or returns 404 (minimal mock worker without /api/health/idle).
          - False if worker reports idle=False, or probe fails/times out (fail-closed so active
            background turns / subagents are never killed prematurely).
        """
        if self._http_client is None:
            return True
        try:
            resp = await self._http_client.get(
                f"{worker.base_url}/api/health/idle",
                headers={"X-Hermes-Session-Token": self.session_token},
                timeout=3.0,
            )
            if resp.status_code == 404:
                return True
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    return data.get("idle") is not False
            return False
        except Exception:
            return False

    async def rollover(self, reason: str = "api") -> dict[str, Any]:
        """Spawn a new worker generation, health-verify it, atomically flip, and drain old."""
        async with self._rollover_lock:
            # Enforce max_draining_workers cap BEFORE spawning another worker (P0-5: RAM/WAL guard).
            draining_live = [
                w for w in self._workers.values()
                if w.draining and not w.retired and w.is_alive()
            ]
            if len(draining_live) >= self.max_draining_workers:
                await self._drain_sweep_once()
                draining_live = [
                    w for w in self._workers.values()
                    if w.draining and not w.retired and w.is_alive()
                ]
            if len(draining_live) >= self.max_draining_workers:
                gens = [w.gen for w in draining_live]
                msg = (
                    f"rollover blocked: {len(draining_live)} draining worker(s) already active "
                    f"(gens={gens}, cap={self.max_draining_workers})"
                )
                rec = {
                    "ts": round(time.time(), 2),
                    "reason": reason,
                    "ok": False,
                    "error": msg,
                    "active_gen": self.active_worker.gen if self.active_worker else None,
                    "draining_gens": gens,
                }
                self._rollover_history.append(rec)
                if len(self._rollover_history) > 20:
                    del self._rollover_history[: len(self._rollover_history) - 20]
                raise RolloverCapExceededError(msg)

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
        wall_now = time.time()
        draining = [w for w in list(self._workers.values()) if w.draining and not w.retired]
        for w in draining:
            if not w.is_alive():
                await self._retire_worker(w)
                continue
            # Hard drain deadline prevents a wedged worker from holding RAM/WAL forever.
            if w.draining_since is not None and (wall_now - w.draining_since) >= self.drain_max_s:
                _log.warning(
                    "Draining worker-gen-%s (pid=%s) exceeded drain_max_s=%.1fs; retiring",
                    w.gen, w.pid, self.drain_max_s,
                )
                await self._retire_worker(w)
                continue
            if self._has_inflight_work(w.gen):
                self._idle_since.pop(w.gen, None)
                continue
            if not await self._worker_is_idle(w):
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

    def _ws_ingress_auth_check(self, websocket: WebSocket) -> Optional[str]:
        """Fail-closed ingress check on WebSocket upgrades BEFORE accepting or forwarding.

        Returns None if the caller presents either:
          - a valid supervisor session token (constant-time verified here), OR
          - a delegated credential mechanism (one-use ticket or session cookie) that
            the upstream worker's authoritative auth gate will verify pre-accept.
        Returns a short rejection reason string if the connection must be rejected immediately.
        """
        tok = (websocket.headers.get("x-hermes-session-token") or "").strip()
        if not tok:
            auth = (websocket.headers.get("authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                tok = auth[7:].strip()
        if not tok:
            tok = (websocket.query_params.get("token") or "").strip()
        if tok:
            if secrets.compare_digest(tok, self.session_token):
                return None
            return "invalid_session_token"

        # Single-use ticket (query param, header, or Sec-WebSocket-Protocol subprotocol)
        if (websocket.query_params.get("ticket") or "").strip():
            return None
        if (websocket.headers.get("x-hermes-gateway-ticket") or "").strip():
            return None
        raw_proto = websocket.headers.get("sec-websocket-protocol") or ""
        if any(p.strip().startswith("hermes-gateway-ticket.") for p in raw_proto.split(",") if p.strip()):
            return None
        # Webapp session cookie (verified authoritatively by upstream worker pre-accept)
        if (websocket.headers.get("cookie") or "").strip():
            return None
        return "missing_credential"

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
            "protocol_version": SUPERVISOR_PROTOCOL_VERSION,
            "pid": os.getpid(),
            "host": self.host,
            "port": self.bound_port,
            "uptime_s": round(time.time() - self.started_at, 1),
            "disk_head": _read_disk_head_sha(),
            "active_gen": active.gen if active else None,
            "active_worker_pid": active.pid if active else None,
            "active_worker_port": active.port if active else None,
            "active_worker_commit": active.commit if active else "",
            "worker_started_at": round(active.started_at, 2) if active else None,
            "max_draining_workers": self.max_draining_workers,
            "drain_max_s": self.drain_max_s,
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
                    "protocol_version": SUPERVISOR_PROTOCOL_VERSION,
                    "pid": os.getpid(),
                    "active_gen": active.gen,
                    "active_worker_pid": active.pid,
                    "active_worker_commit": active.commit,
                    "worker_started_at": round(active.started_at, 2),
                    "max_draining_workers": self.max_draining_workers,
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
        except RolloverCapExceededError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "supervisor": self.status_payload()},
                status_code=409,
            )
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

    @staticmethod
    def _extract_ws_headers_and_subprotocols(websocket: WebSocket) -> tuple[dict[str, str], list[str]]:
        extra_headers: dict[str, str] = {}
        for k in (
            "x-hermes-session-token",
            "authorization",
            "cookie",
            "origin",
            "x-hermes-gateway-ticket",
        ):
            val = websocket.headers.get(k)
            if val:
                extra_headers[k] = val
        raw_proto = websocket.headers.get("sec-websocket-protocol") or ""
        subprotocols = [p.strip() for p in raw_proto.split(",") if p.strip()]
        return extra_headers, subprotocols

    async def _handle_api_ws(self, websocket: WebSocket) -> None:
        reject_reason = self._ws_ingress_auth_check(websocket)
        if reject_reason is not None:
            _log.warning("/api/ws ingress auth rejected: %s", reject_reason)
            await websocket.close(code=4401, reason=f"auth: {reject_reason}")
            return
        extra_headers, subprotocols = self._extract_ws_headers_and_subprotocols(websocket)
        qs = urlencode(list(websocket.query_params.multi_items())) if websocket.query_params else ""
        mux = ClientWsMux(self, websocket, qs, extra_headers, subprotocols=subprotocols)
        self._clients.add(mux)
        try:
            await mux.run()
        finally:
            self._clients.discard(mux)

    async def _handle_generic_ws_tunnel(self, websocket: WebSocket) -> None:
        reject_reason = self._ws_ingress_auth_check(websocket)
        if reject_reason is not None:
            _log.warning("%s ingress auth rejected: %s", websocket.url.path, reject_reason)
            await websocket.close(code=4401, reason=f"auth: {reject_reason}")
            return
        worker = self.active_worker
        if worker is None or not worker.is_alive():
            await websocket.close(code=1013, reason="no active worker")
            return
        # SECURITY (P0-1): Forward client credentials verbatim; NEVER inject self.session_token.
        extra_headers, subprotocols = self._extract_ws_headers_and_subprotocols(websocket)
        path = websocket.url.path
        qs = urlencode(list(websocket.query_params.multi_items())) if websocket.query_params else ""
        url = f"{worker.ws_base_url}{path}" + (f"?{qs}" if qs else "")

        connect_kwargs: dict[str, Any] = {
            "additional_headers": extra_headers,
            "max_size": 64 * 1024 * 1024,
            "ping_interval": 20,
            "ping_timeout": 20,
        }
        if subprotocols:
            connect_kwargs["subprotocols"] = subprotocols

        self._secondary_ws_counts[worker.gen] = self._secondary_ws_counts.get(worker.gen, 0) + 1
        accepted = False
        try:
            async with websockets.connect(url, **connect_kwargs) as up:
                await websocket.accept(subprotocol=getattr(up, "subprotocol", None))
                accepted = True

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
            code = 1011
            if not accepted and ("401" in str(exc) or "403" in str(exc) or "4401" in str(exc)):
                code = 4401
            try:
                await websocket.close(code=code)
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

        async def _safe_sighup_rollover() -> None:
            try:
                await sup.rollover(reason="sighup")
            except RolloverCapExceededError as exc:
                _log.warning("SIGHUP rollover skipped: %s", exc)
            except Exception as exc:
                _log.error("SIGHUP rollover failed: %s", exc)

        if hasattr(signal, "SIGHUP"):
            try:
                loop.add_signal_handler(
                    signal.SIGHUP,
                    lambda: asyncio.create_task(_safe_sighup_rollover()),
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
