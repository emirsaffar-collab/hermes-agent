"""Regression tests for post-compaction spillover sticky-global affinity and
subagent source classification under inherited HERMES_SESSION_SOURCE='desktop'."""

from __future__ import annotations

from types import SimpleNamespace

from agent import realtime_spend_breaker as rsb
from agent import vertex_adapter


def test_post_compaction_turn1_spillover_pins_global_for_epoch():
    """When a rotated/compacted session pinned to europe-west2 has last_prompt_tokens=-1
    (or <45k) on Turn 1 and spills to global because all 5 regional slots are busy,
    it must record global affinity so Turn 2 does not hop back to europe-west2."""
    vertex_adapter.reset_vertex_hybrid_router_state()
    holders = []
    for i in range(vertex_adapter.EUROPE_WEST2_ARGON_MAX_CONCURRENCY):
        r, acq = vertex_adapter.acquire_vertex_hybrid_region(
            "google/gemini-4-argon",
            configured_region="global",
            interactive=True,
            session_id=f"holder_{i}",
        )
        assert (r, acq) == (vertex_adapter.EUROPE_WEST2_VERTEX_REGION, True)
        holders.append(SimpleNamespace(_vertex_regional_slot_held=True))

    # Rotated child session starts pinned to europe-west2 with last_prompt_tokens=-1
    child_sid = "20261114_042500_child"
    vertex_adapter._record_session_vertex_region_affinity(
        child_sid, vertex_adapter.EUROPE_WEST2_VERTEX_REGION
    )
    r1, acq1 = vertex_adapter.acquire_vertex_hybrid_region(
        "google/gemini-4-argon",
        configured_region="global",
        interactive=True,
        session_id=child_sid,
        sticky_wait_seconds=0.01,
        last_prompt_tokens=-1,
    )
    assert (r1, acq1) == (vertex_adapter.GLOBAL_VERTEX_REGION, False)
    assert (
        vertex_adapter.get_session_vertex_region_affinity(child_sid)
        == vertex_adapter.GLOBAL_VERTEX_REGION
    )

    # Even after a regional slot frees up on Turn 2 (52k tokens), child_sid stays on global
    assert vertex_adapter.release_vertex_hybrid_slot(holders.pop()) is True
    r2, acq2 = vertex_adapter.acquire_vertex_hybrid_region(
        "google/gemini-4-argon",
        configured_region="global",
        interactive=True,
        session_id=child_sid,
        last_prompt_tokens=52_000,
    )
    assert (r2, acq2) == (vertex_adapter.GLOBAL_VERTEX_REGION, False)

    for h in holders:
        assert vertex_adapter.release_vertex_hybrid_slot(h) is True


def test_bg_review_spillover_does_not_overwrite_parent_europe_west2_affinity():
    """A :bg-review: fork spilling to global when all 5 slots are busy must not
    clobber its parent session's europe-west2 affinity."""
    vertex_adapter.reset_vertex_hybrid_router_state()
    holders = []
    for i in range(vertex_adapter.EUROPE_WEST2_ARGON_MAX_CONCURRENCY):
        r, acq = vertex_adapter.acquire_vertex_hybrid_region(
            "google/gemini-4-argon",
            configured_region="global",
            interactive=True,
            session_id=f"parent_{i}",
        )
        assert (r, acq) == (vertex_adapter.EUROPE_WEST2_VERTEX_REGION, True)
        holders.append(SimpleNamespace(_vertex_regional_slot_held=True))

    bg_sid = "parent_0:bg-review:1"
    r_bg, acq_bg = vertex_adapter.acquire_vertex_hybrid_region(
        "google/gemini-4-argon",
        configured_region="global",
        interactive=False,
        session_id=bg_sid,
        sticky_wait_seconds=0.01,
        last_prompt_tokens=120_000,
    )
    assert (r_bg, acq_bg) == (vertex_adapter.GLOBAL_VERTEX_REGION, False)
    assert (
        vertex_adapter.get_session_vertex_region_affinity("parent_0")
        == vertex_adapter.EUROPE_WEST2_VERTEX_REGION
    )

    for h in holders:
        assert vertex_adapter.release_vertex_hybrid_slot(h) is True


def test_subagent_under_desktop_session_env_classifies_as_subagent_and_trips_breaker(
    tmp_path, monkeypatch
):
    """A delegate_task subagent inheriting HERMES_SESSION_SOURCE='desktop' from its
    parent must be classified as source='subagent' (non-interactive) rather than
    'desktop' so Tier 1A spend breaker caps apply."""
    from agent.session_source import session_source_for

    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "desktop")

    # Raw session_source_for("subagent") returns inherited "desktop"
    assert session_source_for("subagent") == "desktop"

    subagent = SimpleNamespace(
        session_id="subagent_runaway_1",
        platform="subagent",
        _delegate_depth=1,
        _write_origin=None,
        _memory_write_origin=None,
        _turn_origin=None,
    )
    rt_src = (
        "background_review"
        if (
            getattr(subagent, "_write_origin", None) == "background_review"
            or getattr(subagent, "_memory_write_origin", None) == "background_review"
            or getattr(subagent, "_turn_origin", None) == "background_review"
        )
        else "subagent"
        if (
            getattr(subagent, "platform", None) == "subagent"
            or int(getattr(subagent, "_delegate_depth", 0) or 0) > 0
        )
        else session_source_for(getattr(subagent, "platform", None))
    )
    assert rt_src == "subagent"

    for i in range(6):
        rsb.record_realtime_llm_call(
            session_id="subagent_runaway_1",
            source=rt_src,
            provider="vertex",
            model="google/gemini-4-argon",
            prompt_tokens=210_000,
            uncached_input_tokens=210_000,
            cache_read_tokens=0,
            output_tokens=0,
            request_id=f"sub_req_{i}",
        )

    verdict = rsb.evaluate_realtime_spend_breaker(
        session_id="subagent_runaway_1",
        source=rt_src,
        making_productive_progress=False,
    )
    assert verdict["allowed"] is False
    assert "tier1_session_15m_cap" in verdict["reason"]
