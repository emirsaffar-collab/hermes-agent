"""Regression tests for ContextVar & singleton preservation across importlib.reload (#CF-79)."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import agent.context_compressor as cc_mod
import agent.conversation_compression as conv_comp


def test_reload_mid_summary_dispatch_preserves_contextvar_and_resets_cleanly():
    """Reloading agent.conversation_compression while _run_summary_dispatch is in-flight
    must NOT raise ValueError('<Token ...> was created by a different ContextVar')."""
    orig_var = conv_comp._COMPRESSOR_ATTEMPT_GENERATION
    orig_lock = conv_comp._COMPRESSOR_ATTEMPT_LOCK
    orig_adm_lock = conv_comp._compress_admission_lock

    dummy_compressor = SimpleNamespace()
    dummy_agent = SimpleNamespace(session_id="test_cf79", context_compressor=dummy_compressor)

    seen_inside = {}

    def _fake_compress_fn(messages, **_kwargs):
        seen_inside["gen_before_reload"] = conv_comp._COMPRESSOR_ATTEMPT_GENERATION.get()
        # Simulate or-credit-clamp / hot-reload reloading agent.conversation_compression mid-call
        importlib.reload(conv_comp)
        seen_inside["gen_after_reload"] = conv_comp._COMPRESSOR_ATTEMPT_GENERATION.get()
        seen_inside["same_var"] = conv_comp._COMPRESSOR_ATTEMPT_GENERATION is orig_var
        seen_inside["same_lock"] = conv_comp._COMPRESSOR_ATTEMPT_LOCK is orig_lock
        seen_inside["same_adm_lock"] = conv_comp._compress_admission_lock is orig_adm_lock
        return list(messages)

    out = conv_comp._run_summary_dispatch(
        dummy_agent,
        [{"role": "user", "content": "hi"}],
        _fake_compress_fn,
        {},
        commit_fence=None,
        attempt_generation=42,
        hard_cancel_event=None,
    )
    assert out == [{"role": "user", "content": "hi"}]
    assert seen_inside == {
        "gen_before_reload": 42,
        "gen_after_reload": 42,
        "same_var": True,
        "same_lock": True,
        "same_adm_lock": True,
    }
    assert conv_comp._COMPRESSOR_ATTEMPT_GENERATION.get() is None


def test_pin_summary_route_survives_context_compressor_reload():
    """Reloading agent.context_compressor inside pin_summary_route must reset cleanly."""
    orig_pin_var = cc_mod._SUMMARY_ROUTE_PIN
    route = {"provider": "vertex", "model": "google/gemini-3.8-flash"}
    with cc_mod.pin_summary_route(route):
        importlib.reload(cc_mod)
        assert cc_mod._SUMMARY_ROUTE_PIN is orig_pin_var
        assert cc_mod._SUMMARY_ROUTE_PIN.get() == route
    assert cc_mod._SUMMARY_ROUTE_PIN.get() is None


def test_token_var_reset_survives_forced_contextvar_replacement():
    """Even if _COMPRESSOR_ATTEMPT_GENERATION global is forcibly replaced mid-dispatch
    (pre-CF-79 reload behavior), _attempt_ctx_token.var.reset(_attempt_ctx_token) succeeds
    whereas calling reset() on the replacement ContextVar raises ValueError."""
    import contextvars
    import pytest

    saved_var = conv_comp._COMPRESSOR_ATTEMPT_GENERATION
    dummy_compressor = SimpleNamespace()
    dummy_agent = SimpleNamespace(session_id="test_cf79_forced", context_compressor=dummy_compressor)

    def _force_replace_compress_fn(messages, **_kwargs):
        old_tok = saved_var.set(99)
        replacement_var = contextvars.ContextVar("hermes_compressor_attempt_generation", default=None)
        # Prove negative control: resetting old_tok on replacement_var raises ValueError
        with pytest.raises(ValueError, match="was created by a different ContextVar"):
            replacement_var.reset(old_tok)
        old_tok.var.reset(old_tok)
        conv_comp._COMPRESSOR_ATTEMPT_GENERATION = replacement_var
        return list(messages)

    try:
        out = conv_comp._run_summary_dispatch(
            dummy_agent,
            [{"role": "user", "content": "polarity"}],
            _force_replace_compress_fn,
            {},
            commit_fence=None,
            attempt_generation=7,
            hard_cancel_event=None,
        )
        assert out == [{"role": "user", "content": "polarity"}]
        assert saved_var.get() is None
    finally:
        conv_comp._COMPRESSOR_ATTEMPT_GENERATION = saved_var

