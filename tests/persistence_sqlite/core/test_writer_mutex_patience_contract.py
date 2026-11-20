"""Write patience bounds the in-process writer mutex wait without losing a write (field report).

A field probe held ``StoreHolder._lock`` and saw a ``patience_s=0.05`` write wait ~0.27 s and then run.
The patience budget now covers the mutex: a write that cannot take it in time is refused before
BEGIN with :class:`WriterMutexTimeout`, which callers already treat as lock contention (retry or
defer, never damage). These tests pin the contract end to end:

* refused inside the budget, classified as in-process contention (not a foreign SQLite writer);
* the refusal names the in-process holder (thread, call site, held-for) without SQL or values;
* queued token accounting that hits the refusal keeps the delta and applies it once free.
"""

import logging
import sqlite3
import threading
import time

import pytest

from persistence.sqlite._holder import StoreHolder
from persistence.sqlite._state.errors import classify_persistence_error, is_sqlite_lock_error
from persistence.sqlite._state.write import WriterMutexTimeout
from persistence.sqlite.conversation._store.sessions import create_session, get_session
from tests.waiting import wait_until


@pytest.fixture()
def db(tmp_path):
    handle = StoreHolder(tmp_path / "state.db")
    create_session(handle, "owned", "test")
    yield handle
    handle.close()


def _hold_writer_mutex(db, hold_s):
    """Hold the writer mutex from a named thread through the real write path (a slow callback)."""
    held, release = threading.Event(), threading.Event()

    def slow_write(conn):
        held.set()
        release.wait(hold_s)
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("kept", "owned"))

    holder = threading.Thread(target=lambda: db._execute_write(slow_write), name="slow-transcript-writer")
    holder.start()
    assert held.wait(5), "holder never took the writer mutex"
    return holder, release


def test_refused_within_patience_and_classified_as_in_process_contention(db):
    holder, release = _hold_writer_mutex(db, hold_s=5)
    try:
        started = time.monotonic()
        with pytest.raises(WriterMutexTimeout) as raised:
            db._write_sql("UPDATE sessions SET title = ? WHERE id = ?", ("never", "owned"), patience_s=0.05)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(5)
    assert elapsed < 0.25, f"a 0.05 s write waited {elapsed:.3f}s for the writer mutex"
    exc = raised.value
    # Existing callers retry/defer on the lock class: the refusal must stay inside it.
    assert isinstance(exc, sqlite3.OperationalError) and is_sqlite_lock_error(exc)
    assert classify_persistence_error(exc) == "locked"
    # ... but it is told apart from a foreign SQLite writer.
    assert exc.sqlite_errorcode == sqlite3.SQLITE_BUSY
    assert "in-process" in str(exc)
    assert "another Hermes process" not in str(exc)
    assert get_session(db, "owned")["title"] == "kept"  # the refused write never ran


def test_refusal_names_the_in_process_holder_without_sql_or_values(db, caplog):
    holder, release = _hold_writer_mutex(db, hold_s=5)
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_state"), pytest.raises(WriterMutexTimeout):
            db._write_sql("UPDATE sessions SET title = ? WHERE id = ?", ("secret-title-value", "owned"),
                          patience_s=0.05)
    finally:
        release.set()
        holder.join(5)
    lines = [r.getMessage() for r in caplog.records if "writer mutex" in r.getMessage()]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "slow-transcript-writer" in line  # holder thread
    assert "slow_write" in line  # holder call site (function name, not source text)
    assert "state.db" in line
    assert "held for" in line
    assert "secret-title-value" not in line and "UPDATE" not in line


def test_holder_record_is_cleared_after_release(db):
    holder, release = _hold_writer_mutex(db, hold_s=5)
    release.set()
    holder.join(5)
    db._write_sql("UPDATE sessions SET title = ? WHERE id = ?", ("after", "owned"), patience_s=0.05)
    assert get_session(db, "owned")["title"] == "after"
    assert getattr(db, "_writer_owner", None) is None


def test_queued_token_delta_survives_a_mutex_refusal(db, monkeypatch, caplog):
    """The token writer applies queued deltas with routine patience; a refusal must requeue, not drop."""
    caplog.set_level(logging.DEBUG, logger="hermes_state")
    for name in ("_WRITE_PATIENCE_S", "_TRANSCRIPT_WRITE_PATIENCE_S"):
        monkeypatch.setattr(StoreHolder, name, 0.05)
    holder, release = _hold_writer_mutex(db, hold_s=5)
    try:
        db.queue_token_counts("owned", input_tokens=7, output_tokens=3, model="m")
        # Several 0.05 s attempts are refused while the mutex is held: wait for the first refusal to land.
        wait_until(lambda: any("requeued" in r.getMessage() for r in caplog.records), timeout=5.0,
                   what="a token-writer requeue")
        assert db._token_queue or db._token_writer_busy, "the refused delta left the queue"
    finally:
        release.set()
        holder.join(5)
    assert db.flush_token_counts(timeout=10.0)
    row = get_session(db, "owned")
    assert (row["input_tokens"], row["output_tokens"]) == (7, 3), "queued token delta was lost"


def test_quiet_activity_write_suppresses_in_process_mutex_warning(db, monkeypatch, caplog):
    """Observation-only activity writes (quiet_lock_timeout=True) yield on their sub-second
    patience without emitting the in-process writer mutex WARNING log."""
    from persistence.sqlite._state.write import WriterMutexTimeout

    monkeypatch.setattr(StoreHolder, "_ACTIVITY_WRITE_PATIENCE_S", 0.05)
    holder, release = _hold_writer_mutex(db, hold_s=5)
    try:
        with caplog.at_level(logging.WARNING, logger="hermes_state"), pytest.raises(WriterMutexTimeout):
            db.touch_session_activity("owned", description="executing tool", provenance="tool")
    finally:
        release.set()
        holder.join(5)
    lines = [r.getMessage() for r in caplog.records if "writer mutex" in r.getMessage()]
    assert lines == [], f"quiet activity write logged unexpected writer mutex warning: {lines}"


def test_publish_compression_child_clears_parent_activity_labels_atomically(db, monkeypatch):
    """publish_compression_child clears the closed parent's activity labels inside the atomic
    publication transaction so post-publish clear_session_activity_labels is a zero-write no-op."""
    db.touch_session_activity("owned", description="compressing context", provenance="compression")
    before = db._read_one(
        "SELECT last_activity_description, last_activity_provenance FROM sessions WHERE id = ?",
        ("owned",),
    )
    assert before["last_activity_description"] == "compressing context"

    db.publish_compression_child(
        parent_session_id="owned",
        child_session_id="owned-child-1",
        source="cli",
        messages=[{"role": "user", "content": "handoff summary"}],
        require_compression_lease=False,
    )
    after = db._read_one(
        "SELECT last_activity_description, last_activity_provenance FROM sessions WHERE id = ?",
        ("owned",),
    )
    assert after["last_activity_description"] is None
    assert after["last_activity_provenance"] is None

    write_calls = []
    orig_write_sql = db._write_sql
    monkeypatch.setattr(db, "_write_sql", lambda *a, **kw: (write_calls.append(a), orig_write_sql(*a, **kw))[1])
    db.clear_session_activity_labels("owned")
    assert write_calls == [], f"Expected zero post-publish write_sql calls, got {write_calls}"


def test_checkpoint_holder_extends_writer_mutex_wait_without_timeout(db):
    """When StoreHolder._try_wal_checkpoint holds self._lock on Darwin (fullfsync barrier),
    concurrent in-process writers with standard patience extend their wait rather than failing
    with WriterMutexTimeout."""
    held, release = threading.Event(), threading.Event()

    def hold_as_checkpoint():
        with db._lock:
            db._writer_owner = ("asyncio_7", (None, db._try_wal_checkpoint.__code__), time.monotonic())
            held.set()
            try:
                release.wait(2.0)
            finally:
                db._writer_owner = None

    t = threading.Thread(target=hold_as_checkpoint, name="asyncio_7")
    t.start()
    assert held.wait(2.0)
    timer = threading.Timer(0.18, release.set)
    timer.start()
    try:
        db._write_sql("UPDATE sessions SET title = ? WHERE id = ?", ("after-ckpt", "owned"), patience_s=0.05)
    finally:
        timer.cancel()
        release.set()
        t.join(2.0)
    row = get_session(db, "owned")
    assert row is not None and row["title"] == "after-ckpt"


def test_terminal_api_admissions_uses_partial_index_without_full_scan(db):
    """terminal_api_admissions must use idx_state_meta_terminal_api_request rather than scanning
    non-API gateway.terminal_admission.v1.* rows."""
    import json
    from persistence.sqlite.authority._runtime import terminal_api_admissions

    db._write_sql(
        "INSERT OR REPLACE INTO state_meta(key, value) VALUES (?, ?), (?, ?)",
        (
            "gateway.terminal_admission.v1.adm-cron-1",
            json.dumps({"admission_id": "adm-cron-1", "principal_id": "cron-owner", "request_id": "req-1", "status": "terminal"}),
            "gateway.terminal_admission.v1.adm-api-1",
            json.dumps({"admission_id": "adm-api-1", "principal_id": "api", "request_id": "req-api-1", "status": "terminal"}),
        ),
    )
    rows = terminal_api_admissions(db._conn, "req-api-1")
    assert len(rows) == 1 and rows[0]["admission_id"] == "adm-api-1"
    plan = db._read_all(
        """
        EXPLAIN QUERY PLAN
        SELECT value FROM state_meta
        WHERE key GLOB 'gateway.terminal_admission.v1.*'
          AND json_valid(value)
          AND json_extract(value, '$.principal_id') = 'api'
          AND json_extract(value, '$.request_id') = ?
        """,
        ("req-api-1",),
    )
    detail = " ".join(str(dict(r).get("detail", "")) for r in plan)
    assert "idx_state_meta_terminal_api_request" in detail, f"Expected index scan, got: {detail}"


def test_webhook_script_isolated_runs_on_dedicated_executor_preserving_contextvars():
    """_run_webhook_script_isolated must execute on the 'webhook-script*' thread pool
    and preserve contextvars across the thread boundary."""
    import asyncio
    import contextvars
    from gateway.platforms.webhook import _run_webhook_script_isolated

    var = contextvars.ContextVar("webhook_test_scope", default="unset")

    async def _probe():
        var.set("profile-alpha")
        return await _run_webhook_script_isolated(
            lambda x: (True, {"thread": threading.current_thread().name, "scope": var.get(), "x": x}),
            42,
        )

    keep, payload = asyncio.run(_probe())
    assert keep is True
    assert payload["thread"].startswith("webhook-script")
    assert payload["scope"] == "profile-alpha"
    assert payload["x"] == 42



