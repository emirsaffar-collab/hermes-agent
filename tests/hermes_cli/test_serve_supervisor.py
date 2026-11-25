"""End-to-end polarity tests for the Blue-Green Front-Door Supervisor (serve_supervisor.py).

Exercises real loopback TCP sockets, real worker subprocesses, real WebSockets,
JSON-RPC 2.0 multiplexing (in-flight turn pinning, srq-* approval routing, idle session
handoff, synthetic hello replay), SSE streaming, secondary WS tunnels, failed-rollover
safety, SIGHUP rollover, and clean orphan-free shutdown.
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

from hermes_cli.serve_supervisor import ServeSupervisor


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
        "srq_replies": [],
        "plugins_activated": 0,
        "active_turns": set(),
    }

    async def health(request: Request):
        return JSONResponse({
            "status": "ok",
            "commit": COMMIT,
            "gen": args.gen,
            "pid": os.getpid(),
            "token_len": len(TOKEN),
        })

    async def health_idle(request: Request):
        return JSONResponse({"idle": len(STATE["active_turns"]) == 0, "gen": args.gen})

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
        if ws.headers.get("x-hermes-session-token") != TOKEN:
            await ws.close(code=4401)
            return
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

                if method == "worker.whoami":
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "gen": args.gen,
                            "hello_seen": STATE["hello_seen"],
                            "srq_replies": STATE["srq_replies"],
                        },
                    }))
                    continue

                if method == "prompt.send":
                    sid = params.get("session_id", "s-default")
                    need_approval = bool(params.get("require_approval"))
                    STATE["active_turns"].add(sid)
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {"accepted": True, "gen": args.gen, "session_id": sid},
                    }))
                    await ws.send_text(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "message.start",
                        "params": {"session_id": sid, "gen": args.gen},
                    }))

                    async def _run_turn(session_id: str, require_srq: bool):
                        try:
                            await ws.send_text(json.dumps({
                                "jsonrpc": "2.0",
                                "method": "message.delta",
                                "params": {"session_id": session_id, "text": f"hello-from-gen-{args.gen}", "gen": args.gen},
                            }))
                            srq_res = None
                            if require_srq:
                                srq_id = f"srq-{args.gen}-{session_id}"
                                fut = asyncio.get_running_loop().create_future()
                                waiters[srq_id] = fut
                                await ws.send_text(json.dumps({
                                    "jsonrpc": "2.0",
                                    "id": srq_id,
                                    "method": "approval.request",
                                    "params": {"session_id": session_id, "command": "echo hi", "gen": args.gen},
                                }))
                                srq_res = await asyncio.wait_for(fut, timeout=10.0)
                            await ws.send_text(json.dumps({
                                "jsonrpc": "2.0",
                                "method": "message.complete",
                                "params": {
                                    "session_id": session_id,
                                    "gen": args.gen,
                                    "approved": bool((srq_res or {}).get("approved")) if require_srq else None,
                                },
                            }))
                        finally:
                            STATE["active_turns"].discard(session_id)

                    asyncio.create_task(_run_turn(sid, need_approval))
                    continue

                await ws.send_text(json.dumps({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"gen": args.gen, "method": method},
                }))
        except WebSocketDisconnect:
            pass

    async def ws_pty(ws: WebSocket):
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


def test_blue_green_rollover_mid_stream_srq_idle_handoff_and_reap(tmp_path: Path):
    """Full zero-downtime blue-green lifecycle:
    1) Session A streams on gen-1 and blocks on srq-1-sessA approval.
    2) Unauthenticated rollover returns 401; authenticated rollover flips to gen-2 (zero WS drop).
    3) Idle Session B immediately runs on gen-2 (with replayed hello).
    4) Client answers srq-1-sessA -> routes strictly back to gen-1 -> Session A completes.
    5) Drain reaper automatically SIGTERMs gen-1 while client WS stays alive.
    6) Next turn on Session A transparently runs on gen-2.
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
                # Health shows gen 1
                h1 = (await http.get("/api/health")).json()
                assert h1["commit"] == "commitgen01abcd"
                assert h1["supervisor"]["active_gen"] == 1

                # Unauthenticated rollover MUST fail closed with 401
                unauth = await http.post("/api/supervisor/rollover")
                assert unauth.status_code == 401

                # Connect ONE persistent Electron-style WebSocket
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/ws",
                    additional_headers={"X-Hermes-Session-Token": "test-secret-token-123"},
                ) as ws:
                    # Handshake hello
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": "h1", "method": "hello", "params": {"client": "desktop"}}))
                    hello_resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    assert hello_resp["id"] == "h1"
                    assert hello_resp["result"]["gen"] == 1

                    # Start Session A turn requiring srq-* approval on gen-1
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": "pA1",
                        "method": "prompt.send",
                        "params": {"session_id": "sessA", "text": "long task", "require_approval": True},
                    }))

                    # Collect frames until we receive server-initiated srq-1-sessA
                    srq_id = None
                    seen_gen1_delta = False
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("method") == "message.delta" and frame["params"]["session_id"] == "sessA":
                            assert frame["params"]["gen"] == 1
                            seen_gen1_delta = True
                        if frame.get("method") == "approval.request":
                            srq_id = frame["id"]
                            assert srq_id == "srq-1-sessA"
                            break
                    assert seen_gen1_delta and srq_id == "srq-1-sessA"

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

                    # /api/health IMMEDIATELY reflects gen-2 while gen-1 is draining
                    h2 = (await http.get("/api/health")).json()
                    assert h2["commit"] == "commitgen02abcd"
                    assert h2["supervisor"]["active_gen"] == 2
                    assert h2["supervisor"]["draining_count"] == 1

                    # Plugin activation fans out to BOTH active (gen-2) AND draining (gen-1)
                    fan = (await http.post(
                        "/api/dashboard/agent-plugins/activate",
                        headers={"X-Hermes-Session-Token": "test-secret-token-123"},
                    )).json()
                    gens_hit = sorted(r["gen"] for r in fan["fanout"])
                    assert gens_hit == [1, 2]

                    # Idle Session B starts a turn -> hands off IMMEDIATELY to gen-2!
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": "pB1",
                        "method": "prompt.send",
                        "params": {"session_id": "sessB", "text": "quick ask", "require_approval": False},
                    }))
                    sess_b_done = False
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("id") == "pB1":
                            assert frame["result"]["gen"] == 2
                        if frame.get("method") == "message.complete" and frame["params"]["session_id"] == "sessB":
                            assert frame["params"]["gen"] == 2
                            sess_b_done = True
                            break
                    assert sess_b_done

                    # Verify synthetic hello was replayed transparently to gen-2!
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": "who2", "method": "worker.whoami", "params": {}}))
                    who2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    assert who2["id"] == "who2"
                    assert who2["result"]["gen"] == 2
                    assert who2["result"]["hello_seen"] is True

                    # gen-1 MUST still be alive (Session A still waiting on srq-1-sessA)
                    assert w1.is_alive()

                    # Now answer srq-1-sessA -> MUST route strictly back to gen-1!
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": srq_id,
                        "result": {"approved": True},
                    }))
                    sess_a_done = False
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("method") == "message.complete" and frame["params"]["session_id"] == "sessA":
                            assert frame["params"]["gen"] == 1
                            assert frame["params"]["approved"] is True
                            sess_a_done = True
                            break
                    assert sess_a_done

                    # Drain reaper MUST automatically reap gen-1 now that Session A finished!
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and 1 in sup._workers:
                        await asyncio.sleep(0.05)
                    assert 1 not in sup._workers
                    assert not w1.is_alive()

                    # Same persistent WS is STILL ALIVE: Session A's next turn runs on gen-2!
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": "pA2",
                        "method": "prompt.send",
                        "params": {"session_id": "sessA", "text": "follow-up", "require_approval": False},
                    }))
                    sess_a2_done = False
                    for _ in range(10):
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        if frame.get("id") == "pA2":
                            assert frame["result"]["gen"] == 2
                        if frame.get("method") == "message.complete" and frame["params"]["session_id"] == "sessA":
                            assert frame["params"]["gen"] == 2
                            sess_a2_done = True
                            break
                    assert sess_a2_done

                # Verify SSE streaming + secondary WS (/api/pty) both work on gen-2
                async with http.stream("GET", "/api/stream") as r:
                    assert r.status_code == 200
                    chunks = [line async for line in r.aiter_lines() if line]
                    assert chunks == ["data: gen=2 chunk=0", "data: gen=2 chunk=1", "data: gen=2 chunk=2"]

                async with websockets.connect(f"ws://127.0.0.1:{port}/api/pty") as pty_ws:
                    await pty_ws.send("ls")
                    assert await asyncio.wait_for(pty_ws.recv(), timeout=5) == "pty-gen-2:ls"

                # Confirm gen-1 PID is truly gone from OS
                with pytest.raises(OSError):
                    os.kill(w1_pid, 0)
        finally:
            await _stop_supervisor_server(sup, srv, task)
            # Every worker must be reaped after supervisor shutdown
            assert len(sup._workers) == 0

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
                assert (await http.get("/api/health")).json()["supervisor"]["active_gen"] == 1
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
