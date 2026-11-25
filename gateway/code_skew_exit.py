"""Idle service-restart watcher when a supervised gateway's checkout moves forward on disk.

When ``git pull`` / ``hermes update`` advances the checkout underneath a long-lived supervised
gateway (launchd / systemd / s6 / ``--external-supervisor``), lazy imports can load new modules
into a process whose ``sys.modules`` was frozen at boot. Hot-reload handles stateless modules,
but stateful modules (``gateway.run``, ``agent.*``, ``ContextVar`` owners) stay on the boot
snapshot until the process respawns.

This watcher mirrors ``gateway.runtime_downgrade_exit`` and ``hermes_cli.web_server_skew_exit``:
- Polls ``gateway.code_skew.detect_code_skew()`` + ``hermes_cli.update_lock.update_in_progress()``.
- Requires ``_CONFIRMATIONS = 2`` consecutive skewed reads with no update in flight.
- Gated strictly on ``is_gateway_supervisor_process()`` so manual dev runs never self-terminate.
- Defers pre-runtime downgrades (``tree_predates_runtime()``) to ``runtime_downgrade_watcher``.
- Never interrupts live turns, background delegations, or 2400s cron runs: delegates to
  ``gateway.restart_when_idle.request_service_restart(runner)``, which keeps serving normally
  until the gateway is naturally idle, then closes admission and exits with
  ``GATEWAY_SERVICE_RESTART_EXIT_CODE = 75`` (``EX_TEMPFAIL``) so ``launchd``
  (``KeepAlive.SuccessfulExit=false``) and ``systemd`` respawn it cleanly on the new code.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from gateway.restart import is_gateway_supervisor_process
from gateway.restart_when_idle import request_service_restart
from gateway.runtime_downgrade_exit import tree_predates_runtime

logger = logging.getLogger(__name__)

POLL_S = 30.0
_CONFIRMATIONS = 2


def skew_restart_reason(
    skew,
    *,
    update_in_progress: bool,
    predates: Callable[[], bool],
) -> Optional[str]:
    """Return a human-readable restart reason on confirmed forward code skew, else ``None``."""
    if not skew or update_in_progress or predates():
        return None
    return (
        "Supervised gateway loaded {}, but its checkout on disk is now at {}; "
        "scheduling idle service restart (exit 75) to respawn on the new code."
    ).format(*tuple(skew))


async def code_skew_idle_watcher(
    runner,
    *,
    poll_s: float = POLL_S,
    skew_fn=None,
    update_probe=None,
    predates: Callable[[], bool] = tree_predates_runtime,
    supervised_probe: Callable[[], bool] = is_gateway_supervisor_process,
    schedule_restart: Callable[[object], bool] = request_service_restart,
    max_polls: Optional[int] = None,
) -> None:
    """Supervised gateway watcher: schedule an idle service restart (exit 75) after
    ``_CONFIRMATIONS`` consecutive code-skew observations with no active update lock."""
    try:
        if not supervised_probe():
            logger.debug("code_skew_idle_watcher: unsupervised launch; idle skew respawn disabled")
            return
    except Exception:  # health: allow BLE001 -- cannot prove supervisor owns restart policy
        return

    if skew_fn is None:
        from gateway.code_skew import detect_code_skew as skew_fn
    if update_probe is None:
        from hermes_cli.update_lock import update_in_progress as update_probe
    observe_skew: Callable = skew_fn
    observe_update: Callable[[], bool] = update_probe

    def _read() -> Optional[str]:
        try:
            busy = observe_update()
        except Exception:  # health: allow BLE001 -- unreadable update lock cannot prove swap finished
            busy = True
        return skew_restart_reason(observe_skew(), update_in_progress=busy, predates=predates)

    seen = polls = 0
    while getattr(runner, "_running", False) and (max_polls is None or polls < max_polls):
        polls += 1
        reason = await asyncio.to_thread(_read)
        seen = seen + 1 if reason else 0
        if seen >= _CONFIRMATIONS:
            logger.info("%s", reason)
            runner._exit_reason = reason
            schedule_restart(runner)
            return
        await asyncio.sleep(poll_s)
