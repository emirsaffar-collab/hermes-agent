"""Regression and polarity tests for RC-01 and RC-02 (2026-11-10):
- RC-01: _repair_alternation_after_compression in agent.turn_preflight handles both normal
  repair_message_alternation and stale in-memory sys.modules['agent.message_sanitization'] skew
  without raising ImportError.
- RC-02: _archive_named_rows in hermes_state_messages archives superseded compaction summary blocks
  left in unseen when a post-compaction hook aborts before the caller adopts the new _row_id set,
  preventing triangular n(n+1)/2 compaction row cloning while still cloning genuine concurrent
  unseen user/assistant appends.
"""
from types import SimpleNamespace

from agent import turn_preflight
from agent.conversation_compression_archive import coverage_for_commit
from hermes_state import SessionDB


def test_rc01_repair_alternation_resilient_under_stale_module_skew():
    import agent.message_sanitization as ms_mod

    # 1. Normal path
    agent_normal = SimpleNamespace(_last_flushed_db_idx=2, _db_flush_scan_prefix=2)
    msgs_normal = [{"role": "user", "content": "u1"}, {"role": "user", "content": "u2"}]
    out_normal, repairs_normal = turn_preflight._repair_alternation_after_compression(
        msgs_normal, agent=agent_normal
    )
    assert repairs_normal == 1
    assert len(out_normal) == 1
    assert out_normal[0]["content"] == "u1\n\nu2"
    assert agent_normal._last_flushed_db_idx == 1

    # 2. Stale module skew path (simulating serve --isolated pid=78969 pre-10125d84ef26)
    saved_fn = getattr(ms_mod, "repair_message_alternation", None)
    try:
        if hasattr(ms_mod, "repair_message_alternation"):
            delattr(ms_mod, "repair_message_alternation")
        agent_skew = SimpleNamespace(_last_flushed_db_idx=2, _db_flush_scan_prefix=2)
        msgs_skew = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
        out_skew, repairs_skew = turn_preflight._repair_alternation_after_compression(
            msgs_skew, agent=agent_skew
        )
        assert repairs_skew == 1
        assert len(out_skew) == 1
        assert out_skew[0]["content"] == "a\n\nb"
        assert agent_skew._last_flushed_db_idx == 1
    finally:
        if saved_fn is not None:
            ms_mod.repair_message_alternation = saved_fn


def test_rc02_archive_named_rows_archives_superseded_compaction_without_cloning(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("sid-rc02", source="test")
        db.append_message("sid-rc02", "user", "U1")
        db.append_message("sid-rc02", "assistant", "A1")
        held_before_crash = db.get_messages_as_conversation("sid-rc02", include_row_ids=True)

        # Compaction #1 commits to state.db before caller crashes on post-compaction hook
        cov1, unres1 = coverage_for_commit(db, "sid-rc02", held_before_crash)
        compacted_1 = [
            {
                "role": "user",
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Summary #1",
                "_compressed_summary": True,
            },
            {"role": "assistant", "content": "Tail #1"},
        ]
        db.archive_and_compact(
            "sid-rc02", compacted_1, tail_count=1, covered_ids=cov1, unresolved_held=unres1
        )

        # Caller never adopted compacted_1 in memory; appends U2 and runs Compaction #2,
        # while another surface appends a genuine concurrent UNSEEN_AFTER message after U2.
        db.append_message("sid-rc02", "user", "U2")
        u2_id = db.get_active_message_watermark("sid-rc02")
        held_before_crash.append(
            {"role": "user", "content": "U2", "_row_id": u2_id, "_db_persisted": True}
        )
        db.append_message("sid-rc02", "user", "UNSEEN_AFTER")

        cov2, unres2 = coverage_for_commit(db, "sid-rc02", held_before_crash)
        compacted_2 = [
            {
                "role": "user",
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Summary #2",
                "_compressed_summary": True,
            },
            {"role": "user", "content": "U2"},
        ]
        db.archive_and_compact(
            "sid-rc02", compacted_2, tail_count=1, covered_ids=cov2, unresolved_held=unres2
        )

        live = [row["content"] for row in db.get_messages("sid-rc02")]
        assert live == ["[CONTEXT COMPACTION — REFERENCE ONLY] Summary #2", "U2", "UNSEEN_AFTER"]
        active_summaries = db._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id='sid-rc02' AND active=1 AND _compressed_summary=1"
        ).fetchone()[0]
        assert active_summaries == 1

        # Also verify the watermark fallback path (when no new turn row was flushed after the crash,
        # so covered_ids all point to inactive pre-Compaction-#2 rows and proved_coverage returns None)
        compacted_3 = [
            {
                "role": "user",
                "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Summary #3",
                "_compressed_summary": True,
            },
            {"role": "user", "content": "U2"},
        ]
        db.archive_and_compact(
            "sid-rc02", compacted_3, watermark=u2_id, tail_count=1, covered_ids=[u2_id], unresolved_held=[]
        )
        live3 = [row["content"] for row in db.get_messages("sid-rc02")]
        assert live3 == ["[CONTEXT COMPACTION — REFERENCE ONLY] Summary #3", "U2", "UNSEEN_AFTER"]
        active_summaries3 = db._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id='sid-rc02' AND active=1 AND _compressed_summary=1"
        ).fetchone()[0]
        assert active_summaries3 == 1
    finally:
        db.close()
