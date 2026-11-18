"""Behavior coverage for content-addressed session system prompts."""

from __future__ import annotations

import json
import sqlite3


def test_recovery_cleanup_keeps_tool_pins_and_drops_dangling_pin_refs(tmp_path):
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_holder import raw_holder, recovery_orphan_cleanup
    from tests.fixtures.storage_read import session_row
    from tests.fixtures.storage_seed import seed_session

    pin = {"version": "sha", "tools": [{"type": "function", "function": {"name": "read_file"}}]}
    with open_persistence(profile_scope(home=tmp_path)) as store:
        seed_session(store, "pinned", "tui")
        store.sessions.update_tool_pin("pinned", pin)
        seed_session(store, "dangling", "tui")
        seed_session(store, "legacy", "tui")

        def stale_pins(conn):
            conn.execute("UPDATE sessions SET tool_names = ? WHERE id = 'dangling'", ("ab" * 32,))
            conn.execute("UPDATE sessions SET tool_names = ? WHERE id = 'legacy'", ('["read_file"]',))
        raw_holder(store)._execute_write(stale_pins)
    conn = sqlite3.connect(str(tmp_path / "state.db"), isolation_level=None)
    conn.row_factory = sqlite3.Row
    recovery_orphan_cleanup(conn)
    conn.close()

    with open_persistence(profile_scope(home=tmp_path)) as store:
        assert json.loads(session_row(store, "pinned")["tool_names"]) == pin
        assert session_row(store, "dangling")["tool_names"] is None
        assert session_row(store, "legacy")["tool_names"] == '["read_file"]'


def test_update_system_prompt_scopes_gc_to_replaced_candidate_hash(tmp_path):
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_holder import raw_holder
    from tests.fixtures.storage_seed import seed_session

    with open_persistence(profile_scope(home=tmp_path)) as store:
        seed_session(store, "s1", "cli", system_prompt="p1")
        holder = raw_holder(store)
        holder._write_sql(
            "INSERT INTO system_prompts (hash, prompt) VALUES (?, ?)",
            ("bystander_hash", "bystander_prompt"),
        )

        store.sessions.update_system_prompt("s1", "p1")
        hashes = {r[0] for r in holder._read_all("SELECT hash FROM system_prompts")}
        assert "bystander_hash" in hashes

        store.sessions.update_system_prompt("s1", "p2")
        prompts = {r[0] for r in holder._read_all("SELECT prompt FROM system_prompts")}
        assert "p1" not in prompts
        assert "p2" in prompts
        assert "bystander_prompt" in prompts
