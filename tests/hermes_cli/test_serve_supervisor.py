"""End-to-end contract & polarity tests for the Blue-Green Front-Door Supervisor (serve_supervisor.py).

Exercises real loopback TCP sockets, real worker subprocesses, real WebSockets,
real tui_gateway JSON-RPC 2.0 wire envelopes (method="event" + params.type,
client.capabilities, session.create/session.resume cross-gen rehydration, request.cancel),
WS ingress auth (P0-1), HERMES_DASHBOARD_SESSION_TOKEN=file:<path> resolution (P0-2),
max_draining_workers=1 cap + /api/health/idle busy_ledger gating + drain_max_s deadline (P0-5),
SSE streaming, secondary WS tunnels, failed-rollover safety, and SIGHUP rollover.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import InvalidStatus

from hermes_cli.serve_supervisor import (
    SUPERVISOR_PROTOCOL_VERSION,
    ServeSupervisor,
    _extract_request_cancel_id,
    _is_session_scoped_method,
    _is_terminal_notification,
    _is_turn_start_notification,
    _strip_single_use_ticket_qs,
)


_FAKE_WORKER_SCRIPT = textwrap.dedent(
    """
    import argparse
    import asyncio
    import json
    import os
    import sys
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket, WebSocketDisconnect

    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", type=int, required=True)
    ap.add_argument("--token-fd", type=int, required=True)
    ap.add_argument("--fail-boot", action="store_true")
    ap.add_argument("--commit-prefix", default="commitgen")
    ap.add_argument("--one-shot-ticket", default="")
    args = ap.parse_args()

    raw = b""
    try:
        while True:
            chunk = os.read(args.token_fd, 4096)
            if not chunk:
                break
            raw += chunk
    finally:
        try:
            os.close(args.token_fd)
        except OSError:
            pass
    TOKEN = raw.decode("utf-8").strip()

    if args.fail_boot:
        sys.stderr.write(f"simulated boot failure for gen={args.gen}\\n")
        sys.stderr.flush()
        sys.exit(42)

    COMMIT = f"{args.commit_prefix}{args.gen:02d}abcd"
    STATE = {
        "hello_seen": False,
        "capabilities": {},
        "srq_replies": [],
        "plugins_activated": 0,
        "active_turns": set(),
        "bg_busy": False,
        "live_sessions": {},  # runtime_sid -> stored_key
        "session_seq": 0,
        "ticket_used": False,
        "ws_queries_seen": [],
    }

    def _ws_auth_ok(ws: WebSocket) -> bool:
        tok = (ws.headers.get("x-hermes-session-token") or "").strip()
        if not tok:
            tok = (ws.query_params.get("token") or "").strip()
        if tok and tok == TOKEN:
            return True
        tkt = (ws.query_params.get("ticket") or "").strip()
        if tkt and args.one_shot_ticket and tkt == args.one_shot_ticket and not STATE["ticket_used"]:
            STATE["ticket_used"] = True
            return True
        return False

    async def health(request: Request):
        return JSONResponse({
            "status": "ok",
            "commit": COMMIT,
            "gen": args.gen,
            "pid": os.getpid(),
            "token_len": len(TOKEN),
        })

    async def health_idle(request: Request):
        idle = (len(STATE["active_turns"]) == 0) and (not STATE["bg_busy"])
        return JSONResponse({"idle": idle, "gen": args.gen, "bg_busy": STATE["bg_busy"]})

    async def set_bg_busy(request: Request):
        if request.headers.get("x-hermes-session-token") != TOKEN:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        STATE["bg_busy"] = bool(body.get("busy"))
        return JSONResponse({"ok": True, "gen": args.gen, "bg_busy": STATE["bg_busy"]})

    async def plugins_activate(request: Request):
        STATE["plugins_activated"] += 1
        return JSONResponse({"ok": True, "gen": args.gen, "count": STATE["plugins_activated"]})

    async def sse_stream(request: Request):
        async def _gen():
            for idx in range(3):
                yield f"data: gen={args.gen} chunk={idx}\\n\\n".encode("utf-8")
                await asyncio.sleep(0.02)
        return StreamingResponse(_gen(), media_type="text/event-stream")

    async def echo_http(request: Request):
        return JSONResponse({
            "gen": args.gen,
            "path": request.url.path,
            "method": request.method,
            "auth_ok": request.headers.get("x-hermes-session-token") == TOKEN,
        })

    async def ws_api(ws: WebSocket):
        if not _ws_auth_ok(ws):
            await ws.close(code=4401)
            return
        STATE["ws_queries_seen"].append(str(ws.url.query))
        await ws.accept()
        waiters = {}
        try:
            while True:
                raw_txt = await ws.receive_text()
                msg = json.loads(raw_txt)
                # Reply to our server-initiated srq-* request
                if "id" in msg and "method" not in msg:
                    rid = msg["id"]
                    STATE["srq_replies"].append({"id": rid, "result": msg.get("result"), "gen": args.gen})
                    fut = waiters.pop(rid, None)
                    if fut and not fut.done():
                        fut.set_result(msg.get("result"))
                    continue

                method = msg.get("method", "")
                params = msg.get("params") or {}
                rid = msg.get("id")

                if method == "hello":
                    STATE["hello_seen"] = True
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"gen": args.gen, "commit": COMMIT},
                    }))
                    continue

                if method == "client.capabilities":
                    for k, v in params.items():
                        STATE["capabilities"][k] = v
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"ok": True, "gen": args.gen},
                    }))
                    continue

                if method == "worker.whoami":
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "gen": args.gen,
                            "hello_seen": STATE["hello_seen"],
                            "capabilities": STATE["capabilities"],
                            "srq_replies": STATE["srq_replies"],
                            "ws_queries_seen": STATE["ws_queries_seen"],
                        },
                    }))
                    continue

                if method == "session.create":
                    STATE["session_seq"] += 1
                    rt_sid = f"rt-g{args.gen}-{STATE['session_seq']}"
                    stored = params.get("stored_session_id") or f"stored-{STATE['session_seq']}"
                    STATE["live_sessions"][rt_sid] = stored
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "session_id": rt_sid,
                            "stored_session_id": stored,
                            "gen": args.gen,
                        },
                    }))
                    continue

                if method == "session.resume":
                    STATE["session_seq"] += 1
                    stored = str(params.get("session_id") or "")
                    rt_sid = f"rt-g{args.gen}-res-{STATE['session_seq']}"
                    STATE["live_sessions"][rt_sid] = stored
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "session_id": rt_sid,
                            "resumed": stored,
                            "session_key": stored,
                            "messages_omitted": bool(params.get("omit_messages")),
                            "gen": args.gen,
                        },
                    }))
                    continue

                if method == "session.active_list":
                    current_sid = str(params.get("current_session_id") or "")
                    rows = []
                    for rt_sid, stored in STATE["live_sessions"].items():
                        rows.append({
                            "id": rt_sid,
                            "session_key": stored,
                            "current": rt_sid == current_sid,
                            "status": "working" if rt_sid in STATE["active_turns"] else "idle",
                            "gen": args.gen,
                        })
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"sessions": rows},
                    }))
                    continue

                if method == "process.list":
                    sid = str(params.get("session_id") or "")
                    if sid not in STATE["live_sessions"]:
                        await ws.send_text(json.dumps({
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {"code": 5010, "message": f"session {sid} not in memory"},
                        }))
                        continue
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"processes": [], "gen": args.gen, "session_id": sid},
                    }))
                    continue

                if method in ("prompt.submit", "prompt.send"):
                    sid = params.get("session_id", "")
                    if params.get("strict_session") and sid not in STATE["live_sessions"]:
                        await ws.send_text(json.dumps({
                            "jsonrpc": "2.0",
                            "id": rid,
                            "error": {"code": 4001, "message": f"session not found: {sid}"},
                        }))
                        continue
                    need_approval = bool(params.get("require_approval"))
                    cancel_srq = bool(params.get("cancel_srq"))
                    hold_s = float(params.get("hold_s") or 0.0)
                    STATE["active_turns"].add(sid)
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"accepted": True, "gen": args.gen, "session_id": sid},
                    }))
                    # Emit REAL tui_gateway/server.py wire envelope: method="event", params.type=...
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {"type": "message.start", "session_id": sid, "gen": args.gen},
                    }))

                    async def _run_turn(session_id: str, require_srq: bool, do_cancel: bool, hold_for: float):
                        try:
                            if hold_for > 0:
                                await asyncio.sleep(hold_for)
                            await ws.send_text(json.dumps({
                                "jsonrpc": "2.0",
                                "method": "event",
                                "params": {
                                    "type": "message.delta",
                                    "session_id": session_id,
                                    "text": f"hello-from-gen-{args.gen}",
                                    "gen": args.gen,
                                },
                            }))
                            srq_res = None
                            if require_srq:
                                srq_caps = STATE["capabilities"].get("server_requests") or []
                                if "approval" not in srq_caps:
                                    # Real tui_gateway behavior: silently fails approval if capability missing!
                                    await ws.send_text(json.dumps({
                                        "jsonrpc": "2.0",
                                        "method": "event",
                                        "params": {
                                            "type": "error",
                                            "session_id": session_id,
                                            "gen": args.gen,
                                            "message": "approval capability missing",
                                        },
                                    }))
                                    return
                                srq_id = f"srq-{args.gen}-{session_id}"
                                fut = asyncio.get_running_loop().create_future()
                                waiters[srq_id] = fut
                                await ws.send_text(json.dumps({
                                    "jsonrpc": "2.0",
                                    "id": srq_id,
                                    "method": "approval.request",
                                    "params": {"session_id": session_id, "command": "echo hi", "gen": args.gen},
                                }))
                                if do_cancel:
                                    await asyncio.sleep(0.25)
                                    waiters.pop(srq_id, None)
                                    await ws.send_text(json.dumps({
                                        "jsonrpc": "2.0",
                                        "method": "request.cancel",
                                        "params": {"id": srq_id, "reason": "timeout"},
                                    }))
                                else:
                                    srq_res = await asyncio.wait_for(fut, timeout=10.0)
                            await ws.send_text(json.dumps({
                                "jsonrpc": "2.0",
                                "method": "event",
                                "params": {
                                    "type": "message.complete",
                                    "session_id": session_id,
                                    "gen": args.gen,
                                    "approved": bool((srq_res or {}).get("approved")) if (require_srq and not do_cancel) else None,
                                },
                            }))
                        finally:
                            STATE["active_turns"].discard(session_id)

                    asyncio.create_task(_run_turn(sid, need_approval, cancel_srq, hold_s))
                    continue

                await ws.send_text(json.dumps({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"gen": args.gen, "method": method},
                }))
        except WebSocketDisconnect:
            pass

    async def ws_pty(ws: WebSocket):
        if not _ws_auth_ok(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            while True:
                data = await ws.receive_text()
                await ws.send_text(f"pty-gen-{args.gen}:{data}")
        except WebSocketDisconnect:
            pass

    app = Starlette(routes=[
        Route("/api/health", health, methods=["GET"]),
        Route("/api/health/idle", health_idle, methods=["GET"]),
        Route("/api/test/bg-busy", set_bg_busy, methods=["POST"]),
        Route("/api/dashboard/agent-plugins/activate", plugins_activate, methods=["POST"]),
        Route("/api/stream", sse_stream, methods=["GET"]),
        WebSocketRoute("/api/ws", ws_api),
        WebSocketRoute("/api/pty", ws_pty),
        Route("/{path:path}", echo_http, methods=["GET", "POST"]),
    ])

    async def _main():
        cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False)
        srv = uvicorn.Server(cfg)
        if not cfg.loaded:
            cfg.load()
        srv.lifespan = cfg.lifespan_class(cfg)
        with srv.capture_signals():
            await srv.startup()
            port = srv.servers[0].sockets[0].getsockname()[1]
            os.write(1, f"HERMES_BACKEND_READY port={port}\\n".encode("utf-8"))
            await srv.main_loop()
            if srv.started:
                await srv.shutdown()

    asyncio.run(_main())
    """
)


async def _start_supervisor_server(sup: ServeSupervisor):
    await sup.start_runtime()
    app = sup.build_app()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", access_log=False)
    srv = uvicorn.Server(cfg)
    if not cfg.loaded:
        cfg.load()
    srv.lifespan = cfg.lifespan_class(cfg)
    await srv.startup()
    sup.bound_port = srv.servers[0].sockets[0].getsockname()[1]
    task = asyncio.create_task(srv.main_loop())
    return srv, task, sup.bound_port


async def _stop_supervisor_server(sup: ServeSupervisor, srv: uvicorn.Server, task: asyncio.Task):
    srv.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except Exception:
        task.cancel()
    if srv.started:
        await srv.shutdown()
    await sup.shutdown_runtime()


def test_p0_1_ws_ingress_auth_rejects_unauth_and_strips_ticket_on_rollover(tmp_path: Path):
    """P0-1 polarity: unauthenticated and wrong-token WS clients MUST be rejected pre-accept
    on BOTH /api/ws and /api/pty; single-use ?ticket= works on initial connect and is stripped
    on rollover so gen-2 does not reject a replayed ticket."""
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(_FAKE_WORKER_SCRIPT, encoding="utf-8")

    async def _scenario():
        sup = ServeSupervisor(
            host="127.0.0.1",
            port=0,
            session_token="strict-secret-token-xyz",
            worker_cmd_builder=lambda gen, fd: [
                sys.executable,
                str(worker_py),
                "--gen", str(gen),
                "--token-fd", str(fd),
                "--one-shot-ticket", "one-shot-ticket-abc",
            ],
            drain_poll_s=0.1,
            drain_settle_s=0.1,
        )
        srv, task, port = await _start_supervisor_server(sup)
        try:
            # 1. Unauthenticated /api/ws MUST be rejected pre-accept (HTTP 403)
            with pytest.raises(InvalidStatus) as exc_ws_unauth:
                async with websockets.connect(f"ws://127.0.0.1:{port}/api/ws"):
                    pass
            assert exc_ws_unauth.value.response.status_code == 403

            # 2. Wrong token /api/ws MUST be rejected pre-accept (HTTP 403)
            with pytest.raises(InvalidStatus) as exc_ws_bad:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/ws",
                    additional_headers={"X-Hermes-Session-Token": "wrong-token"},
                ):
                    pass
            assert exc_ws_bad.value.response.status_code == 403

            # 3. Unauthenticated /api/pty MUST be rejected pre-accept (HTTP 403)
            with pytest.raises(InvalidStatus) as exc_pty_unauth:
                async with websockets.connect(f"ws://127.0.0.1:{port}/api/pty"):
                    pass
            assert exc_pty_unauth.value.response.status_code == 403

            # 4. Wrong token /api/pty MUST be rejected pre-accept (HTTP 403)
            with pytest.raises(InvalidStatus) as exc_pty_bad:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/pty",
                    additional_headers={"X-Hermes-Session-Token": "wrong-token"},
                ):
                    pass
            assert exc_pty_bad.value.response.status_code == 403

            # 5. Authenticated /api/pty succeeds
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/api/pty",
                additional_headers={"X-Hermes-Session-Token": "strict-secret-token-xyz"},
            ) as pty_ws:
                await pty_ws.send("whoami")
                assert await asyncio.wait_for(pty_ws.recv(), timeout=5) == "pty-gen-1:whoami"

            # 6. Single-use ?ticket= works on gen-1, and post-rollover gen-2 strips ?ticket=
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/ws?ticket=one-shot-ticket-abc&profile=default"
                ) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": "w1", "method": "worker.whoami", "params": {}}))
                    r1 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    assert r1["result"]["gen"] == 1
                    assert "ticket=one-shot-ticket-abc" in r1["result"]["ws_queries_seen"][0]

                    ro = await http.post(
                        "/api/supervisor/rollover",
                        headers={"X-Hermes-Session-Token": "strict-secret-token-xyz"},
                    )
                    assert ro.status_code == 200

                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": "w2", "method": "worker.whoami", "params": {}}))
                    r2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    assert r2["result"]["gen"] == 2
                    # Single-use ticket MUST have been stripped on gen-2 reconnect; profile preserved!
                    assert "ticket=" not in r2["result"]["ws_queries_seen"][0]
                    assert "profile=default" in r2["result"]["ws_queries_seen"][0]
        finally:
            await _stop_supervisor_server(sup, srv, task)

    asyncio.run(_scenario())


def test_p0_2_env_file_session_token_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """P0-2: ServeSupervisor(session_token=None) resolves HERMES_DASHBOARD_SESSION_TOKEN=file:<path>
    so launchd ai.hermes.serve (:9119) accepts the disk token instead of returning 401."""
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(_FAKE_WORKER_SCRIPT, encoding="utf-8")
    tok_file = tmp_path / "dashboard-session-token"
    tok_file.write_text("launchd-disk-token-9119\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", f"file:{tok_file}")

    async def _scenario():
        sup = ServeSupervisor(
            host="127.0.0.1",
            port=0,
            session_token=None,
            worker_cmd_builder=lambda gen, fd: [
                sys.executable, str(worker_py), "--gen", str(gen), "--token-fd", str(fd)
            ],
        )
        assert sup.session_token == "launchd-disk-token-9119"
        srv, task, port = await _start_supervisor_server(sup)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                bad = await http.get("/api/supervisor/status", headers={"X-Hermes-Session-Token": "wrong"})
                assert bad.status_code == 401
                ok = await http.get(
                    "/api/supervisor/status",
                    headers={"X-Hermes-Session-Token": "launchd-disk-token-9119"},
                )
                assert ok.status_code == 200
                payload = ok.json()
                assert payload["protocol_version"] == SUPERVISOR_PROTOCOL_VERSION
                assert payload["active_gen"] == 1
                assert payload["worker_started_at"] is not None
        finally:
            await _stop_supervisor_server(sup, srv, task)

    asyncio.run(_scenario())


def test_blue_green_rollover_mid_stream_srq_idle_handoff_and_reap(tmp_path: Path):
    """Full P0-3 + P0-4 zero-downtime blue-green contract using REAL tui_gateway wire protocol:
    1) Client sends client.capabilities (server_requests=["approval"]) and creates Session A & Session B on gen-1.
    2) Session A streams on gen-1 (method="event", params.type="message.start"/"message.delta") and pauses on srq-1-*.
    3) Rollover flips to gen-2 (health advertises protocol_version=2 & worker_started_at).
    4) Idle Session B (created on gen-1, NOT in gen-2 RAM!) sends prompt.submit with strict_session=True + require_approval=True:
       - Supervisor transparently rehydrates Session B on gen-2 via session.resume (omit_messages=True),
       - Supervisor replays client.capabilities to gen-2 so gen-2 CAN emit srq-2-*,
       - Supervisor aliases gen-2's worker_sid <-> client_sid across events, responses, AND srq-2-*!
    5) Client answers srq-1-* -> routes back to gen-1 -> Session A emits event/message.complete -> pin clears -> gen-1 reaps!
    6) Session A's next turn transparently rehydrates onto gen-2 and completes on gen-2!
    """
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(_FAKE_WORKER_SCRIPT, encoding="utf-8")

    async def _scenario():
        sup = ServeSupervisor(
            host="127.0.0.1",
            port=0,
            session_token="test-secret-token-123",
            worker_cmd_builder=lambda gen, fd: [
                sys.executable, str(worker_py), "--gen", str(gen), "--token-fd", str(fd)
            ],
            drain_poll_s=0.1,
            drain_settle_s=0.15,
        )
        srv, task, port = await _start_supervisor_server(sup)
        try:
            w1 = sup.active_worker
            assert w1 is not None and w1.gen == 1
            w1_pid = w1.pid

            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                h1 = (await http.get("/api/health")).json()
                assert h1["commit"] == "commitgen01abcd"
                assert h1["supervisor"]["active_gen"] == 1
                assert h1["supervisor"]["protocol_version"] == SUPERVISOR_PROTOCOL_VERSION
                assert isinstance(h1["supervisor"]["worker_started_at"], (int, float))

                # Unauthenticated rollover MUST fail closed with 401
                assert (await http.post("/api/supervisor/rollover")).status_code == 401

                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/ws",
                    additional_headers={"X-Hermes-Session-Token": "test-secret-token-123"},
                ) as ws:
                    # Real tui_gateway handshake: client.capabilities (NO fake hello!)
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": "cap1",
                        "method": "client.capabilities",
                        "params": {"server_requests": ["approval", "clarify"]},
                    }))
                    cap_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    assert cap_resp["id"] == "cap1" and cap_resp["result"]["gen"] == 1

                    # Create Session A & Session B on gen-1
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": "cA", "method": "session.create",
                        "params": {"stored_session_id": "stored-A"},
                    }))
                    cA = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    sid_a = cA["result"]["session_id"]
                    assert sid_a.startswith("rt-g1-")

                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": "cB", "method": "session.create",
                        "params": {"stored_session_id": "stored-B"},
                    }))
                    cB = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    sid_b = cB["result"]["session_id"]
                    assert sid_b.startswith("rt-g1-")

                    # Start Session A turn requiring srq-* approval on gen-1
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": "pA1",
                        "method": "prompt.submit",
                        "params": {"session_id": sid_a, "text": "long task", "require_approval": True, "strict_session": True},
                    }))

                    srq_a_id = None
                    seen_gen1_delta = False
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("method") == "event" and frame["params"].get("type") == "message.delta":
                            assert frame["params"]["session_id"] == sid_a
                            assert frame["params"]["gen"] == 1
                            seen_gen1_delta = True
                        if frame.get("method") == "approval.request":
                            srq_a_id = frame["id"]
                            assert frame["params"]["session_id"] == sid_a
                            break
                    assert seen_gen1_delta and srq_a_id is not None

                    # ROLLOVER WHILE SESSION A IS MID-TURN!
                    ro = await http.post(
                        "/api/supervisor/rollover",
                        headers={"X-Hermes-Session-Token": "test-secret-token-123"},
                    )
                    assert ro.status_code == 200
                    ro_data = ro.json()
                    assert ro_data["ok"] is True
                    assert ro_data["rollover"]["old_gen"] == 1
                    assert ro_data["rollover"]["new_gen"] == 2
                    assert ro_data["rollover"]["draining_active_turns"] == 1

                    # Plugin activation fans out to BOTH active (gen-2) AND draining (gen-1)
                    fan = (await http.post(
                        "/api/dashboard/agent-plugins/activate",
                        headers={"X-Hermes-Session-Token": "test-secret-token-123"},
                    )).json()
                    assert sorted(r["gen"] for r in fan["fanout"]) == [1, 2]

                    # Idle Session B (created on gen-1!) runs on gen-2 with strict_session=True + require_approval=True!
                    # Proves BOTH cross-gen session.resume rehydration AND client.capabilities replay!
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": "pB1",
                        "method": "prompt.submit",
                        "params": {"session_id": sid_b, "text": "post-rollover ask", "require_approval": True, "strict_session": True},
                    }))
                    srq_b_id = None
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("id") == "pB1":
                            assert "error" not in frame, f"Cross-gen rehydrate failed: {frame}"
                            assert frame["result"]["gen"] == 2
                            assert frame["result"]["session_id"] == sid_b  # aliased back to client sid!
                        if frame.get("method") == "approval.request":
                            srq_b_id = frame["id"]
                            assert frame["params"]["gen"] == 2
                            assert frame["params"]["session_id"] == sid_b  # aliased back to client sid!
                            break
                    assert srq_b_id is not None

                    # Answer gen-2's srq-2-* -> Session B completes on gen-2!
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": srq_b_id, "result": {"approved": True}}))
                    sess_b_done = False
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("method") == "event" and frame["params"].get("type") == "message.complete":
                            assert frame["params"]["session_id"] == sid_b
                            assert frame["params"]["gen"] == 2
                            assert frame["params"]["approved"] is True
                            sess_b_done = True
                            break
                    assert sess_b_done

                    # Verify client.capabilities was replayed to gen-2
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": "who2", "method": "worker.whoami", "params": {}}))
                    who2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    assert who2["result"]["gen"] == 2
                    assert who2["result"]["capabilities"]["server_requests"] == ["approval", "clarify"]

                    # gen-1 MUST still be alive (Session A still waiting on srq_a_id)
                    assert w1.is_alive()

                    # Now answer gen-1's srq-1-* -> routes back to gen-1 -> Session A completes -> gen-1 drains!
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": srq_a_id, "result": {"approved": True}}))
                    sess_a_done = False
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("method") == "event" and frame["params"].get("type") == "message.complete":
                            assert frame["params"]["session_id"] == sid_a
                            assert frame["params"]["gen"] == 1
                            assert frame["params"]["approved"] is True
                            sess_a_done = True
                            break
                    assert sess_a_done

                    # Drain reaper MUST automatically reap gen-1 now that real event/message.complete fired!
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and 1 in sup._workers:
                        await asyncio.sleep(0.05)
                    assert 1 not in sup._workers
                    assert not w1.is_alive()

                    # Session A's NEXT turn transparently rehydrates onto gen-2 and completes on gen-2!
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": "pA2",
                        "method": "prompt.submit",
                        "params": {"session_id": sid_a, "text": "follow-up", "require_approval": False, "strict_session": True},
                    }))
                    sess_a2_done = False
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("id") == "pA2":
                            assert "error" not in frame
                            assert frame["result"]["gen"] == 2
                            assert frame["result"]["session_id"] == sid_a
                        if frame.get("method") == "event" and frame["params"].get("type") == "message.complete":
                            assert frame["params"]["session_id"] == sid_a
                            assert frame["params"]["gen"] == 2
                            sess_a2_done = True
                            break
                    assert sess_a2_done

                # Verify SSE streaming + authenticated /api/pty both work on gen-2
                async with http.stream("GET", "/api/stream") as r:
                    assert r.status_code == 200
                    chunks = [line async for line in r.aiter_lines() if line]
                    assert chunks == ["data: gen=2 chunk=0", "data: gen=2 chunk=1", "data: gen=2 chunk=2"]

                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/pty",
                    additional_headers={"X-Hermes-Session-Token": "test-secret-token-123"},
                ) as pty_ws:
                    await pty_ws.send("ls")
                    assert await asyncio.wait_for(pty_ws.recv(), timeout=5) == "pty-gen-2:ls"

                with pytest.raises(OSError):
                    os.kill(w1_pid, 0)
        finally:
            await _stop_supervisor_server(sup, srv, task)
            assert len(sup._workers) == 0

    asyncio.run(_scenario())


def test_p0_5_max_draining_cap_busy_ledger_gate_and_hard_drain_deadline(tmp_path: Path):
    """P0-5:
    1) max_draining_workers=1 returns HTTP 409 on second rollover while gen-1 is draining.
    2) /api/health/idle returning idle=False (e.g. background task in busy_ledger with zero WS pins)
       prevents premature kill of draining gen-1 until idle=True.
    3) Hard drain_max_s deadline reaps a permanently wedged draining worker.
    """
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(_FAKE_WORKER_SCRIPT, encoding="utf-8")

    async def _scenario():
        sup = ServeSupervisor(
            host="127.0.0.1",
            port=0,
            session_token="tok-cap-test",
            worker_cmd_builder=lambda gen, fd: [
                sys.executable, str(worker_py), "--gen", str(gen), "--token-fd", str(fd)
            ],
            drain_poll_s=0.1,
            drain_settle_s=0.15,
            max_draining_workers=1,
            drain_max_s=30.0,
        )
        srv, task, port = await _start_supervisor_server(sup)
        try:
            w1 = sup.active_worker
            assert w1 is not None
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                # Mark gen-1 busy in background (busy_ledger simulation, NO WS connected!)
                r_busy = await http.post(
                    "/api/test/bg-busy",
                    json={"busy": True},
                    headers={"X-Hermes-Session-Token": "tok-cap-test"},
                )
                assert r_busy.status_code == 200

                # First rollover succeeds (gen-1 -> draining, gen-2 -> active)
                ro1 = await http.post(
                    "/api/supervisor/rollover",
                    headers={"X-Hermes-Session-Token": "tok-cap-test"},
                )
                assert ro1.status_code == 200

                # Sleep past drain_settle_s -> gen-1 MUST STILL BE ALIVE because /api/health/idle is False!
                await asyncio.sleep(0.4)
                assert 1 in sup._workers and w1.is_alive()

                # Second rollover while gen-1 is still draining MUST be blocked with HTTP 409!
                ro2 = await http.post(
                    "/api/supervisor/rollover",
                    headers={"X-Hermes-Session-Token": "tok-cap-test"},
                )
                assert ro2.status_code == 409
                assert sup.active_worker is not None and sup.active_worker.gen == 2
                assert 3 not in sup._workers

                # Release gen-1 background work directly -> gen-1 MUST now drain & exit cleanly!
                async with httpx.AsyncClient(base_url=w1.base_url) as w1_http:
                    await w1_http.post(
                        "/api/test/bg-busy",
                        json={"busy": False},
                        headers={"X-Hermes-Session-Token": "tok-cap-test"},
                    )
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and 1 in sup._workers:
                    await asyncio.sleep(0.05)
                assert 1 not in sup._workers and not w1.is_alive()

                # Now test hard drain_max_s deadline: mark gen-2 permanently busy, shorten drain_max_s, rollover to gen-3
                w2 = sup.active_worker
                assert w2 is not None and w2.gen == 2
                await http.post(
                    "/api/test/bg-busy",
                    json={"busy": True},
                    headers={"X-Hermes-Session-Token": "tok-cap-test"},
                )
                sup.drain_max_s = 0.45
                ro3 = await http.post(
                    "/api/supervisor/rollover",
                    headers={"X-Hermes-Session-Token": "tok-cap-test"},
                )
                assert ro3.status_code == 200
                # Even though w2 stays bg_busy=True, drain_max_s=0.45s MUST retire w2!
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and 2 in sup._workers:
                    await asyncio.sleep(0.05)
                assert 2 not in sup._workers and not w2.is_alive()
        finally:
            await _stop_supervisor_server(sup, srv, task)

    asyncio.run(_scenario())


def test_request_cancel_clears_pending_srq_pin(tmp_path: Path):
    """If a draining worker cancels an unanswered srq-* via request.cancel, the supervisor
    clears pending_srq so the draining worker does not leak forever."""
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(_FAKE_WORKER_SCRIPT, encoding="utf-8")

    async def _scenario():
        sup = ServeSupervisor(
            host="127.0.0.1",
            port=0,
            session_token="tok-cancel-test",
            worker_cmd_builder=lambda gen, fd: [
                sys.executable, str(worker_py), "--gen", str(gen), "--token-fd", str(fd)
            ],
            drain_poll_s=0.1,
            drain_settle_s=0.1,
        )
        srv, task, port = await _start_supervisor_server(sup)
        try:
            w1 = sup.active_worker
            assert w1 is not None
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/ws",
                    additional_headers={"X-Hermes-Session-Token": "tok-cancel-test"},
                ) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": "cap1", "method": "client.capabilities",
                        "params": {"server_requests": ["approval"]},
                    }))
                    await asyncio.wait_for(ws.recv(), timeout=5)

                    # Start turn that emits srq-1-* and auto-cancels it after 250ms
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": "pC1", "method": "prompt.submit",
                        "params": {"session_id": "sess-cancel", "require_approval": True, "cancel_srq": True},
                    }))
                    # Wait for approval.request
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("method") == "approval.request":
                            break

                    # Rollover while srq is pending (do NOT answer srq!)
                    ro = await http.post(
                        "/api/supervisor/rollover",
                        headers={"X-Hermes-Session-Token": "tok-cancel-test"},
                    )
                    assert ro.status_code == 200

                    # Drain cancel + message.complete frames; gen-1 MUST auto-reap cleanly!
                    seen_cancel = False
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("method") == "request.cancel":
                            seen_cancel = True
                        if frame.get("method") == "event" and frame["params"].get("type") == "message.complete":
                            break
                    assert seen_cancel

                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and 1 in sup._workers:
                        await asyncio.sleep(0.05)
                    assert 1 not in sup._workers and not w1.is_alive()
        finally:
            await _stop_supervisor_server(sup, srv, task)

    asyncio.run(_scenario())


def test_failed_rollover_preserves_active_worker_and_ws(tmp_path: Path):
    """Negative polarity: if candidate worker-gen-2 crashes during boot,
    POST /api/supervisor/rollover returns 502, worker-gen-1 stays active, and client WS survives."""
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(_FAKE_WORKER_SCRIPT, encoding="utf-8")

    def _cmd(gen: int, fd: int) -> list[str]:
        base = [sys.executable, str(worker_py), "--gen", str(gen), "--token-fd", str(fd)]
        if gen == 2:
            base.append("--fail-boot")
        return base

    async def _scenario():
        sup = ServeSupervisor(
            host="127.0.0.1",
            port=0,
            session_token="tok-fail-test",
            worker_cmd_builder=_cmd,
            worker_ready_timeout_s=5.0,
        )
        srv, task, port = await _start_supervisor_server(sup)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/ws",
                    additional_headers={"X-Hermes-Session-Token": "tok-fail-test"},
                ) as ws:
                    ro = await http.post(
                        "/api/supervisor/rollover",
                        headers={"X-Hermes-Session-Token": "tok-fail-test"},
                    )
                    assert ro.status_code == 502
                    assert ro.json()["ok"] is False
                    assert sup.active_worker is not None and sup.active_worker.gen == 1

                    # Existing WS still serves RPCs on gen-1 with zero interruption
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": "still-alive", "method": "worker.whoami", "params": {}}))
                    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    assert resp["id"] == "still-alive"
                    assert resp["result"]["gen"] == 1
        finally:
            await _stop_supervisor_server(sup, srv, task)

    asyncio.run(_scenario())


def test_sighup_rollover_triggers_zero_downtime_cutover(tmp_path: Path):
    """Sending SIGHUP to the Front-Door Supervisor rolls over worker-gen-1 -> worker-gen-2."""
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(_FAKE_WORKER_SCRIPT, encoding="utf-8")

    async def _scenario():
        sup = ServeSupervisor(
            host="127.0.0.1",
            port=0,
            session_token="tok-sighup-test",
            worker_cmd_builder=lambda gen, fd: [
                sys.executable, str(worker_py), "--gen", str(gen), "--token-fd", str(fd)
            ],
            drain_poll_s=0.1,
            drain_settle_s=0.1,
        )
        srv, task, port = await _start_supervisor_server(sup)
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, lambda: asyncio.create_task(sup.rollover(reason="sighup")))
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                h1 = (await http.get("/api/health")).json()
                assert h1["supervisor"]["active_gen"] == 1
                assert h1["supervisor"]["protocol_version"] == SUPERVISOR_PROTOCOL_VERSION
                os.kill(os.getpid(), signal.SIGHUP)
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    cur = (await http.get("/api/health")).json()
                    if cur["supervisor"]["active_gen"] == 2 and cur["supervisor"]["draining_count"] == 0:
                        break
                    await asyncio.sleep(0.1)
                final = (await http.get("/api/health")).json()
                assert final["supervisor"]["active_gen"] == 2
                assert final["commit"] == "commitgen02abcd"
                assert final["supervisor"]["draining_count"] == 0
        finally:
            loop.remove_signal_handler(signal.SIGHUP)
            await _stop_supervisor_server(sup, srv, task)

    asyncio.run(_scenario())


def test_tui_gateway_wire_contract_drift_guard():
    """Drift guard: assert serve_supervisor helpers match REAL tui_gateway event envelopes
    and registered prompt/subagent methods so mock/prod protocol drift cannot recur."""
    # Real tui_gateway wraps events inside method="event", params={"type": ..., "session_id": ...}
    assert _is_turn_start_notification("event", {"type": "message.start", "session_id": "s1"})
    assert _is_terminal_notification("event", {"type": "message.complete", "session_id": "s1"})
    assert _is_terminal_notification("event", {"type": "error", "session_id": "s1"})
    assert _is_terminal_notification("event", {"type": "session.info", "session_id": "s1", "running": False})
    assert not _is_terminal_notification("event", {"type": "session.info", "session_id": "s1", "running": True})
    assert not _is_terminal_notification("event", {"type": "message.delta", "session_id": "s1"})
    assert _extract_request_cancel_id("request.cancel", {"id": "srq-42"}) == "srq-42"
    assert _strip_single_use_ticket_qs("ticket=secret123&profile=work") == "profile=work"

    # Every real tui_gateway prompt/subagent/process/tool session-scoped method must be session-scoped
    for m in (
        "prompt.submit",
        "prompt.background",
        "prompt.btw",
        "prompt.enqueue",
        "prompt.cancel",
        "subagent.steer",
        "subagent.kill",
        "process.list",
        "process.stop",
        "process.kill",
        "shell.exec",
        "cli.exec",
        "command.dispatch",
        "slash.exec",
        "rollback.list",
        "handoff.request",
        "terminal.resize",
        "agents.list",
    ):
        assert _is_session_scoped_method(m), f"{m} must be session-scoped"

    # Cross-check against real tui_gateway source files so upstream renames fail CI immediately
    root = Path(__file__).resolve().parents[2]
    server_src = (root / "tui_gateway" / "server.py").read_text(encoding="utf-8")
    assert '"method": "event"' in server_src
    srq_src = (root / "tui_gateway" / "server_requests.py").read_text(encoding="utf-8")
    assert '"request.cancel"' in srq_src
    voice_src = (root / "tui_gateway" / "methods_voice.py").read_text(encoding="utf-8")
    assert '"client.capabilities"' in voice_src


def test_active_list_merges_draining_and_rehydrated_sessions_and_routes_process_list(tmp_path: Path):
    """Regression: Desktop's 45s silent-turn probe calls ``session.active_list``.

    1. A live turn pinned on draining gen-1 MUST still appear with status='working'
       after rollover to gen-2 (prevents false "Hermes ended this turn without a reply.").
    2. An idle session transparently rehydrated onto gen-2 (with a different worker_sid)
       MUST have its row ``id`` translated back to client_sid and ``current`` recomputed.
    3. ``process.list`` MUST route to the pinned draining worker for active turns AND
       rehydrate cleanly on gen-2 for idle sessions instead of failing "not in memory".
    """
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(_FAKE_WORKER_SCRIPT, encoding="utf-8")

    async def _scenario():
        sup = ServeSupervisor(
            host="127.0.0.1",
            port=0,
            session_token="tok-active-list",
            worker_cmd_builder=lambda gen, fd: [
                sys.executable, str(worker_py), "--gen", str(gen), "--token-fd", str(fd)
            ],
            drain_poll_s=0.1,
            drain_settle_s=0.2,
        )
        srv, task, port = await _start_supervisor_server(sup)
        try:
            ws_url = f"ws://127.0.0.1:{port}/api/ws"
            headers = {"X-Hermes-Session-Token": "tok-active-list"}
            async with websockets.connect(ws_url, additional_headers=headers) as ws:
                # Create two sessions on gen-1: sid1 will hold an active turn across rollover;
                # sid2 stays idle and hands off to gen-2 after rollover.
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": "c1", "method": "session.create", "params": {"stored_session_id": "stored-a"}}))
                r1 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                sid1 = r1["result"]["session_id"]

                await ws.send(json.dumps({"jsonrpc": "2.0", "id": "c2", "method": "session.create", "params": {"stored_session_id": "stored-b"}}))
                r2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                sid2 = r2["result"]["session_id"]

                # Enable approval capability so sid1's turn pauses cleanly on gen-1
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": "cap",
                    "method": "client.capabilities",
                    "params": {"server_requests": ["approval"]},
                }))
                json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))

                # Start a turn on sid1 that waits for approval on gen-1
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": "p1",
                    "method": "prompt.submit",
                    "params": {"session_id": sid1, "require_approval": True, "strict_session": True},
                }))
                srq_id = None
                for _ in range(10):
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if ev.get("method") == "approval.request":
                        srq_id = ev["id"]
                        break
                assert srq_id is not None

                # Roll over to gen-2 while sid1 is mid-turn on draining gen-1
                rec = await sup.rollover(reason="test-active-list")
                assert rec["new_gen"] == 2
                w1 = sup.get_worker(1)
                assert w1 is not None and w1.draining

                # Send process.list for pinned sid1 -> MUST hit draining gen-1, NOT gen-2!
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": "pl1",
                    "method": "process.list",
                    "params": {"session_id": sid1},
                }))
                pl1 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert "result" in pl1, f"process.list on draining sid1 failed: {pl1}"
                assert pl1["result"]["gen"] == 1
                assert pl1["result"]["session_id"] == sid1

                # Send process.list for idle sid2 -> MUST rehydrate onto gen-2 and rewrite sid back!
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": "pl2",
                    "method": "process.list",
                    "params": {"session_id": sid2},
                }))
                pl2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert "result" in pl2, f"process.list on rehydrated sid2 failed: {pl2}"
                assert pl2["result"]["gen"] == 2
                assert pl2["result"]["session_id"] == sid2

                # Start a held turn on rehydrated sid2 (gen-2 assigns a DIFFERENT worker_sid!)
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": "p2",
                    "method": "prompt.submit",
                    "params": {"session_id": sid2, "hold_s": 0.6, "strict_session": True},
                }))
                got_start2 = False
                for _ in range(6):
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if ev.get("method") == "event" and ev.get("params", {}).get("type") == "message.start":
                        got_start2 = True
                        break
                assert got_start2

                # Now call session.active_list! Must include BOTH:
                # - sid1 (working on draining gen-1)
                # - sid2 (working on active gen-2, aliased BACK from gen-2's worker_sid -> client sid2!)
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": "al1",
                    "method": "session.active_list",
                    "params": {"current_session_id": sid2},
                }))
                al_resp = None
                for _ in range(10):
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if ev.get("id") == "al1":
                        al_resp = ev
                        break
                assert al_resp is not None and "result" in al_resp
                by_id = {row["id"]: row for row in al_resp["result"]["sessions"]}
                assert sid1 in by_id, f"Draining gen-1 live session {sid1} missing from active_list: {by_id}"
                assert by_id[sid1]["status"] == "working"
                assert by_id[sid1]["current"] is False
                assert sid2 in by_id, f"Rehydrated gen-2 session {sid2} missing/unaliased in active_list: {by_id}"
                assert by_id[sid2]["status"] == "working"
                assert by_id[sid2]["current"] is True

                # Complete sid1 cleanly so gen-1 drains
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": srq_id, "result": {"approved": True}}))
                done_sids = set()
                deadline = time.monotonic() + 5.0
                while len(done_sids) < 2 and time.monotonic() < deadline:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if ev.get("method") == "event" and ev.get("params", {}).get("type") == "message.complete":
                        done_sids.add(ev["params"]["session_id"])
                assert done_sids == {sid1, sid2}
        finally:
            await _stop_supervisor_server(sup, srv, task)

    asyncio.run(_scenario())


def test_forced_drain_timeout_emits_terminal_error_for_pinned_turns(tmp_path: Path):
    """When drain_max_s forcibly retires a draining worker with an active turn, the
    supervisor MUST emit a terminal ``event`` (type='error') for each pinned session
    instead of leaving Desktop silent until the 45s no-reply watchdog fires."""
    worker_py = tmp_path / "fake_worker.py"
    worker_py.write_text(_FAKE_WORKER_SCRIPT, encoding="utf-8")

    async def _scenario():
        sup = ServeSupervisor(
            host="127.0.0.1",
            port=0,
            session_token="tok-drain-err",
            worker_cmd_builder=lambda gen, fd: [
                sys.executable, str(worker_py), "--gen", str(gen), "--token-fd", str(fd)
            ],
            drain_poll_s=0.1,
            drain_settle_s=0.1,
            drain_max_s=0.4,
        )
        srv, task, port = await _start_supervisor_server(sup)
        try:
            ws_url = f"ws://127.0.0.1:{port}/api/ws"
            headers = {"X-Hermes-Session-Token": "tok-drain-err"}
            async with websockets.connect(ws_url, additional_headers=headers) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": "c1", "method": "session.create", "params": {"stored_session_id": "stored-kill"}}))
                sid = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))["result"]["session_id"]

                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": "p1",
                    "method": "prompt.submit",
                    "params": {"session_id": sid, "hold_s": 30.0},
                }))
                got_start = False
                for _ in range(5):
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                    if ev.get("method") == "event" and ev.get("params", {}).get("type") == "message.start":
                        got_start = True
                        break
                assert got_start

                await sup.rollover(reason="test-forced-retire")

                # Wait for drain_max_s to fire -> client must receive an explicit error event
                err_ev = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                    if (
                        ev.get("method") == "event"
                        and ev.get("params", {}).get("type") == "error"
                        and ev.get("params", {}).get("session_id") == sid
                    ):
                        err_ev = ev
                        break
                assert err_ev is not None, "Expected explicit error event on forced worker retirement"
                assert "retired" in err_ev["params"]["payload"]["message"].lower()
        finally:
            await _stop_supervisor_server(sup, srv, task)

    asyncio.run(_scenario())
