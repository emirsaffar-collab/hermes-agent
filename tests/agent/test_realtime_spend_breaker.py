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




