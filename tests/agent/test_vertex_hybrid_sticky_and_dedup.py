import json
import os
from agent import vertex_adapter
from tools import file_tools_read_tracking as ftrt


def test_canonical_affinity_session_id_strips_bg_review():
    assert (
        vertex_adapter._canonical_affinity_session_id("20261114_010000_abc123:bg-review:2")
        == "20261114_010000_abc123"
    )
    assert (
        vertex_adapter._canonical_affinity_session_id("20261114_010000_abc123")
        == "20261114_010000_abc123"
    )


def test_turn1_sub_45k_global_records_sticky_affinity(monkeypatch):
    vertex_adapter.reset_vertex_hybrid_router_state()
    monkeypatch.setattr(vertex_adapter, "vertex_regional_cooldown_remaining", lambda: 10.0)
    region, slot = vertex_adapter.acquire_vertex_hybrid_region(
        "google/gemini-4-argon",
        configured_region="global",
        interactive=True,
        session_id="sess_turn1_39k",
        last_prompt_tokens=39_000,
    )
    assert region == "global"
    assert slot is False
    assert vertex_adapter.get_session_vertex_region_affinity("sess_turn1_39k") == "global"

    # Even after regional cooldown expires on Turn 2 (44k tokens), session stays pinned to global
    monkeypatch.setattr(vertex_adapter, "vertex_regional_cooldown_remaining", lambda: 0.0)
    region2, slot2 = vertex_adapter.acquire_vertex_hybrid_region(
        "google/gemini-4-argon",
        configured_region="global",
        interactive=True,
        session_id="sess_turn1_39k",
        last_prompt_tokens=44_000,
    )
    assert region2 == "global"
    assert slot2 is False


def test_bg_review_inherits_parent_europe_west2_affinity():
    vertex_adapter.reset_vertex_hybrid_router_state()
    parent_sid = "sess_parent_ew2"
    bg_sid = f"{parent_sid}:bg-review:1"
    vertex_adapter._record_session_vertex_region_affinity(parent_sid, "europe-west2")
    region, slot = vertex_adapter.acquire_vertex_hybrid_region(
        "google/gemini-4-argon",
        configured_region="global",
        interactive=False,
        session_id=bg_sid,
        last_prompt_tokens=120_000,
    )
    assert region == "europe-west2"
    assert slot is True
    vertex_adapter._EUROPE_WEST2_ARGON_SEMAPHORE.release()

    # Real build_cache_parity_fork shares parent.session_id directly (without :bg-review:)
    region_direct, slot_direct = vertex_adapter.acquire_vertex_hybrid_region(
        "google/gemini-4-argon",
        configured_region="global",
        interactive=False,
        session_id=parent_sid,
        last_prompt_tokens=0,
    )
    assert region_direct == "europe-west2"
    assert slot_direct is True
    vertex_adapter._EUROPE_WEST2_ARGON_SEMAPHORE.release()


def test_seed_file_dedup_from_messages_seeds_surviving_reads(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("x = 1\n" * 60, encoding="utf-8")
    resolved = os.path.realpath(str(f))
    tid = "sess_resume_1"
    dedup_key = (resolved, 1, 2000)

    with ftrt._read_tracker_lock:
        td = ftrt._task_data(tid)
        td["dedup"][dedup_key] = ftrt._file_metadata(resolved)
        td["dedup_generation_reads"].add(dedup_key)

    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": resolved, "offset": 1, "limit": 2000}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": json.dumps({"content": "1|x = 1\n" * 60, "total_lines": 60}),
        },
    ]
    ftrt.reset_file_dedup(tid, surviving_messages=messages)
    _, _, stub_eligible = ftrt._dedup_lookup(tid, dedup_key)
    assert stub_eligible is True
