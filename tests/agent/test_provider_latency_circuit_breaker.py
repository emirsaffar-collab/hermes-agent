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
