"""Regression tests for VG-02 (fallback compression Flex parity) & VG-03 (Vertex Sync Flex 0.5x pricing/ledger parity)."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from types import SimpleNamespace

from agent import aux_accounting, auxiliary_client, usage_pricing


def test_resolve_fallback_entry_preserves_compression_flex_client(monkeypatch):
    """VG-02: Vertex fallback entry for task='compression' must wrap in VertexFlexCompressionAuxiliaryClient,
    while non-compression tasks must stay on the standard client."""
    monkeypatch.setenv("HERMES_VERTEX_COMPRESSION_FLEX", "1")
    monkeypatch.setattr("agent.vertex_adapter.has_vertex_credentials", lambda: True)
    monkeypatch.setattr(
        "agent.vertex_adapter.get_vertex_config",
        lambda region=None: ("fake-token", "https://aiplatform.googleapis.com/v1beta1/projects/p/locations/global/endpoints/openapi"),
    )
    monkeypatch.setattr("agent.vertex_adapter.vertex_access_token", lambda: "fake-token")

    entry = {"provider": "vertex", "model": "google/gemini-3.8-flash"}

    comp_client, comp_model = auxiliary_client._resolve_fallback_entry(entry, task="compression")
    assert comp_model == "google/gemini-3.8-flash"
    assert isinstance(comp_client, auxiliary_client.VertexFlexCompressionAuxiliaryClient)

    title_client, title_model = auxiliary_client._resolve_fallback_entry(entry, task="title_generation")
    assert title_model == "google/gemini-3.8-flash"
    assert not isinstance(title_client, auxiliary_client.VertexFlexCompressionAuxiliaryClient)


def test_fallback_chains_forward_task_to_resolve_fallback_entry(monkeypatch):
    """VG-02: Both configured and main fallback chains must pass task='compression' to _resolve_fallback_entry."""
    seen_tasks: list[str | None] = []

    def fake_resolve(entry, *, task=None):
        seen_tasks.append(task)
        return SimpleNamespace(base_url="https://aiplatform.googleapis.com/v1"), entry["model"]

    monkeypatch.setattr(auxiliary_client, "_resolve_fallback_entry", fake_resolve)
    monkeypatch.setattr(auxiliary_client, "_is_provider_unhealthy", lambda *a, **k: False)
    monkeypatch.setattr(auxiliary_client, "_context_too_small", lambda *a, **k: None)
    monkeypatch.setattr(
        auxiliary_client,
        "_get_auxiliary_task_config",
        lambda task: {"fallback_chain": [{"provider": "vertex", "model": "google/gemini-3.8-flash"}]},
    )

    fb_client, fb_model, _ = auxiliary_client._try_configured_fallback_chain("compression", "openrouter")
    assert fb_client is not None and fb_model == "google/gemini-3.8-flash"

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", dict)
    monkeypatch.setattr(
        "hermes_cli.fallback_config.get_fallback_chain",
        lambda cfg: [{"provider": "vertex", "model": "google/gemini-3.8-flash"}],
    )
    main_client, main_model, _ = auxiliary_client._try_main_fallback_chain("compression", "openrouter")
    assert main_client is not None and main_model == "google/gemini-3.8-flash"

    assert seen_tasks == ["compression", "compression"]


def test_vertex_flex_usage_pricing_applies_half_rate():
    """VG-03: estimate_usage_cost must apply exact 0.5x pricing when Vertex serves service_tier='flex'."""
    raw = SimpleNamespace(prompt_tokens=100_000, completion_tokens=10_000, total_tokens=110_000)
    std_usage = usage_pricing.normalize_usage(raw, provider="vertex")
    flex_resp = SimpleNamespace(service_tier="flex")
    flex_usage = usage_pricing.with_served_service_tier(std_usage, flex_resp)

    std_cost = usage_pricing.estimate_usage_cost("google/gemini-3.8-flash", std_usage, provider="vertex")
    flex_cost = usage_pricing.estimate_usage_cost("google/gemini-3.8-flash", flex_usage, provider="vertex")

    assert std_cost.amount_usd is not None and std_cost.amount_usd > 0
    assert flex_cost.amount_usd is not None
    assert flex_cost.amount_usd == std_cost.amount_usd * Decimal("0.5")
    assert flex_cost.pricing_version is not None and flex_cost.pricing_version.endswith("-flex-0.5x")


def test_aux_accounting_persists_flex_tier_and_half_cost_to_ledger(monkeypatch, tmp_path):
    """VG-03: End-to-end aux_accounting -> realtime_spend_breaker SQLite round-trip must record
    service_tier='flex' and 0.5x net_eur / estimated_cost_usd."""
    db_path = tmp_path / "ai-spend-realtime.db"
    monkeypatch.setenv("HERMES_AI_SPEND_DB_PATH", str(db_path))
    monkeypatch.setattr("hermes_cli.config.load_config", dict)

    rows_std: list[dict] = []
    rows_flex: list[dict] = []

    raw_usage = SimpleNamespace(prompt_tokens=100_000, completion_tokens=10_000, total_tokens=110_000)
    base_url = "https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi"

    std_resp = SimpleNamespace(id="chatcmpl-std-001", model="google/gemini-3.8-flash", usage=raw_usage)
    flex_resp = SimpleNamespace(
        id="vflex-test-002", model="google/gemini-3.8-flash", service_tier="flex", usage=raw_usage,
    )

    db_std = SimpleNamespace(record_auxiliary_usage=lambda *a, **kw: rows_std.append(kw))
    tok = aux_accounting.set_accounting_context(db_std, "sess-std")
    try:
        aux_accounting.record_aux_usage(std_resp, "compression", provider="vertex", base_url=base_url)
    finally:
        aux_accounting.reset_accounting_context(tok)

    db_flex = SimpleNamespace(record_auxiliary_usage=lambda *a, **kw: rows_flex.append(kw))
    tok = aux_accounting.set_accounting_context(db_flex, "sess-flex")
    try:
        aux_accounting.record_aux_usage(flex_resp, "compression", provider="vertex", base_url=base_url)
    finally:
        aux_accounting.reset_accounting_context(tok)

    assert len(rows_std) == 1 and len(rows_flex) == 1
    assert rows_std[0]["estimated_cost_usd"] is not None
    assert rows_flex[0]["estimated_cost_usd"] == rows_std[0]["estimated_cost_usd"] * 0.5

    con = sqlite3.connect(str(db_path))
    ledger = {
        r[0]: (r[1], r[2])
        for r in con.execute("SELECT session_id, service_tier, net_eur FROM llm_call_ledger").fetchall()
    }
    con.close()
    assert ledger["sess-std"][0] == ""
    assert ledger["sess-flex"][0] == "flex"
    assert abs(ledger["sess-flex"][1] - (ledger["sess-std"][1] * 0.5)) < 1e-5
