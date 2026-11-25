"""Tests for Wave 1/2 speed & 24/7 bulkhead architecture upgrades (MARKER-GUARD-SELFTEST):
- W1-C: Cron scheduler two-pool bulkhead isolation (interactive_chat vs background_other)
- W2-A: _FAST_ACCEPT_ABSENT_RE polarity fix + parallel fail-closed pre_tool_call dispatch (deny > ask > allow)
- W2-B: Vertex regional TTFB stale cap lowered to 75s
"""

from __future__ import annotations

import threading
import time

from agent import chat_completion_stream_monitor as sm
from agent import shell_hooks
from cron import scheduler as sched
from hermes_cli.plugins import PluginManager


def test_cron_bulkhead_lane_classification_and_pool_isolation(tmp_path, monkeypatch):
    """Interactive chat jobs run in their own bulkhead pool even when background pool is saturated."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_CRON_BULKHEAD", raising=False)
    assert sched._job_bulkhead_lane({"id": "c247fast0001", "name": "chat247-owner-fast"}) == "interactive_chat"
    assert sched._job_bulkhead_lane({"id": "evelina1", "name": "evelina-responder"}) == "interactive_chat"
    assert sched._job_bulkhead_lane({"id": "audit1", "name": "atlas-daily-audit"}) == "background_other"
    assert sched._job_bulkhead_lane({"id": "x1", "name": "custom", "lane": "interactive_chat"}) == "interactive_chat"

    bg_pool = sched._get_bulkhead_pool(2, "background_other")
    chat_pool = sched._get_bulkhead_pool(2, "interactive_chat")
    assert bg_pool is not chat_pool

    release_bg = threading.Event()
    bg_started = threading.Event()

    def _blocking_bg():
        bg_started.set()
        release_bg.wait(timeout=5.0)
        return "bg_done"

    bg_fut = None
    try:
        bg_fut = bg_pool.submit(_blocking_bg)
        assert bg_started.wait(timeout=2.0)

        # Even with background_other 100% saturated (1/1 worker blocked), interactive_chat runs immediately
        t0 = time.monotonic()
        chat_fut = chat_pool.submit(lambda: "chat_fast")
        assert chat_fut.result(timeout=1.0) == "chat_fast"
        assert (time.monotonic() - t0) < 0.5
    finally:
        release_bg.set()
        if bg_fut is not None:
            bg_fut.result(timeout=2.0)
        sched.discard_parallel_pools([sched.hermes_home_key(tmp_path)])


def test_fast_accept_marker_and_persona_guards_polarity():
    """W2-A: tool names alone must NOT defeat fast-accept; actual marker/persona tokens still run."""
    benign_terminal = {"tool_name": "terminal", "args": {"command": "git status -s"}}
    assert shell_hooks._fast_accept_pre_tool_hook("/path/to/marker-corruption-guard.py", benign_terminal) is True
    assert shell_hooks._fast_accept_pre_tool_hook("/path/to/persona-save-guard.py", benign_terminal) is True

    bad_marker = {"tool_name": "write_file", "args": {"content": "text HERMES-CONTEXT-" + "COMPRESSION here"}}
    assert shell_hooks._fast_accept_pre_tool_hook("/path/to/marker-corruption-guard.py", bad_marker) is False

    bad_omitted = {"tool_name": "terminal", "args": {"command": "echo '⟪400 chars omitted⟫'"}}
    assert shell_hooks._fast_accept_pre_tool_hook("/path/to/marker-corruption-guard.py", bad_omitted) is False

    bad_persona = {"tool_name": "execute_code", "args": {"code": "txt = open('MASTER-PERSONA.md').read()"}}
    assert shell_hooks._fast_accept_pre_tool_hook("/path/to/persona-save-guard.py", bad_persona) is False


def test_parallel_pre_tool_call_speed_and_precedence(monkeypatch):
    """W2-A: pre_tool_call runs matched hooks concurrently and merges block/deny > ask > allow."""
    monkeypatch.delenv("HERMES_HOOKS_SERIAL", raising=False)
    pm = PluginManager()

    def _ask_hook(**kwargs):
        time.sleep(0.15)
        return {"action": "ask", "message": "confirm?"}

    def _block_hook(**kwargs):
        time.sleep(0.15)
        return {"action": "block", "message": "denied!"}

    def _allow_hook_1(**kwargs):
        time.sleep(0.15)

    def _allow_hook_2(**kwargs):
        time.sleep(0.15)
        return {"context": "note"}

    t0 = time.monotonic()
    results = pm._run_hook_callbacks(
        "pre_tool_call",
        [_allow_hook_2, _ask_hook, _allow_hook_1, _block_hook],
        {"tool_name": "terminal", "args": {"command": "ls"}},
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.45, f"Expected parallel execution (<0.45s), took {elapsed:.3f}s"
    assert results[0]["action"] == "block"
    assert results[1]["action"] == "ask"
    assert results[2] == {"context": "note"}

    # Escape hatch: HERMES_HOOKS_SERIAL=1 forces serial execution
    monkeypatch.setenv("HERMES_HOOKS_SERIAL", "1")
    t1 = time.monotonic()
    pm._run_hook_callbacks(
        "pre_tool_call",
        [_allow_hook_1, _allow_hook_1, _allow_hook_1],
        {"tool_name": "terminal", "args": {"command": "ls"}},
    )
    assert (time.monotonic() - t1) >= 0.40


def test_vertex_regional_ttfb_stale_cap_default():
    assert sm.VERTEX_REGIONAL_TTFB_STALE_CAP_SECONDS == 75.0
