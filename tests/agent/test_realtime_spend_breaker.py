from agent import realtime_spend_breaker as rsb


def test_log_tail_does_not_inflate_non_interactive_breaker_or_fake_resets(tmp_path, monkeypatch):
    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))

    r1 = rsb.record_realtime_llm_call(
        session_id="log_tail:rid_sess_a",
        source="log_tail",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=160_000,
        uncached_input_tokens=10_000,
        cache_read_tokens=150_000,
        output_tokens=400,
        request_id="rid_sess_a",
    )
    r2 = rsb.record_realtime_llm_call(
        session_id="log_tail:rid_sess_b",
        source="log_tail",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=60_000,
        uncached_input_tokens=20_000,
        cache_read_tokens=40_000,
        output_tokens=300,
        request_id="rid_sess_b",
    )
    assert r1["cache_reset"] == 0
    assert r2["cache_reset"] == 0

    summary = rsb.get_realtime_spend_summary()
    assert summary["net_eur_today_non_interactive"] == 0.0
    assert summary["cache_resets_today"] == 0


def test_tier0_prefixes_and_cron_spend_cap_silent_deferral(tmp_path, monkeypatch):
    from agent.turn_failure_copy import exit_reason_failure
    from cron.scheduler import _final_response_from_result

    assert rsb.is_tier0_critical_cron("nw-chat-responder", "local-abc") is True
    assert rsb.is_tier0_critical_cron("chat247-responder", "local-abc") is True
    assert rsb.is_tier0_critical_cron("chat247-owner", "local-abc") is True
    assert rsb.is_tier0_critical_cron("newintra-builder-owner", "local-abc") is True
    assert rsb.is_tier0_critical_cron("morning-preemptive-audit", "local-abc") is True
    assert rsb.is_tier0_critical_cron("daily-retrospective", "local-abc") is True
    assert rsb.is_tier0_critical_cron("night-driver", "local-abc") is True
    assert rsb.is_tier0_critical_cron("saffar-inbox-brief", "local-abc") is True
    assert rsb.is_tier0_critical_cron("decision-log-pipeline", "local-abc") is True
    assert rsb.is_tier0_critical_cron("daily-db-backup-drill", "local-abc") is True
    assert rsb.is_tier0_critical_cron("atlas-watchdog", "local-abc") is False

    ef = exit_reason_failure("realtime_spend_breaker:tier3_daily_hard_cap")
    assert ef is not None
    assert ef.reason == "spend_cap_deferred"
    assert ef.fails_turn is False

    res = {
        "final_response": "[SILENT]",
        "completed": True,
        "failed": False,
        "interrupted": False,
        "turn_exit_reason": "realtime_spend_breaker:tier3_daily_hard_cap",
    }
    assert _final_response_from_result(res, "j1", "atlas-watchdog", object) == "[SILENT]"


def test_bq_verified_rates_and_in_process_upgrades_log_tail(tmp_path, monkeypatch):
    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))

    # Global Argon (g7QP) vs Regional europe-west2 Argon (0L8P = 1.10x)
    eur_global = rsb.compute_call_net_eur(
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=250_000,
        uncached_input_tokens=100_000,
        cache_read_tokens=150_000,
        output_tokens=1_000,
        cluster_id="Ffg7QP",
    )
    eur_ew2 = rsb.compute_call_net_eur(
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=250_000,
        uncached_input_tokens=100_000,
        cache_read_tokens=150_000,
        output_tokens=1_000,
        cluster_id="Ff0L8P",
    )
    assert round(eur_ew2 / eur_global, 4) == 1.10

    # log_tail row upgraded by live in-process row with reasoning tokens included
    rsb.record_realtime_llm_call(
        session_id="log_tail:CN_3atLcDrqFg7QPgvDv8QM",
        source="log_tail",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=85_995,
        uncached_input_tokens=47_806,
        cache_read_tokens=38_189,
        output_tokens=209,
        request_id="CN_3atLcDrqFg7QPgvDv8QM",
    )
    rsb.record_realtime_llm_call(
        session_id="20261114_045816_c001c5",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=85_995,
        uncached_input_tokens=47_806,
        cache_read_tokens=38_189,
        output_tokens=299,
        request_id="CN_3atLcDrqFg7QPgvDv8QM",
    )
    summary = rsb.get_realtime_spend_summary()
    assert summary["calls_today"] == 1
    assert summary["calls_today_all"] == 1
    assert summary["net_eur_today"] == summary["net_eur_today_all"]

    # Interactive human surfaces (desktop, mobile, tui) are never blocked by Tier 3 daily cap
    rsb.record_realtime_llm_call(
        session_id="cron_big",
        source="cron",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=20_000_000,
        uncached_input_tokens=20_000_000,
        cache_read_tokens=0,
        output_tokens=20_000_000,
        request_id="rid_big_cap",
    )
    verdict_mobile = rsb.evaluate_realtime_spend_breaker(
        session_id="20261114_053148_a1c3c5",
        source="mobile",
        model="google/gemini-4-argon",
        provider="vertex",
        making_productive_progress=True,
    )
    assert verdict_mobile["allowed"] is True
    assert verdict_mobile["reason"] == "interactive_allowed"
    verdict_tui = rsb.evaluate_realtime_spend_breaker(
        session_id="20261114_053148_a1c3c6",
        source="tui",
        model="google/gemini-4-argon",
        provider="vertex",
        making_productive_progress=True,
    )
    assert verdict_tui["allowed"] is True
    assert verdict_tui["reason"] == "interactive_allowed"


def test_manual_ha_emergency_brake_override_blocks_non_interactive_only(tmp_path, monkeypatch):
    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    brake_flag = tmp_path / "ai-emergency-brake-override.flag"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))
    monkeypatch.setenv("HERMES_AI_EMERGENCY_BRAKE_FLAG_PATH", str(brake_flag))

    assert rsb.is_manual_emergency_brake_active() is False
    v_ok = rsb.evaluate_realtime_spend_breaker(
        session_id="cron_normal",
        source="cron",
        job_name="atlas-watchdog",
    )
    assert v_ok["allowed"] is True

    brake_flag.write_text("on\n")
    assert rsb.is_manual_emergency_brake_active() is True
    summary = rsb.get_realtime_spend_summary()
    assert summary["manual_emergency_brake_active"] is True
    assert summary["breaker_state"] == "MANUAL_EMERGENCY_BRAKE"

    v_blocked = rsb.evaluate_realtime_spend_breaker(
        session_id="cron_normal",
        source="cron",
        job_name="atlas-watchdog",
    )
    assert v_blocked["allowed"] is False
    assert "manual_ha_emergency_brake" in v_blocked["reason"]

    v_tier0 = rsb.evaluate_realtime_spend_breaker(
        session_id="cron_tier0",
        source="cron",
        job_name="alert-triage",
    )
    assert v_tier0["allowed"] is True

    v_desktop = rsb.evaluate_realtime_spend_breaker(
        session_id="desktop_user",
        source="desktop",
        making_productive_progress=True,
    )
    assert v_desktop["allowed"] is True


def test_pruned_system_prompt_38k_floor_and_per_session_15m_reset_aggregation(tmp_path, monkeypatch):
    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))

    # 4 separate desktop sessions hitting warm 38,273-token messages[0] cache on Turn 1/2
    for idx in range(4):
        row = rsb.record_realtime_llm_call(
            session_id=f"desk_turn1_{idx}",
            source="desktop",
            provider="vertex",
            model="google/gemini-4-argon",
            prompt_tokens=72_394,
            uncached_input_tokens=34_121,
            cache_read_tokens=38_273,
            output_tokens=400,
            request_id=f"rid_turn1_{idx}",
        )
        assert row["cache_reset"] == 0

    summary = rsb.get_realtime_spend_summary()
    assert summary["cache_resets_15m"] == 0
    assert summary["cache_resets_15m_total"] == 0

    # Now desk_turn1_0 warms up to 145k cached, then suffers an intra-session floor reset to 38,273
    r_warm = rsb.record_realtime_llm_call(
        session_id="desk_turn1_0",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=150_000,
        uncached_input_tokens=5_000,
        cache_read_tokens=145_000,
        output_tokens=400,
        request_id="rid_warm_0",
    )
    assert r_warm["cache_reset"] == 0

    r_drop = rsb.record_realtime_llm_call(
        session_id="desk_turn1_0",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=165_000,
        uncached_input_tokens=126_727,
        cache_read_tokens=38_273,
        output_tokens=400,
        request_id="rid_drop_0",
    )
    assert r_drop["cache_reset"] == 1
    summary2 = rsb.get_realtime_spend_summary()
    assert summary2["cache_resets_15m"] == 1

    # Regional hybrid hop where cached_in >= 50% of prompt (94,284 / 141,852 = 66.5%) is not a reset
    rsb.record_realtime_llm_call(
        session_id="desk_turn1_1",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=130_541,
        uncached_input_tokens=8_649,
        cache_read_tokens=121_892,
        output_tokens=400,
        request_id="rid_reg_a",
    )
    r_reg_b = rsb.record_realtime_llm_call(
        session_id="desk_turn1_1",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=141_852,
        uncached_input_tokens=47_568,
        cache_read_tokens=94_284,
        output_tokens=400,
        request_id="rid_reg_b",
    )
    assert r_reg_b["cache_reset"] == 0


def test_ew2_macro_clusters_sustained_cold_cache_and_compression_rotation_lineage(tmp_path, monkeypatch):
    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))

    # 1) All 9 europe-west2 macro-clusters carry the 1.10x regional multiplier
    eur_global = rsb.compute_call_net_eur(
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=200_000,
        uncached_input_tokens=80_000,
        cache_read_tokens=120_000,
        output_tokens=1_000,
        cluster_id="Kyg7QP",
    )
    for macro in ("0L8P", "l-oP", "9_oP", "6-wP", "6_gP", "08QP", "4ATb", "9wMP"):
        eur_reg = rsb.compute_call_net_eur(
            provider="vertex",
            model="google/gemini-4-argon",
            prompt_tokens=200_000,
            uncached_input_tokens=80_000,
            cache_read_tokens=120_000,
            output_tokens=1_000,
            cluster_id=f"Ky{macro}",
        )
        assert round(eur_reg / eur_global, 4) == 1.10

    # 2) Sustained 0% cold-cache failure on Flash (Turn 1-2 = 0, Turn 3+ = 1)
    resets = []
    for idx in range(6):
        row = rsb.record_realtime_llm_call(
            session_id="flash_cold_storm",
            source="cron",
            job_name="chat247-owner",
            provider="vertex",
            model="google/gemini-3.8-flash",
            prompt_tokens=60_000 + idx * 1_000,
            uncached_input_tokens=60_000 + idx * 1_000,
            cache_read_tokens=0,
            output_tokens=300,
            request_id=f"rid_flash_cold_{idx}",
        )
        resets.append(row["cache_reset"])
    assert resets == [0, 0, 1, 1, 1, 1]

    # 3) Compression child rotation (#CF-91) preserves rolling 15m spend across parent -> child
    for idx in range(2):
        rsb.record_realtime_llm_call(
            session_id="cron_parent_rot",
            source="cron",
            job_name="atlas-watchdog",
            provider="vertex",
            model="google/gemini-4-argon",
            prompt_tokens=800_000,
            uncached_input_tokens=800_000,  # ~€1.44 net each -> €2.89 total on parent
            cache_read_tokens=0,
            output_tokens=1_000,
            request_id=f"rid_rot_p_{idx}",
        )
    rsb.record_realtime_llm_call(
        session_id="cron_child_rot",
        source="cron",
        job_name="atlas-watchdog",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=800_000,
        uncached_input_tokens=800_000,  # +€1.44 on child -> combined €4.33 >= €4.00 Tier 1 cap
        cache_read_tokens=0,
        output_tokens=1_000,
        request_id="rid_rot_c_0",
    )
    verdict_rot = rsb.evaluate_realtime_spend_breaker(
        session_id="cron_child_rot",
        source="cron",
        job_name="atlas-watchdog",
        parent_session_id="cron_parent_rot",
        parent_source="cron",
    )
    assert verdict_rot["allowed"] is False
    assert "tier1_session_15m_cap" in verdict_rot["reason"]

    # 4) 10s write_realtime_status_snapshot(extra=None) preserves watchdog-enriched bq_mtd
    rsb.write_realtime_status_snapshot(
        extra={
            "bq_mtd": {"ok": True, "true_mtd_vertex_net_eur": 11078.32},
            "may_2027_sparplan_targets": {"gcp_vertex_ai_daily_target_eur": 250.0},
        }
    )
    snap_after = rsb.write_realtime_status_snapshot(extra=None)
    assert snap_after.get("bq_mtd", {}).get("ok") is True
    assert snap_after["bq_mtd"]["true_mtd_vertex_net_eur"] == 11078.32
    assert snap_after.get("may_2027_sparplan_targets", {}).get("gcp_vertex_ai_daily_target_eur") == 250.0


def test_tier0_cron_subagent_lineage_and_promo_cliff(tmp_path, monkeypatch):
    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))

    # 1) Promo-cliff rate switch verification
    assert rsb.resolve_vertex_rates("google/gemini-4-argon", ts=1796111999.0) == (1.7962, 0.08981, 8.981)
    assert rsb.resolve_vertex_rates("google/gemini-4-argon", ts=1796112000.0) == (3.5924, 0.17962, 17.962)
    assert rsb.resolve_vertex_rates("google/gemini-3.8-flash", ts=1798790399.0) == (0.673573, 0.067357, 3.367844)
    assert rsb.resolve_vertex_rates("google/gemini-3.8-flash", ts=1798790400.0) == (1.347146, 0.134714, 6.735688)

    # Seed estate desktop spend above €195/day Tier 3 cap
    rsb.record_realtime_llm_call(
        session_id="desktop_heavy_1",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=120_000_000,
        uncached_input_tokens=120_000_000,
        cache_read_tokens=0,
        output_tokens=100_000,
        request_id="req_desktop_heavy_1",
    )

    # 2) Subagent spawned by Tier-0 cron (daily-retrospective) inherits cron_job_name and passes Tier 3
    v_t0_sub = rsb.evaluate_realtime_spend_breaker(
        session_id="sub_retro_1",
        source="subagent",
        job_name="daily-retrospective",
        parent_session_id="cron_retro_parent",
        parent_source="cron",
    )
    assert v_t0_sub["allowed"] is True
    assert v_t0_sub["reason"] == "tier0_critical_cron_exempt"

    # 3) Subagent spawned by non-Tier-0 cron is blocked when estate_today >= €195
    v_nont0_sub = rsb.evaluate_realtime_spend_breaker(
        session_id="sub_other_1",
        source="subagent",
        job_name="atlas-watchdog",
        parent_session_id="cron_other_parent",
        parent_source="cron",
    )
    assert v_nont0_sub["allowed"] is False
    assert v_nont0_sub["reason"].startswith("tier3_daily_hard_cap")

    # 4) Summary breaker_state stays ARMED_OK when only interactive Desktop spend exceeds €195
    summary_after_desktop = rsb.get_realtime_spend_summary()
    assert summary_after_desktop["breaker_state"] == "ARMED_OK"


def test_manual_ui_pause_throttle_and_hourly_budget_cap(tmp_path, monkeypatch):
    import json
    import time

    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    cap_file = tmp_path / "ai-budget-cap.json"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))
    monkeypatch.setenv("HERMES_AI_BUDGET_CAP_PATH", str(cap_file))

    now = time.time()

    # 1) Manual UI pause blocks even productive interactive Desktop sessions (positive polarity)
    rsb.quarantine_session("sess_paused_ui", "manual_ui_pause:Pausad manuellt av Emir (45m)", 0.0, ttl_s=600.0, now=now)
    v_paused = rsb.evaluate_realtime_spend_breaker(
        session_id="sess_paused_ui",
        source="desktop",
        making_productive_progress=True,
        consecutive_calls_since_user=1,
        now=now + 1.0,
    )
    assert v_paused["allowed"] is False
    assert "manual_ui_pause:" in v_paused["reason"]

    # Negative polarity: automatic tier1 quarantine still allows productive interactive Desktop session
    rsb.quarantine_session("sess_auto_q", "tier1_stalled_desktop_loop", 3.5, ttl_s=600.0, now=now)
    v_auto_q = rsb.evaluate_realtime_spend_breaker(
        session_id="sess_auto_q",
        source="desktop",
        making_productive_progress=True,
        consecutive_calls_since_user=2,
        now=now + 1.0,
    )
    assert v_auto_q["allowed"] is True

    # 2) Manual UI throttle allows interactive session below €1.50/15m, blocks once >= €1.50/15m
    rsb.quarantine_session("sess_throttled_ui", "manual_ui_throttle:Strypt manuellt av Emir", 0.0, ttl_s=600.0, now=now)
    v_th_ok = rsb.evaluate_realtime_spend_breaker(
        session_id="sess_throttled_ui",
        source="desktop",
        making_productive_progress=True,
        consecutive_calls_since_user=3,
        now=now + 1.0,
    )
    assert v_th_ok["allowed"] is True

    rsb.record_realtime_llm_call(
        session_id="sess_throttled_ui",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=1_000_000,
        uncached_input_tokens=1_000_000,  # ~€1.80 >= €1.50 throttle limit
        cache_read_tokens=0,
        output_tokens=1_000,
        request_id="req_th_1",
    )
    v_th_blocked = rsb.evaluate_realtime_spend_breaker(
        session_id="sess_throttled_ui",
        source="desktop",
        making_productive_progress=True,
        consecutive_calls_since_user=4,
        now=now + 3.0,
    )
    assert v_th_blocked["allowed"] is False
    assert v_th_blocked["reason"].startswith("throttled:manual_ui_throttle:")

    # 3) Manual UI hourly budget cap ($15/h -> €13.50/h -> €3.38/15m)
    cap_file.write_text(
        json.dumps(
            {
                "ts": now,
                "expires_at": now + 3600.0,
                "cap_usd_hr": 15.0,
                "cap_eur_hr": 13.50,
                "cap_eur_15m": 3.38,
                "operator": "Emir",
                "active": True,
            }
        ),
        encoding="utf-8",
    )
    # Seed estate 15m spend above €3.38
    rsb.record_realtime_llm_call(
        session_id="sess_heavy_loop",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-argon",
        prompt_tokens=2_000_000,
        uncached_input_tokens=2_000_000,  # ~€3.59 in 15m
        cache_read_tokens=0,
        output_tokens=1_000,
        request_id="req_cap_1",
    )
    # Non-Tier-0 cron is throttled under the $15/h UI cap
    v_cron_capped = rsb.evaluate_realtime_spend_breaker(
        session_id="cron_non_t0",
        source="cron",
        job_name="atlas-watchdog",
        now=now + 5.0,
    )
    assert v_cron_capped["allowed"] is False
    assert "manual_budget_cap_15m" in v_cron_capped["reason"]

    # Tier-0 critical cron remains exempt
    v_t0_exempt = rsb.evaluate_realtime_spend_breaker(
        session_id="cron_t0",
        source="cron",
        job_name="chat247-responder",
        now=now + 5.0,
    )
    assert v_t0_exempt["allowed"] is True

    # Runaway multi-call interactive loop (calls=8, session €3.59/15m) is throttled under $15/h UI cap
    v_loop_capped = rsb.evaluate_realtime_spend_breaker(
        session_id="sess_heavy_loop",
        source="desktop",
        making_productive_progress=True,
        consecutive_calls_since_user=8,
        now=now + 5.0,
    )
    assert v_loop_capped["allowed"] is False
    assert "manual_budget_cap_interactive_throttle" in v_loop_capped["reason"]

    # Immediate user turn (consecutive_calls_since_user=1) on the same session is NEVER blocked
    v_user_turn = rsb.evaluate_realtime_spend_breaker(
        session_id="sess_heavy_loop",
        source="desktop",
        making_productive_progress=True,
        consecutive_calls_since_user=1,
        now=now + 5.0,
    )
    assert v_user_turn["allowed"] is True


def test_compaction_storm_long_term_fixes_guardrails_budget_and_lineage(tmp_path, monkeypatch):
    import time

    from agent.iteration_budget import IterationBudget
    from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController

    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    cap_file = tmp_path / "ai-budget-cap.json"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))
    monkeypatch.setenv("HERMES_AI_BUDGET_CAP_PATH", str(cap_file))

    # 1) Cross-compaction guardrail memory: 1x reload forgiven, 3rd unchanged reload across compactions warns
    # and clears productive progress, 4th blocks, and file mutation resets cross-compaction memory.
    ctrl = ToolCallGuardrailController(ToolCallGuardrailConfig(hard_stop_enabled=True))
    d1 = ctrl.after_call("read_file", {"path": "/repo/app.py"}, '{"content": "x = 1\\n"}', failed=False)
    assert d1.action == "allow"
    assert ctrl.is_making_productive_progress() is True

    # Compaction #1: resets _turn_productive_calls to 0; 1st post-compaction reload is forgiven
    ctrl.reset_after_compaction()
    assert ctrl.is_making_productive_progress() is False
    d2 = ctrl.after_call("read_file", {"path": "/repo/app.py"}, '{"content": "x = 1\\n"}', failed=False)
    assert d2.action == "allow"
    assert ctrl.is_making_productive_progress() is True

    # Compaction #2: 3rd unchanged read across compactions triggers idempotent_no_progress_warning
    # and flips is_making_productive_progress() to False!
    ctrl.reset_after_compaction()
    assert ctrl.is_making_productive_progress() is False
    d3 = ctrl.after_call("read_file", {"path": "/repo/app.py"}, '{"content": "x = 1\\n"}', failed=False)
    assert d3.action == "warn"
    assert d3.code == "idempotent_no_progress_warning"
    assert ctrl.is_making_productive_progress() is False

    # Compaction #3: 4th unchanged read is blocked by before_call once count reaches cross_block_after
    ctrl.reset_after_compaction()
    ctrl.after_call("read_file", {"path": "/repo/app.py"}, '{"content": "x = 1\\n"}', failed=False)
    d_block = ctrl.before_call("read_file", {"path": "/repo/app.py"})
    assert d_block.action == "block"
    assert d_block.code == "idempotent_no_progress_block"

    # Landing a file mutation clears cross-compaction read blocks so verification reads succeed
    ctrl.after_call("patch", {"path": "/repo/app.py"}, '{"success": true}', failed=False)
    assert ctrl.before_call("read_file", {"path": "/repo/app.py"}).action == "allow"
    assert ctrl.is_making_productive_progress() is True

    # 2) IterationBudget: capped execute_code refunds stop at max_refunds=20; extend_on_progress caps at 2x
    budget = IterationBudget(20)
    assert budget.consume() is True
    for _ in range(20):
        assert budget.refund(capped=True, max_refunds=20) is True
        assert budget.consume() is True
    # 21st capped refund is denied
    assert budget.refund(capped=True, max_refunds=20) is False
    # Uncapped refund (for cancelled/restarted API requests) still works
    assert budget.refund(capped=False) is True

    # extend_on_progress caps at 2x (40 for initial 20), not 4x (80)
    budget._used = 18
    for _ in range(20):
        assert budget.extend_on_progress(1) is True
        budget._used = budget.max_total - 2
    assert budget.max_total == 40
    assert budget.extend_on_progress(1) is False

    # 3) Multi-hop compaction lineage (s0 -> s1 -> s2 -> s3) sums 15m spend across all 4 segments
    # and trips tier1_runaway_autonomous_turn at >=90 calls and >=€10.00 in 15m even if productive
    now = time.time()
    for seg_idx, (sid, p_sid) in enumerate(
        [("seg_0", ""), ("seg_1", "seg_0"), ("seg_2", "seg_1"), ("seg_3", "seg_2")]
    ):
        if p_sid:
            rsb.record_session_parent(sid, p_sid)
        rsb.record_realtime_llm_call(
            session_id=sid,
            source="desktop",
            provider="vertex",
            model="google/gemini-4-argon",
            prompt_tokens=1_500_000,
            uncached_input_tokens=1_500_000,  # ~€2.69 per segment -> ~€10.78 across 4 segments
            cache_read_tokens=0,
            output_tokens=1_000,
            request_id=f"req_seg_{seg_idx}",
        )

    # At 40 calls (below 90 runaway ceiling) with making_productive_progress=True -> allowed,
    # and session_net_eur_15m reflects all 4 segments (> €10.00, not just last 2 segments €5.39)
    v_mid = rsb.evaluate_realtime_spend_breaker(
        session_id="seg_3",
        source="desktop",
        making_productive_progress=True,
        consecutive_calls_since_user=40,
        parent_session_id="seg_2",
        now=now + 5.0,
    )
    assert v_mid["allowed"] is True
    assert v_mid["session_net_eur_15m"] > 10.0

    # At 90 calls without user input and >€10.00 across the compaction chain -> halted!
    v_runaway = rsb.evaluate_realtime_spend_breaker(
        session_id="seg_3",
        source="desktop",
        making_productive_progress=True,
        consecutive_calls_since_user=90,
        parent_session_id="seg_2",
        now=now + 5.0,
    )
    assert v_runaway["allowed"] is False
    assert "tier1_runaway_autonomous_turn" in v_runaway["reason"]


def test_extract_vertex_cluster_id_aligns_22_char_borg_request_id():
    # 23-char (17-byte, raw[11]==0x0f) Borg request_id slices rid[10:16] directly
    assert rsb.extract_vertex_cluster_id("K3P8aqzEB_Cxg7QP5O6x-AI") == "Cxg7QP"
    # 22-char (16-byte, 2-byte usec LEB128 varint) Borg request_id aligns byte 10 -> 11 to recover canonical TPU pod & cell
    assert rsb.extract_vertex_cluster_id("WnX8aqJ4_7iDtA_u76WoDw") == "-4g7QP"
    assert rsb.extract_vertex_cluster_id("tqr3apZd-q_r7A_q_abgCA") == "qv6-wP"
    assert rsb.extract_vertex_cluster_id("s7T3ar4cgfSy0w_Oq6HpAg") == "H0stMP"
    # 20-char (15-byte, 1-byte or 2-byte usec LEB128 varint) Borg request_id aligns to canonical TPU pod & cell
    assert rsb.extract_vertex_cluster_id("vAr8amqdi-vsD8XjlIkI") == "2L6-wP"
    assert rsb.extract_vertex_cluster_id("u5b7arcMxry6wQ_46qgR") == "a8usEP"
    assert rsb.extract_vertex_cluster_id("7ZD7atJ9gdbgBNuDk5gC") == "HW4ATb"
    assert rsb.extract_vertex_cluster_id("n5j8asPcMJ2SmNAP-a4I") == "2SmNAP"
    # 22-char 4ATb (3-byte usec LEB128 varint) preserves rid[10:16]
    assert rsb.extract_vertex_cluster_id("pjb5auGeHoHW4ATbg5OYAg") == "HW4ATb"



