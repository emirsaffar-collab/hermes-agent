"""Tests for supervised gateway idle code-skew respawn (exit 75 via restart_when_idle)."""

from __future__ import annotations

import asyncio

import pytest

from gateway import code_skew_exit as cse
from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
from gateway.restart_when_idle import request_service_restart
from gateway.run_shutdown import _resolve_gateway_exit_verdict

_SKEW = ("aaaaaaaaaa", "bbbbbbbbbb")


class _StubRunner:
    def __init__(self, *, awaitable_work: int = 0):
        self._running = True
        self._draining = False
        self._restart_requested = False
        self._restart_detached = False
        self._restart_via_service = False
        self._restart_task_started = False
        self._restart_after_turn_timeout = 60.0
        self._stop_task = None
        self._restart_task: asyncio.Task | None = None
        self._awaitable_work = awaitable_work
        self.should_exit_with_failure = False
        self._exit_code: int | None = None
        self._exit_reason: str | None = None
        self.stop_calls: list[dict] = []

    exit_code = property(lambda self: self._exit_code)

    def _awaitable_work_count(self) -> int:
        return self._awaitable_work

    def _describe_active_work(self) -> list[dict]:
        return [{"kind": "turn", "session": "s1"}] if self._awaitable_work > 0 else []

    def _scale_to_zero_status(self, *_args, **_kwargs) -> None:
        pass

    async def stop(self, **kwargs):
        self.stop_calls.append(kwargs)
        self._running = False


def _run_watcher(
    skews,
    *,
    supervised: bool = True,
    update: bool | Exception = False,
    predates: bool = False,
    schedule_restart=None,
    runner=None,
):
    runner = runner or _StubRunner()
    seq = iter(skews)
    scheduled: list[object] = []

    def _update_probe():
        if isinstance(update, Exception):
            raise update
        return update

    def _sched(r):
        scheduled.append(r)
        return True

    async def _go():
        await cse.code_skew_idle_watcher(
            runner,
            poll_s=0,
            skew_fn=lambda: next(seq, None),
            update_probe=_update_probe,
            predates=lambda: predates,
            supervised_probe=lambda: supervised,
            schedule_restart=schedule_restart or _sched,
            max_polls=len(skews) + 1,
        )
        task = getattr(runner, "_restart_task", None)
        if task is not None:
            await task

    asyncio.run(_go())
    return runner, scheduled


def test_confirmed_skew_on_supervised_gateway_schedules_idle_service_restart():
    runner, scheduled = _run_watcher([_SKEW, _SKEW], supervised=True)
    assert scheduled == [runner]
    assert "exit 75" in (runner._exit_reason or "")
    assert "aaaaaaaaaa" in (runner._exit_reason or "") and "bbbbbbbbbb" in (runner._exit_reason or "")


def test_unsupervised_manual_dev_gateway_never_self_terminates():
    runner, scheduled = _run_watcher([_SKEW, _SKEW, _SKEW], supervised=False)
    assert scheduled == []
    assert runner._exit_reason is None


def test_negative_controls_blip_active_update_lock_error_and_downgrade():
    # Single blip -> no restart
    assert _run_watcher([_SKEW, None, _SKEW, None])[1] == []
    # Active update lock -> no restart
    assert _run_watcher([_SKEW, _SKEW, _SKEW], update=True)[1] == []
    # Unreadable update lock -> fail-closed, no restart
    assert _run_watcher([_SKEW, _SKEW, _SKEW], update=OSError("lock unreadable"))[1] == []
    # Pre-runtime downgrade -> deferred to runtime_downgrade_watcher
    assert _run_watcher([_SKEW, _SKEW, _SKEW], predates=True)[1] == []


def test_real_restart_when_idle_waits_for_active_work_and_resolves_exit_75(monkeypatch):
    monkeypatch.setattr("gateway.restart_when_idle.background_units", lambda _r: [])
    runner = _StubRunner(awaitable_work=1)
    seq = iter([_SKEW, _SKEW])

    async def _go():
        watcher_task = asyncio.create_task(
            cse.code_skew_idle_watcher(
                runner,
                poll_s=0,
                skew_fn=lambda: next(seq, None),
                update_probe=lambda: False,
                predates=lambda: False,
                supervised_probe=lambda: True,
                schedule_restart=request_service_restart,
                max_polls=4,
            )
        )
        await watcher_task
        # Watcher armed restart_when_idle, but active work is still running -> NOT draining yet!
        assert runner._restart_requested is True
        assert runner._draining is False
        assert runner.stop_calls == []
        # Finish active work -> restart_when_idle detects idle, closes admission, and stops with service_restart=True
        runner._awaitable_work = 0
        await runner._restart_task

    asyncio.run(_go())
    assert runner._draining is True
    assert runner.stop_calls == [{"restart": True, "detached_restart": False, "service_restart": True}]
    with pytest.raises(SystemExit) as exc:
        _resolve_gateway_exit_verdict(runner, signal_initiated_shutdown=False)
    assert exc.value.code == GATEWAY_SERVICE_RESTART_EXIT_CODE
