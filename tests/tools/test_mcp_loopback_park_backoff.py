"""Tests for loopback HTTP parked self-probe backoff bounds (RCA 2026-11-26)."""
import pytest


def _make_task(url: str | None, command: str | None = None):
    from tools.mcp_tool import MCPServerTask

    cfg = {}
    if url is not None:
        cfg["url"] = url
    if command is not None:
        cfg["command"] = command
    task = MCPServerTask("test-srv")
    task._config = cfg
    return task


@pytest.mark.parametrize(
    "url,expected_loopback",
    [
        ("http://127.0.0.1:8082/mcp", True),
        ("http://localhost:8086/mcp", True),
        ("http://[::1]:8087/mcp", True),
        ("https://mcp.example.com/mcp", False),
        ("http://100.117.81.127:8080/mcp", False),
    ],
)
def test_is_loopback_http_classification(url, expected_loopback):
    task = _make_task(url=url)
    assert task._is_loopback_http() is expected_loopback


def test_stdio_is_not_loopback_http():
    task = _make_task(url=None, command="python3")
    assert task._is_loopback_http() is False


def test_loopback_park_backoff_capped_at_5s_to_60s():
    task = _make_task(url="http://127.0.0.1:8082/mcp")
    waits = []
    for failures in range(7):
        task._parked_probe_failures = failures
        waits.append(task._next_park_probe_timeout())
    assert waits == [5, 10, 20, 40, 60, 60, 60]


def test_remote_and_stdio_park_backoff_unchanged():
    for task in (
        _make_task(url="https://remote.example.com/mcp"),
        _make_task(url=None, command="npx"),
    ):
        waits = []
        for failures in range(6):
            task._parked_probe_failures = failures
            waits.append(task._next_park_probe_timeout())
        assert waits == [300, 600, 1200, 2400, 3600, 3600]


def test_loopback_respects_lower_monkeypatched_interval(monkeypatch):
    """Existing fast tests that monkeypatch _PARKED_RETRY_INTERVAL=0.05 stay fast on loopback."""
    from tools import mcp_tool

    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 0.05)
    task = _make_task(url="http://127.0.0.1:8082/mcp")
    task._parked_probe_failures = 0
    assert task._next_park_probe_timeout() == 0.05
