"""Regression tests for P0-1 (Vertex Hybrid Router), P1-1 (slash-skill dedup),
P1-2 (background review cache disable), and P0-2 (post-compaction loop guard)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from agent.context_compressor import ContextCompressor
from agent.turn_context_compaction import should_defer_preflight_to_real_usage
from agent.vertex_adapter import (
    EUROPE_WEST2_ARGON_MAX_CONCURRENCY,
    EUROPE_WEST2_VERTEX_REGION,
    GLOBAL_VERTEX_REGION,
    acquire_vertex_hybrid_region,
    build_vertex_base_url,
    is_vertex_argon_model,
    is_vertex_global_only_model,
    parse_vertex_base_url,
    record_vertex_regional_429,
    release_vertex_hybrid_slot,
    reset_vertex_hybrid_router_state,
)
import tools.skills_tool as skills_tool_mod
from tools.skills_tool import skill_view
from tools.skills_tool_dedup import (
    _skill_view_tracker,
    reset_skill_view_dedup,
    seed_skill_view_dedup_from_messages,
)


@pytest.fixture(autouse=True)
def _clean_hybrid_router_and_dedup():
    reset_vertex_hybrid_router_state()
    reset_skill_view_dedup()
    yield
    reset_vertex_hybrid_router_state()
    reset_skill_view_dedup()


def _setup_skill(tmp_path: Path, monkeypatch, name: str = "wrap", body: str = "# Wrap Skill\nRun preflight and close out.\n") -> Path:
    monkeypatch.setattr(skills_tool_mod, "SKILLS_DIR", tmp_path)
    sdir = tmp_path / name
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n{body}",
        encoding="utf-8",
    )
    return sdir


class TestVertexHybridRouterP01:
    def test_parse_vertex_base_url_global_and_regional(self):
        global_url = "https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi"
        assert parse_vertex_base_url(global_url) == ("uop-atlas", "global")

        regional_url = "https://europe-west2-aiplatform.googleapis.com/v1/projects/uop-atlas/locations/europe-west2/endpoints/openapi"
        assert parse_vertex_base_url(regional_url) == ("uop-atlas", "europe-west2")

        assert parse_vertex_base_url("https://openrouter.ai/api/v1") is None

    def test_model_routing_predicates(self):
        assert is_vertex_argon_model("google/gemini-4-argon") is True
        assert is_vertex_argon_model("gemini-4-argon@vertex") is True
        assert is_vertex_argon_model("google/gemini-3.8-flash") is False

        assert is_vertex_global_only_model("google/gemini-3.8-flash") is True
        assert is_vertex_global_only_model("gemini-3-pro") is True
        assert is_vertex_global_only_model("gemini-4-argon") is False

    def test_semaphore_caps_regional_concurrency_at_five_and_falls_back_to_global(self):
        clients = []
        for _ in range(EUROPE_WEST2_ARGON_MAX_CONCURRENCY):
            region, acquired = acquire_vertex_hybrid_region(
                "google/gemini-4-argon",
                configured_region="global",
                interactive=True,
            )
            assert acquired is True
            assert region == EUROPE_WEST2_VERTEX_REGION
            url = build_vertex_base_url("uop-atlas", region)
            assert "europe-west2-aiplatform.googleapis.com" in url
            c = SimpleNamespace(_vertex_regional_slot_held=acquired)
            clients.append(c)

        # 6th concurrent request must spill over to global without blocking
        spill_region, spill_acquired = acquire_vertex_hybrid_region(
            "google/gemini-4-argon",
            configured_region="global",
            interactive=True,
        )
        assert spill_acquired is False
        assert spill_region == GLOBAL_VERTEX_REGION

        # Releasing one slot makes regional routing available again
        assert release_vertex_hybrid_slot(clients.pop()) is True
        next_region, next_acquired = acquire_vertex_hybrid_region(
            "google/gemini-4-argon",
            configured_region="global",
            interactive=True,
        )
        assert next_acquired is True
        assert next_region == EUROPE_WEST2_VERTEX_REGION
        clients.append(SimpleNamespace(_vertex_regional_slot_held=next_acquired))

        for c in clients:
            assert release_vertex_hybrid_slot(c) is True
            # Idempotent second release returns False
            assert release_vertex_hybrid_slot(c) is False

    def test_background_review_and_flash_stay_on_global(self):
        bg_region, bg_acq = acquire_vertex_hybrid_region(
            "google/gemini-4-argon",
            configured_region="global",
            interactive=False,
        )
        assert bg_acq is False
        assert bg_region == GLOBAL_VERTEX_REGION

        flash_region, flash_acq = acquire_vertex_hybrid_region(
            "google/gemini-3.8-flash",
            configured_region="europe-west2",
            interactive=True,
        )
        assert flash_acq is False
        assert flash_region == GLOBAL_VERTEX_REGION

    def test_regional_429_triggers_cooldown_to_global(self):
        record_vertex_regional_429(cooldown_seconds=30.0)
        region, acquired = acquire_vertex_hybrid_region(
            "google/gemini-4-argon",
            configured_region="global",
            interactive=True,
        )
        assert acquired is False
        assert region == GLOBAL_VERTEX_REGION


class TestSlashSkillDedupP11:
    def test_load_skill_payload_marks_dedup_for_session(self, tmp_path, monkeypatch):
        from agent.skill_commands import _load_skill_payload

        _setup_skill(tmp_path, monkeypatch, "wrap")
        res = _load_skill_payload("wrap", task_id="sess-p11-test")
        assert res is not None
        payload, _skill_dir, display_name = res
        assert payload.get("content")
        assert (display_name, "") in _skill_view_tracker.get("sess-p11-test", {})

        # Subsequent _skill_view_with_bump(name="wrap", task_id="sess-p11-test") must return unchanged stub
        from tools.skills_tool import _skill_view_with_bump

        second = json.loads(_skill_view_with_bump({"name": "wrap"}, task_id="sess-p11-test"))
        assert second.get("dedup") is True
        assert second.get("status") == "unchanged"
        assert second.get("content_returned") is False

    def test_seed_skill_view_dedup_scans_user_scaffold_only_when_unpruned(self, tmp_path, monkeypatch):
        from agent.skill_commands import _build_skill_message, _load_skill_payload
        from tools.skills_tool import _skill_view_with_bump

        _setup_skill(tmp_path, monkeypatch, "wrap")
        loaded = _load_skill_payload("wrap", task_id=None)
        assert loaded is not None
        loaded_skill, skill_dir, display_name = loaded
        note = (
            f'[IMPORTANT: The user has invoked the "{display_name}" skill, indicating they want '
            "you to follow its instructions. The full skill content is loaded below.]"
        )
        msg_text = _build_skill_message(
            loaded_skill,
            skill_dir,
            note,
            user_instruction="close out",
        )
        messages = [{"role": "user", "content": msg_text}]
        seed_skill_view_dedup_from_messages("sess-scaffold-1", messages)
        assert ("wrap", "") in _skill_view_tracker.get("sess-scaffold-1", {})

        # Subsequent _skill_view_with_bump in that seeded session returns the unchanged stub
        second = json.loads(_skill_view_with_bump({"name": "wrap"}, task_id="sess-scaffold-1"))
        assert second.get("dedup") is True

        # When the scaffold was compacted into a [SKILL_PRUNED] stub, do NOT seed dedup
        pruned_messages = [
            {
                "role": "user",
                "content": (
                    f"{note}\n"
                    "[SKILL_PRUNED: content lost in compression; reload with skill_view(name='wrap')]"
                ),
            }
        ]
        seed_skill_view_dedup_from_messages("sess-scaffold-pruned", pruned_messages)
        assert ("wrap", "") not in _skill_view_tracker.get("sess-scaffold-pruned", {})


class TestBackgroundReviewPromptCacheOffP12:
    """P1-2: Background review fork must disable prompt caching to prevent one-shot write surcharges."""

    def test_background_review_fork_disables_prompt_caching(self):
        import run_agent
        from tests.agent.test_background_review_cache_parity import (
            _make_agent_stub,
            _make_recorder_class,
            _SyncThread,
        )
        from unittest.mock import patch

        agent = _make_agent_stub(run_agent.AIAgent)
        captured: dict = {}
        _Recorder = _make_recorder_class(
            captured,
            record_on_run=("_cache_disabled", "_use_prompt_caching", "_memory_write_origin"),
        )
        with patch.object(run_agent, "AIAgent", _Recorder), patch("threading.Thread", _SyncThread):
            agent._spawn_background_review(
                messages_snapshot=[],
                review_memory=True,
                review_skills=False,
            )

        assert captured.get("_memory_write_origin") == "background_review"
        assert captured.get("_cache_disabled") is True
        assert captured.get("_use_prompt_caching") is False


class TestPostCompactionLoopGuardP02:
    def test_defers_180k_unanchored_rough_estimate_immediately_after_compaction(self):
        cc = SimpleNamespace(
            context_length=1_048_576,
            threshold_tokens=157_286,
            awaiting_real_usage_after_compression=True,
            last_compression_rough_tokens=95_000,
            should_defer_preflight_to_real_usage=lambda est: True,
        )
        agent = SimpleNamespace(
            context_compressor=cc,
            _request_pressure_anchored=False,
        )
        # Before P0-2 fix, last_compression_rough_tokens > 0 short-circuited deferral at >=180k
        assert should_defer_preflight_to_real_usage(agent, 210_000) is True

        # When NOT awaiting post-compression real usage, the 180k pricing-cliff cap still fires
        cc.awaiting_real_usage_after_compression = False
        assert should_defer_preflight_to_real_usage(agent, 210_000) is False

    def test_compressor_enforces_post_compaction_step_floor_and_shallow_strike(self):
        cc = ContextCompressor(
            model="google/gemini-4-argon",
            threshold_percent=0.15,
            quiet_mode=True,
        )
        cc.context_length = 1_048_576
        cc.threshold_tokens = 157_286

        # Compaction #1 lands at 152,000 real tokens (barely below 157,286 due to high system+tools floor)
        cc._verify_compaction_cleared_threshold = True
        cc.update_from_response({"prompt_tokens": 152_000})
        assert cc._last_post_compaction_prompt_tokens == 152_000
        assert cc._ineffective_compression_count == 0

        # Effective threshold is now 152,000 + 18,874 = 170,874 (capped at 180,000)
        assert cc._effective_compression_threshold() == 170_874
        # A 7,000-token tool call (159,000 real tokens) must NOT immediately re-trigger compaction
        assert cc.should_compress(159_000) is False
        assert cc.should_compress(170_000) is False
        # Once growth crosses the step floor (171,000), compaction is allowed again
        assert cc.should_compress(171_000) is True

        # Compaction #2 is another shallow compaction landing at 153,000 (<5% headroom below 157,286)
        cc._verify_compaction_cleared_threshold = True
        cc.update_from_response({"prompt_tokens": 153_000})
        assert cc._ineffective_compression_count == 1

        # Compaction #3 is also shallow (154,000) -> 2nd strike trips anti-thrash breaker
        cc._verify_compaction_cleared_threshold = True
        cc.update_from_response({"prompt_tokens": 154_000})
        assert cc._ineffective_compression_count == 2
        should_now, reason = cc.should_compress_info(178_000)
        assert should_now is False
        assert reason == "ineffective"
