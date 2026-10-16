"""Unit tests for agent/provider_latency_circuit_breaker.py."""
from agent.provider_latency_circuit_breaker import (
    adjust_provider_order,
    is_provider_demoted,
    record_provider_failure,
    record_provider_latency,
    reset_circuit_breaker,
)


def test_provider_latency_circuit_breaker_healthy():
    reset_circuit_breaker()
    # Fast requests do not trigger demotion
    record_provider_latency("z-ai/glm-5.3-flash", "Novita", 3.5)
    record_provider_latency("z-ai/glm-5.3-flash", "Novita", 4.0)
    assert not is_provider_demoted("z-ai/glm-5.3-flash", "Novita")
    assert adjust_provider_order("z-ai/glm-5.3-flash", ["Novita", "Relace"]) == ["Novita", "Relace"]


def test_provider_latency_circuit_breaker_demotes_degraded():
    reset_circuit_breaker()
    # Slow requests trigger demotion
    record_provider_latency("z-ai/glm-5.3-flash", "Novita", 35.0)
    record_provider_latency("z-ai/glm-5.3-flash", "Novita", 45.0)
    assert is_provider_demoted("z-ai/glm-5.3-flash", "Novita")

    # Demoted provider gets moved to the end of the order
    order = ["Novita", "Relace", "Phala"]
    assert adjust_provider_order("z-ai/glm-5.3-flash", order) == ["Relace", "Phala", "Novita"]


def test_provider_latency_circuit_breaker_record_failure():
    reset_circuit_breaker()
    record_provider_failure("z-ai/glm-5.3-flash", "Novita")
    assert is_provider_demoted("z-ai/glm-5.3-flash", "Novita")
    order = ["Novita", "Relace"]
    assert adjust_provider_order("z-ai/glm-5.3-flash", order) == ["Relace", "Novita"]


def test_provider_latency_circuit_breaker_case_insensitive():
    reset_circuit_breaker()
    record_provider_latency("Z-AI/GLM-5.3-FLASH", "novita", 30.0)
    record_provider_latency("z-ai/glm-5.3-flash", "NOVITA", 40.0)
    assert is_provider_demoted("z-ai/glm-5.3-flash", "Novita")
    assert adjust_provider_order("z-ai/glm-5.3-flash", ["Novita", "Relace"]) == ["Relace", "Novita"]


def test_provider_latency_circuit_breaker_suppresses_repeat_warnings_while_demoted(caplog):
    """Once a (model, provider) pair enters active cooldown, subsequent slow calls
    refresh the cooldown without re-emitting WARNING on every turn (prevents 70+
    warnings/10m on single-provider reasoning models like google/gemini-4-argon @ Google
    from tripping provider-latency-storm-watchdog)."""
    import logging

    reset_circuit_breaker()
    with caplog.at_level(logging.WARNING, logger="agent.provider_latency_circuit_breaker"):
        record_provider_latency("google/gemini-4-argon", "Google", 25.0)
        record_provider_latency("google/gemini-4-argon", "Google", 28.0)  # transitions to demoted -> 1st WARNING
        record_provider_latency("google/gemini-4-argon", "Google", 31.0)  # already demoted -> must NOT warn again
        record_provider_latency("google/gemini-4-argon", "Google", 24.0)  # already demoted -> must NOT warn again
        # Flapping across the 20.0s boundary within the same 600s window must also NOT re-warn:
        record_provider_latency("google/gemini-4-argon", "Google", 10.0)
        record_provider_latency("google/gemini-4-argon", "Google", 10.0)
        record_provider_latency("google/gemini-4-argon", "Google", 10.0)  # avg drops to 17.0s < 20s (early recovery)
        record_provider_latency("google/gemini-4-argon", "Google", 35.0)
        record_provider_latency("google/gemini-4-argon", "Google", 35.0)  # avg jumps to 20.0s >= 20s again within 600s

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"Expected exactly 1 transition warning while in 600s window, got {len(warnings)}: {warnings}"
    assert is_provider_demoted("google/gemini-4-argon", "Google")

