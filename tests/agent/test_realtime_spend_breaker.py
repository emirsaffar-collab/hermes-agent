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
    for macro in ("0L8P", "9_oP", "6-wP", "6_gP", "08QP", "4ATb", "7bQP", "7PIP", "9wMP"):
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
