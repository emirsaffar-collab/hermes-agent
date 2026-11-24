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

    # compression + gemini-3.8-flash -> wrapped in VertexFlexCompressionAuxiliaryClient
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
