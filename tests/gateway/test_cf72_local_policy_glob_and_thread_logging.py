"""Tests for CF-72:
1. _BOOT_RECEIPTS_SQL (local_receipt_ids) and moved_receipts use GLOB and skip gateway.local_policy.v1:local-* rows;
   recover_gateway_native_sessions offloads recover_local_sessions via asyncio.to_thread.
2. run_preflight_compression and compress_after_tool_results run repair_message_alternation immediately.
3. propagate_context_to_thread propagates hermes_logging session_id into worker threads.
"""
from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import sqlite3

import hermes_logging
from gateway import run_runtime, session_local_recovery
from gateway.session_viewer_close import CLOSE_PREFIX
import hermes_state_local
from tools.thread_context import propagate_context_to_thread


def test_boot_receipts_sql_and_moved_receipts_use_glob_and_skip_local_oneshots():
    assert "m.key GLOB ?" in session_local_recovery._BOOT_RECEIPTS_SQL
    assert "m.key NOT GLOB 'gateway.local_policy.v1:local-*'" in session_local_recovery._BOOT_RECEIPTS_SQL
    src_moved = inspect.getsource(hermes_state_local.moved_receipts)
    assert "m.key NOT GLOB 'gateway.local_policy.v1:local-*'" in src_moved
    src_runtime = inspect.getsource(run_runtime.recover_gateway_native_sessions)
    assert "asyncio.to_thread" in src_runtime

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, ended_at REAL, end_reason TEXT, session_key TEXT)"
    )
    conn.execute(
        "CREATE TABLE session_admissions (target_session_id TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('local-123', 'cli', NULL, NULL, 'skey-local')"
    )
    conn.execute(
        "INSERT INTO sessions VALUES ('20261110_014015_008491', 'desktop', NULL, NULL, 'skey-desktop')"
    )
    conn.execute(
        "INSERT INTO state_meta VALUES ('gateway.local_policy.v1:local-123', ?)",
        (json.dumps({"entry": {"session_id": "local-123"}, "route": "old-route"}),),
    )
    conn.execute(
        "INSERT INTO state_meta VALUES ('gateway.local_policy.v1:20261110_014015_008491', ?)",
        (json.dumps({"entry": {"session_id": "20261110_014015_008491"}, "route": "old-route"}),),
    )
    prefix = session_local_recovery.POLICY_PREFIX
    start = len(prefix) + 1
    rows = [
        r[0][len(prefix):]
        for r in conn.execute(
            session_local_recovery._BOOT_RECEIPTS_SQL,
            (prefix + "*", start, CLOSE_PREFIX, start),
        )
    ]
    assert rows == ["20261110_014015_008491"]
    moved = hermes_state_local.moved_receipts(conn)
    assert list(moved.keys()) == ["20261110_014015_008491"]


def test_turn_preflight_repairs_alternation_immediately_after_compression():
    from agent import turn_preflight

    src_pre = inspect.getsource(turn_preflight.run_preflight_compression)
    src_post = inspect.getsource(turn_preflight.compress_after_tool_results)
    assert "repair_message_alternation" in src_pre
    assert "repair_message_alternation" in src_post


def test_propagate_context_to_thread_preserves_session_logging_context():
    prev = hermes_logging.get_session_context()
    try:
        hermes_logging.set_session_context("20261110_014015_008491")
        with ThreadPoolExecutor(max_workers=1) as ex:
            observed = ex.submit(propagate_context_to_thread(hermes_logging.get_session_context)).result()
        assert observed == "20261110_014015_008491"
    finally:
        hermes_logging.set_session_context(prev)
