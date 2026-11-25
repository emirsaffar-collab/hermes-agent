"""Tests for Vertex EU multi-region REP routing, Gemini 4 spend accounting/breaker
polarity, and aux:compression Sync Flex (0.50x) with transparent fallback."""

from __future__ import annotations

from types import SimpleNamespace
import pytest

from agent import realtime_spend_breaker as rsb
from agent import vertex_adapter as va
from agent.auxiliary_client import (
    VertexFlexCompressionAuxiliaryClient,
    _build_vertex_client,
)


@pytest.fixture(autouse=True)
def _reset_router():
    va.reset_vertex_hybrid_router_state()
    yield
    va.reset_vertex_hybrid_router_state()


def test_vertex_multi_region_rep_url_build_and_parse():
    eu_url = va.build_vertex_base_url("uop-atlas", "eu")
    us_url = va.build_vertex_base_url("uop-atlas", "us")
    gl_url = va.build_vertex_base_url("uop-atlas", "global")
    ew2_url = va.build_vertex_base_url("uop-atlas", "europe-west2")

    assert eu_url == "https://aiplatform.eu.rep.googleapis.com/v1beta1/projects/uop-atlas/locations/eu/endpoints/openapi"
    assert us_url == "https://aiplatform.us.rep.googleapis.com/v1beta1/projects/uop-atlas/locations/us/endpoints/openapi"
    assert gl_url == "https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi"
    assert ew2_url == "https://europe-west2-aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/europe-west2/endpoints/openapi"

    assert va.parse_vertex_base_url(eu_url) == ("uop-atlas", "eu")
    assert va.parse_vertex_base_url(us_url) == ("uop-atlas", "us")
    assert va.parse_vertex_base_url(gl_url) == ("uop-atlas", "global")
    assert va.parse_vertex_base_url(ew2_url) == ("uop-atlas", "europe-west2")


def test_neon_and_hydrogen_multi_region_policy_and_cooldown_fallback(monkeypatch):
    # Default (unset): stays global
    monkeypatch.delenv("HERMES_VERTEX_NEON_REGION", raising=False)
    monkeypatch.setattr(va, "_vertex_config", dict)
    assert va.acquire_vertex_hybrid_region("google/gemini-4-neon", session_id="s1") == ("global", False)

    # Explicit eu policy routes Neon & Hydrogen to eu, never Flash 3.8
    monkeypatch.setenv("HERMES_VERTEX_NEON_REGION", "eu")
    assert va.acquire_vertex_hybrid_region("google/gemini-4-neon", session_id="s_eu") == ("eu", False)
    assert va.acquire_vertex_hybrid_region("google/gemini-4-hydrogen", session_id="s_hy") == ("eu", False)
    assert va.acquire_vertex_hybrid_region("google/gemini-3.8-flash", session_id="s_fl") == ("global", False)

    # Tripping eu cooldown demotes cleanly to global
    va.record_vertex_regional_429(cooldown_seconds=60.0, session_id="s_eu")
    assert va.vertex_eu_multi_region_cooldown_remaining() > 0.0
    assert va.acquire_vertex_hybrid_region("google/gemini-4-neon", session_id="s_eu2") == ("global", False)


def test_neon_canary_50_is_deterministic_sticky_and_balanced(monkeypatch):
    monkeypatch.setenv("HERMES_VERTEX_NEON_REGION", "canary_50")
    monkeypatch.setattr(va, "_vertex_config", dict)

    counts = {"eu": 0, "global": 0}
    for i in range(100):
        sid = f"20261125_090000_sess_{i:03d}"
        reg1, slot1 = va.acquire_vertex_hybrid_region("google/gemini-4-neon", session_id=sid)
        reg2, slot2 = va.acquire_vertex_hybrid_region("google/gemini-4-neon", session_id=sid)
        assert reg1 == reg2  # sticky per session
        assert slot1 is False and slot2 is False
        counts[reg1] += 1

    assert 35 <= counts["eu"] <= 65
    assert 35 <= counts["global"] <= 65


def test_spend_breaker_neon_hydrogen_eu_and_flex_rates_plus_cache_reset_polarity(tmp_path, monkeypatch):
    db_file = tmp_path / "ai-spend-realtime.db"
    status_file = tmp_path / "ai-cost-realtime-status.json"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_file))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(status_file))

    # Neon global vs EU 1.10x (both via explicit region and via verified EU macro-cluster 2s4P)
    eur_neon_gl = rsb.compute_call_net_eur(
        provider="vertex",
        model="google/gemini-4-neon",
        prompt_tokens=1_000_000,
        uncached_input_tokens=100_000,
        cache_read_tokens=900_000,
        output_tokens=10_000,
        region="global",
    )
    eur_neon_eu_reg = rsb.compute_call_net_eur(
        provider="vertex",
        model="google/gemini-4-neon",
        prompt_tokens=1_000_000,
        uncached_input_tokens=100_000,
        cache_read_tokens=900_000,
        output_tokens=10_000,
        region="eu",
    )
    eur_neon_eu_cid = rsb.compute_call_net_eur(
        provider="vertex",
        model="google/gemini-4-neon",
        prompt_tokens=1_000_000,
        uncached_input_tokens=100_000,
        cache_read_tokens=900_000,
        output_tokens=10_000,
        cluster_id="Ff2s4P",
    )
    # Expected global: (0.1*3.00 + 0.9*0.15 + 0.01*15.00) * 0.8981 = 0.585 * 0.8981 = 0.5253885
    assert abs(eur_neon_gl - 0.525389) < 1e-5
    assert round(eur_neon_eu_reg / eur_neon_gl, 4) == 1.10
    assert eur_neon_eu_cid == eur_neon_eu_reg

    # Flash 3.8 Flex = exact 0.50x of Standard
    eur_fl_std = rsb.compute_call_net_eur(
        provider="vertex",
        model="google/gemini-3.8-flash",
        prompt_tokens=60_000,
        uncached_input_tokens=60_000,
        cache_read_tokens=0,
        output_tokens=4_000,
        ts=1795600000.0,
    )
    eur_fl_flex = rsb.compute_call_net_eur(
        provider="vertex",
        model="google/gemini-3.8-flash",
        prompt_tokens=60_000,
        uncached_input_tokens=60_000,
        cache_read_tokens=0,
        output_tokens=4_000,
        service_tier="flex",
        ts=1795600000.0,
    )
    assert round(eur_fl_flex / eur_fl_std, 4) == 0.50

    # Polarity test: Neon warm turn -> cache_reset=0; sudden drop 90k -> 5k -> cache_reset=1
    r_warm1 = rsb.record_realtime_llm_call(
        session_id="neon_pol_sess",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-neon",
        prompt_tokens=100_000,
        uncached_input_tokens=10_000,
        cache_read_tokens=90_000,
        output_tokens=500,
        request_id="rid_neon_1",
        ts=1795600000.0,
    )
    r_warm2 = rsb.record_realtime_llm_call(
        session_id="neon_pol_sess",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-neon",
        prompt_tokens=105_000,
        uncached_input_tokens=10_000,
        cache_read_tokens=95_000,
        output_tokens=500,
        request_id="rid_neon_2",
        ts=1795600030.0,
    )
    r_drop = rsb.record_realtime_llm_call(
        session_id="neon_pol_sess",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-neon",
        prompt_tokens=110_000,
        uncached_input_tokens=105_000,
        cache_read_tokens=5_000,
        output_tokens=500,
        request_id="rid_neon_3",
        ts=1795600060.0,
    )
    assert r_warm1["cache_reset"] == 0
    assert r_warm2["cache_reset"] == 0
    assert r_drop["cache_reset"] == 1


def test_vertex_flex_compression_wrapper_and_fallback(monkeypatch):
    monkeypatch.setattr(va, "has_vertex_credentials", lambda: True)
    monkeypatch.setattr(
        va,
        "get_vertex_config",
        lambda region=None, **kw: ("ya29.tok", va.build_vertex_base_url("uop-atlas", region or "global")),
    )

    # Default (HERMES_VERTEX_COMPRESSION_FLEX unset / 0): standard synchronous client (no Sync Flex)
    monkeypatch.delenv("HERMES_VERTEX_COMPRESSION_FLEX", raising=False)
    c_default, _ = _build_vertex_client("vertex", "google/gemini-3.8-flash", task="compression")
    assert not isinstance(c_default, VertexFlexCompressionAuxiliaryClient)

    # Opt-in HERMES_VERTEX_COMPRESSION_FLEX=1 -> wrapped in VertexFlexCompressionAuxiliaryClient
    monkeypatch.setenv("HERMES_VERTEX_COMPRESSION_FLEX", "1")
    c_comp, m_comp = _build_vertex_client("vertex", "google/gemini-3.8-flash", task="compression")
    assert isinstance(c_comp, VertexFlexCompressionAuxiliaryClient)
    assert m_comp == "google/gemini-3.8-flash"

    # non-compression task or non-flash model -> plain OpenAI client
    c_title, _ = _build_vertex_client("vertex", "google/gemini-3.8-flash", task="title")
    c_neon, _ = _build_vertex_client("vertex", "google/gemini-4-neon", task="compression")
    assert not isinstance(c_title, VertexFlexCompressionAuxiliaryClient)
    assert not isinstance(c_neon, VertexFlexCompressionAuxiliaryClient)

    # Success path via call_vertex_native_flex
    fake_flex_resp = SimpleNamespace(
        id="vflex-abc123",
        model="google/gemini-3.8-flash",
        service_tier="flex",
        choices=[SimpleNamespace(index=0, message=SimpleNamespace(role="assistant", content="## Summary\nok"), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=50_000, completion_tokens=500, total_tokens=50_500),
    )
    monkeypatch.setattr(va, "call_vertex_native_flex", lambda **kw: fake_flex_resp)
    resp = c_comp.chat.completions.create(model="google/gemini-3.8-flash", messages=[{"role": "user", "content": "hi"}])
    assert resp.id == "vflex-abc123"
    assert resp.service_tier == "flex"

    # Failure fallback path -> transparently calls fallback OpenAI client
    def _boom(**kw):
        raise RuntimeError("flex 503")

    monkeypatch.setattr(va, "call_vertex_native_flex", _boom)
    fb_called = {}

    def _fb_create(**kw):
        fb_called["called"] = True
        return SimpleNamespace(
            id="std-999",
            choices=[SimpleNamespace(index=0, message=SimpleNamespace(role="assistant", content="fallback ok"), finish_reason="stop")],
        )

    c_comp._fallback_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_fb_create)))
    c_comp.chat.completions._fallback_client = c_comp._fallback_client
    resp_fb = c_comp.chat.completions.create(model="google/gemini-3.8-flash", messages=[{"role": "user", "content": "hi"}])
    assert fb_called.get("called") is True
    assert resp_fb.id == "std-999"


def test_vertex_client_cache_key_isolates_compression_from_interactive_tasks(monkeypatch):
    """V1a + V1b regression: compression and title/web_extract must NEVER share a _client_cache slot on Vertex,
    and get_text_auxiliary_client('compression') must forward task='compression'."""
    from agent.auxiliary_client import (
        VertexFlexCompressionAuxiliaryClient,
        _client_cache,
        _client_cache_key,
        _get_cached_client,
        get_text_auxiliary_client,
    )

    k_title = _client_cache_key("vertex", async_mode=False, task="title", model="google/gemini-3.8-flash")
    k_comp = _client_cache_key("vertex", async_mode=False, task="compression", model="google/gemini-3.8-flash")
    assert k_title != k_comp, "Vertex compression and title collided on _client_cache_key!"

    _client_cache.clear()
    monkeypatch.setenv("HERMES_VERTEX_COMPRESSION_FLEX", "1")
    monkeypatch.setattr(va, "has_vertex_credentials", lambda: True)
    monkeypatch.setattr(
        va,
        "get_vertex_config",
        lambda region=None, **kw: ("ya29.tok", va.build_vertex_base_url("uop-atlas", region or "global")),
    )

    # Order 1: title warms cache FIRST, then compression MUST still get Flex wrapper
    c_title_1, _ = _get_cached_client("vertex", "google/gemini-3.8-flash", task="title")
    c_comp_1, _ = _get_cached_client("vertex", "google/gemini-3.8-flash", task="compression")
    assert not isinstance(c_title_1, VertexFlexCompressionAuxiliaryClient)
    assert isinstance(c_comp_1, VertexFlexCompressionAuxiliaryClient)

    # Order 2: compression warms cache FIRST, then title MUST NOT get hijacked onto Flex
    _client_cache.clear()
    c_comp_2, _ = _get_cached_client("vertex", "google/gemini-3.8-flash", task="compression")
    c_title_2, _ = _get_cached_client("vertex", "google/gemini-3.8-flash", task="title")
    assert isinstance(c_comp_2, VertexFlexCompressionAuxiliaryClient)
    assert not isinstance(c_title_2, VertexFlexCompressionAuxiliaryClient)

    # Public consumer entry point get_text_auxiliary_client('compression') forwards task
    monkeypatch.setattr(
        "agent.auxiliary_client._resolve_task_provider_model",
        lambda task=None, *a, **kw: ("vertex", "google/gemini-3.8-flash", None, None, None),
    )
    c_pub, _ = get_text_auxiliary_client("compression")
    assert isinstance(c_pub, VertexFlexCompressionAuxiliaryClient)
    c_pub_title, _ = get_text_auxiliary_client("title")
    assert not isinstance(c_pub_title, VertexFlexCompressionAuxiliaryClient)
    _client_cache.clear()


def test_spend_breaker_persists_region_service_tier_and_decodes_vflex_cluster(tmp_path, monkeypatch):
    """V2 + V3 regressions: llm_call_ledger persists region/service_tier via ALTER TABLE migration and decodes vflex- cluster."""
    import sqlite3
    from agent import realtime_spend_breaker as rsb

    db = tmp_path / "spend.db"
    con = sqlite3.connect(str(db))
    con.execute(
        """
        CREATE TABLE llm_call_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL, day_cet TEXT NOT NULL, session_id TEXT NOT NULL,
            source TEXT NOT NULL, job_name TEXT DEFAULT '', provider TEXT NOT NULL,
            model TEXT NOT NULL, prompt_tokens INTEGER NOT NULL,
            uncached_in INTEGER NOT NULL, cached_in INTEGER NOT NULL,
            out_tokens INTEGER NOT NULL, net_eur REAL NOT NULL,
            cluster_id TEXT DEFAULT '', cache_reset INTEGER DEFAULT 0,
            request_id TEXT UNIQUE
        )
        """
    )
    con.commit()
    con.close()

    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db))
    monkeypatch.setenv("HERMES_AI_SPEND_STATUS_PATH", str(tmp_path / "status.json"))

    assert rsb.extract_vertex_cluster_id("vflex-pg4Ga7XaI_u4stMPqIaIuAQ") == rsb.extract_vertex_cluster_id("pg4Ga7XaI_u4stMPqIaIuAQ")

    rsb.record_realtime_llm_call(
        session_id="sess_eu_persist",
        source="desktop",
        provider="vertex",
        model="google/gemini-4-neon",
        prompt_tokens=100_000,
        uncached_input_tokens=10_000,
        cache_read_tokens=90_000,
        output_tokens=1_000,
        request_id="2w4Ga9qDKNeBwdYPpqipmQY",
        region="eu",
    )
    rsb.record_realtime_llm_call(
        session_id="sess_flex_persist",
        source="aux:compression",
        provider="vertex",
        model="google/gemini-3.8-flash",
        prompt_tokens=60_000,
        uncached_input_tokens=60_000,
        cache_read_tokens=0,
        output_tokens=1_500,
        request_id="vflex-pg4Ga7XaI_u4stMPqIaIuAQ",
        region="global",
        service_tier="flex",
    )

    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT session_id, region, service_tier, cluster_id FROM llm_call_ledger ORDER BY id").fetchall()
    con.close()
    assert rows[0][0] == "sess_eu_persist" and rows[0][1] == "eu" and rows[0][2] == ""
    assert rows[1][0] == "sess_flex_persist" and rows[1][1] == "global" and rows[1][2] == "flex"
    assert rows[1][3] == rsb.extract_vertex_cluster_id("pg4Ga7XaI_u4stMPqIaIuAQ")


def test_usage_pricing_google_dec1_and_jan1_cliffs(monkeypatch):
    from datetime import UTC, datetime
    from decimal import Decimal
    from agent import usage_pricing as up

    monkeypatch.setattr(up, "_UTC_NOW", lambda: datetime(2026, 11, 24, 12, tzinfo=UTC))
    argon_pre = up.get_pricing_entry(model_name="gemini-4-argon", provider="google")
    flash_pre = up.get_pricing_entry(model_name="gemini-3.8-flash", provider="google")
    assert argon_pre is not None and argon_pre.input_cost_per_million == Decimal("2.00")
    assert flash_pre is not None and flash_pre.input_cost_per_million == Decimal("0.75")

    monkeypatch.setattr(up, "_UTC_NOW", lambda: datetime(2026, 12, 1, 0, tzinfo=UTC))
    argon_post = up.get_pricing_entry(model_name="gemini-4-argon", provider="google")
    flash_still_promo = up.get_pricing_entry(model_name="gemini-3.8-flash", provider="google")
    neon_steady = up.get_pricing_entry(model_name="gemini-4-neon", provider="google")
    assert argon_post is not None and argon_post.input_cost_per_million == Decimal("4.00")
    assert argon_post.output_cost_per_million == Decimal("20.00")
    assert argon_post.cache_read_cost_per_million == Decimal("0.20")
    assert flash_still_promo is not None and flash_still_promo.input_cost_per_million == Decimal("0.75")
    assert neon_steady is not None and neon_steady.input_cost_per_million == Decimal("3.00")

    monkeypatch.setattr(up, "_UTC_NOW", lambda: datetime(2027, 1, 1, 0, tzinfo=UTC))
    flash_post = up.get_pricing_entry(model_name="gemini-3.8-flash", provider="google")
    assert flash_post is not None and flash_post.input_cost_per_million == Decimal("1.50")
    assert flash_post.output_cost_per_million == Decimal("7.50")
    assert flash_post.cache_read_cost_per_million == Decimal("0.15")


def test_vertex_flex_timeout_cap_and_circuit_breaker(monkeypatch):
    """RCA 2026-11-25 regression: Flex must NEVER inherit _COMPRESSION_TIMEOUT_FLOOR_SECONDS (300s)
    and must trip a process-local circuit breaker after consecutive failures to bypass Flex directly
    to standard Vertex without burning 300s watchdog stalls."""
    from agent.auxiliary_client import (
        _COMPRESSION_TIMEOUT_FLOOR_SECONDS,
        _VertexFlexCompletionsAdapter,
    )

    _VertexFlexCompletionsAdapter.reset_breaker()
    monkeypatch.delenv("HERMES_VERTEX_FLEX_TIMEOUT_S", raising=False)
    monkeypatch.delenv("HERMES_VERTEX_FLEX_BREAKER_THRESHOLD", raising=False)
    monkeypatch.delenv("HERMES_VERTEX_FLEX_BREAKER_COOLDOWN_S", raising=False)

    seen_timeouts: list[float] = []
    flex_calls = {"n": 0}
    fb_calls = {"n": 0}

    def _fake_fb(**kw):
        fb_calls["n"] += 1
        return SimpleNamespace(
            id=f"std-{fb_calls['n']}",
            choices=[SimpleNamespace(index=0, message=SimpleNamespace(role="assistant", content="std ok"), finish_reason="stop")],
        )

    fb_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_fake_fb)))
    adapter = _VertexFlexCompletionsAdapter(fb_client, "google/gemini-3.8-flash")

    def _slow_timeout(**kw):
        flex_calls["n"] += 1
        seen_timeouts.append(kw["timeout"])
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(va, "call_vertex_native_flex", _slow_timeout)

    # Call 1: caller passes 300.0s floor -> Flex must be capped at 25.0s (strictly <= 30.0s)
    r1 = adapter.create(
        model="google/gemini-3.8-flash",
        messages=[{"role": "user", "content": "compress 1"}],
        timeout=_COMPRESSION_TIMEOUT_FLOOR_SECONDS,
    )
    assert r1.id == "std-1"
    assert seen_timeouts == [25.0]
    assert seen_timeouts[0] <= 30.0

    # Smaller caller timeout (12.0s) is respected rather than raised to 25.0s
    r2 = adapter.create(
        model="google/gemini-3.8-flash",
        messages=[{"role": "user", "content": "compress 2"}],
        timeout=12.0,
    )
    assert r2.id == "std-2"
    assert seen_timeouts == [25.0, 12.0]
    assert flex_calls["n"] == 2

    # Call 3: circuit breaker is now OPEN after 2 consecutive failures -> Flex NOT called at all
    r3 = adapter.create(
        model="google/gemini-3.8-flash",
        messages=[{"role": "user", "content": "compress 3"}],
        timeout=_COMPRESSION_TIMEOUT_FLOOR_SECONDS,
    )
    assert r3.id == "std-3"
    assert flex_calls["n"] == 2, "Circuit breaker failed to bypass Flex after 2 consecutive failures!"
    assert fb_calls["n"] == 3

    # After cooldown expires (simulated via reset) and a Flex call succeeds, breaker resets cleanly
    _VertexFlexCompletionsAdapter.reset_breaker()
    monkeypatch.setattr(
        va,
        "call_vertex_native_flex",
        lambda **kw: SimpleNamespace(
            id="vflex-recovered",
            service_tier="flex",
            choices=[SimpleNamespace(index=0, message=SimpleNamespace(role="assistant", content="flex ok"), finish_reason="stop")],
        ),
    )
    r4 = adapter.create(
        model="google/gemini-3.8-flash",
        messages=[{"role": "user", "content": "compress 4"}],
        timeout=_COMPRESSION_TIMEOUT_FLOOR_SECONDS,
    )
    assert r4.id == "vflex-recovered"
    _VertexFlexCompletionsAdapter.reset_breaker()
