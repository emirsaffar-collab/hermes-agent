"""A compaction archives each dropped tool result exactly once, after its summary exists, and the
committed chat names every archive whatever the generated summary says.

Planning (``_plan_compaction_window``) only decides whether work can start, so it writes nothing; a
prepared pass (refused or completed) summarizes the originals and writes nothing either: the compaction
that splices its summary archives the window once.
The summary is model text and may drop a pointer, so the committed summary row lists every archive the
replaced rows named; the retention sweep then keeps those files.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import prepared_compaction as pc
from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY, ContextCompressor
from agent.prepared_compaction import PreparedCompaction


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    return tmp_path


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=200_000):
        c = ContextCompressor(model="test/model", quiet_mode=True, protect_first_n=2, protect_last_n=4)
    c._session_id = "s1"
    return c


def _transcript(n=24, chars=12_000):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "read them"}]
    for i in range(n):
        cid = f"call_{i}"
        msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {
                     "name": "read_file", "arguments": json.dumps({"path": f"f{i}.py"})}}]},
                 {"role": "tool", "tool_call_id": cid, "content": f"line {i}\n" + chr(65 + i % 26) * chars}]
    return msgs + [{"role": "assistant", "content": "all read"}, {"role": "user", "content": "now summarize"}]


def _archived(home) -> list[Path]:
    d = home / "cache" / "pruned"
    return sorted(d.iterdir()) if d.exists() else []


def _no_pointer_summary():
    """A summarizer that, like a real model may, names none of the archive paths it was shown."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = "## Goal\nRead the files.\n## Progress\nRead them all."
    return patch("agent.context_compressor.call_llm", return_value=response)


def test_planning_and_the_prepared_pass_write_nothing_and_the_splice_archives_each_result_once(home):
    msgs = _transcript()
    c = _compressor()
    for _ in range(3):  # every tool batch plans; none of them is admitted work
        c._plan_compaction_window(msgs)
    assert _archived(home) == []

    c.prepared_compaction = PreparedCompaction()
    c._generate_summary = lambda turns, **kw: c._with_summary_prefix("prepared")
    c.last_prompt_tokens = c.threshold_tokens - 1
    agent = SimpleNamespace(context_compressor=c, compression_enabled=True, api_mode="chat_completions")
    with patch.object(pc._slots, "acquire", return_value=False):  # every summary slot busy: refused
        assert pc.maybe_prepare(agent, msgs, origin="tool-batch") is None
    assert _archived(home) == []

    thread = pc.maybe_prepare(agent, msgs, origin="tool-batch")
    thread.join(5.0)
    assert c.prepared_compaction._entry is not None and _archived(home) == []

    compressed = c.compress(msgs, current_tokens=c.threshold_tokens + 1)

    assert any(m.get(COMPRESSED_SUMMARY_METADATA_KEY) for m in compressed)
    bodies = [p.read_bytes() for p in _archived(home)]
    assert bodies and len(bodies) == len(set(bodies))  # no tool result was written twice


def test_the_committed_summary_names_every_archive_the_generated_text_omits_and_the_sweep_keeps_them(home):
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_messages, seed_session
    from tools.tool_result_storage import cleanup_spillover_cache

    msgs = _transcript()
    c = _compressor()
    with _no_pointer_summary():
        compressed = c.compress(msgs, current_tokens=c.threshold_tokens + 1)
    files = _archived(home)
    assert files

    committed = "\n".join(str(m.get("content")) for m in compressed)
    for path in files:
        assert f"cache/pruned/{path.name}" in committed
    (summary_row,) = [m for m in compressed if m.get(COMPRESSED_SUMMARY_METADATA_KEY)]
    assert "Archived tool output" in summary_row["content"]

    db = open_persistence(profile_scope(home=home), eager=True)
    try:
        seed_session(db, "s1", "cli")
        seed_messages(db, "s1", *(m for m in compressed if m.get("role") != "system"))
    finally:
        db.close()
    old = 1_000_000_000
    for path in files:
        os.utime(path, (old, old))
    assert cleanup_spillover_cache(max_age_hours=1) == 0
    assert _archived(home) == files


def test_multihop_compaction_collapses_ancestor_archives_to_session_prefixes_and_keeps_sweep_safe(home):
    """Positive polarity: Hop 1 archives are listed verbatim in msg[0].
    Negative polarity: Hop 2 collapses Hop 1's archives into `<session_id>_* (N)` prefixes instead of
    snowballing verbatim paths, strips `Archived tool output` from PREVIOUS SUMMARY, excludes `cache/pruned/`
    from `## Anchor Index -> files:`, and still preserves all files across `cleanup_spillover_cache()` when
    both hops' transcripts are in `state.db`."""
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_messages, seed_session
    from tools.tool_result_storage import OLDER_ARCHIVE_PREFIX_HEADING, cleanup_spillover_cache

    c = _compressor()
    c._session_id = "20261120_010000_hop001"
    hop1_msgs = _transcript()
    with _no_pointer_summary():
        hop1_compressed = c.compress(hop1_msgs, current_tokens=c.threshold_tokens + 1)
    hop1_files = _archived(home)
    assert hop1_files
    hop1_committed = "\n".join(str(m.get("content")) for m in hop1_compressed)
    (hop1_summary_row,) = [m for m in hop1_compressed if m.get(COMPRESSED_SUMMARY_METADATA_KEY)]
    hop1_summary_only_files = [
        p for p in hop1_files
        if not any(p.name in str(m.get("content", "")) for m in hop1_compressed if m is not hop1_summary_row)
    ]
    assert hop1_summary_only_files
    for path in hop1_files:
        assert f"cache/pruned/{path.name}" in hop1_committed
    for path in hop1_summary_only_files:
        assert f"cache/pruned/{path.name}" in hop1_summary_row["content"]

    # Hop 2: child session starts from hop1_compressed and adds another 30 tool rounds.
    c._session_id = "20261120_020000_hop002"
    hop2_msgs = list(hop1_compressed)
    for i in range(30, 60):
        cid = f"c_{i}"
        hop2_msgs.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": cid, "function": {"name": "terminal", "arguments": f'{{"command":"echo {i}"}}'}}],
            }
        )
        hop2_msgs.append({"role": "tool", "tool_call_id": cid, "content": f"round={i}\n" + ("y" * 600)})

    with _no_pointer_summary():
        hop2_compressed = c.compress(hop2_msgs, current_tokens=c.threshold_tokens + 1)
    all_files = _archived(home)
    hop2_files = [p for p in all_files if p not in hop1_files]
    assert hop2_files

    (hop2_summary_row,) = [m for m in hop2_compressed if m.get(COMPRESSED_SUMMARY_METADATA_KEY)]
    hop2_text = hop2_summary_row["content"]
    # Positive: Hop 2's own dropped tool outputs are listed verbatim.
    for path in hop2_files:
        if not any(path.name in str(m.get("content", "")) for m in hop2_compressed if m is not hop2_summary_row):
            assert f"cache/pruned/{path.name}" in hop2_text
    # Negative: Hop 1's summary-only file paths do NOT snowball into Hop 2's summary row; they collapse to prefix.
    for path in hop1_summary_only_files:
        assert f"cache/pruned/{path.name}" not in hop2_text
    assert OLDER_ARCHIVE_PREFIX_HEADING in hop2_text
    assert f"20261120_010000_hop001_* ({len(hop1_summary_only_files)})" in hop2_text
    from tools.tool_result_storage import ancestor_archive_prefix_counts
    assert ancestor_archive_prefix_counts([{"content": "example cache/pruned/x.json and cache/pruned/0.json"}]) == {}

    # Retention sweep keeps both Hop 1 and Hop 2 files when both hops are persisted in state.db.
    db = open_persistence(profile_scope(home=home), eager=True)
    try:
        seed_session(db, "20261120_010000_hop001", "cli")
        seed_messages(db, "20261120_010000_hop001", *(m for m in hop1_compressed if m.get("role") != "system"))
        seed_session(db, "20261120_020000_hop002", "cli")
        seed_messages(db, "20261120_020000_hop002", *(m for m in hop2_compressed if m.get("role") != "system"))
    finally:
        db.close()
    old = 1_000_000_000
    for path in all_files:
        os.utime(path, (old, old))
    assert cleanup_spillover_cache(max_age_hours=1) == 0
    assert _archived(home) == all_files
