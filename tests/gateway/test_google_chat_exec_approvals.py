"""Two-way polarity tests for Google Chat interactive exec-approval cards,
button-only approval enforcement, forged plain-text /approve rejection,
and read/draft-only toolset isolation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import ExecApprovalPrompt, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.slash_commands import GatewaySlashCommandsMixin
from plugins.platforms.google_chat.adapter import GoogleChatAdapter
from tools import approval as approval_mod
from tools import approval_context
from toolsets import TOOLSETS


def _make_adapter(*, allowed_ids: set[str] | None = None) -> GoogleChatAdapter:
    cfg = PlatformConfig(
        enabled=True,
        extra={
            "service_account_json": "/dev/null",
            "http_events_url": "https://example.invalid/google-chat/events",
            "http_events_service_account_email": "chat@system.gserviceaccount.com",
        },
    )
    adapter = GoogleChatAdapter(cfg)
    allowed = allowed_ids if allowed_ids is not None else {"users/109155536057359579735"}
    adapter.set_authorization_check(lambda uid, _ct, _cid, **_kw: uid in allowed)
    return adapter


def _sample_prompt(
    *,
    session_key: str = "google_chat:dm:spaces/DM_EMIR",
    chat_id: str = "spaces/DM_EMIR",
    request_id: str = "req-abc123",
    command: str = "git status",
) -> ExecApprovalPrompt:
    return ExecApprovalPrompt(
        chat_id=chat_id,
        command=command,
        description="Terminal execution requires interactive button approval",
        session_key=session_key,
        request_id=request_id,
        smart_denied=False,
        text="Approve command: `git status`",
        actions=[
            ("Approve once", "once", "primary"),
            ("Allow session", "session", "secondary"),
            ("Always allow", "always", "secondary"),
            ("Deny", "deny", "danger"),
        ],
    )


def _addon_click(
    *,
    approval_id: str,
    choice: str,
    request_id: str = "req-abc123",
    user_id: str = "users/109155536057359579735",
    user_email: str = "emir.saffar@uropenn.se",
    user_type: str = "HUMAN",
    space_name: str = "spaces/DM_EMIR",
    space_type: str = "DIRECT_MESSAGE",
    invoked_function: str = "hermes_exec_approval",
) -> dict:
    return {
        "commonEventObject": {
            "invokedFunction": invoked_function,
            "parameters": {
                "approval_id": approval_id,
                "choice": choice,
                "request_id": request_id,
            },
        },
        "chat": {
            "user": {"name": user_id, "email": user_email, "type": user_type},
            "buttonClickedPayload": {
                "space": {"name": space_name, "spaceType": space_type},
            },
        },
    }


def _legacy_click(
    *,
    approval_id: str,
    choice: str,
    request_id: str = "req-abc123",
    user_id: str = "users/109155536057359579735",
    space_name: str = "spaces/DM_EMIR",
) -> dict:
    return {
        "type": "CARD_CLICKED",
        "user": {"name": user_id, "email": "emir.saffar@uropenn.se", "type": "HUMAN"},
        "space": {"name": space_name, "type": "DM"},
        "action": {
            "actionMethodName": "hermes_exec_approval",
            "parameters": [
                {"key": "approval_id", "value": approval_id},
                {"key": "choice", "value": choice},
                {"key": "request_id", "value": request_id},
            ],
        },
    }


def test_p1_adapter_declares_exec_approval_button_support():
    adapter = _make_adapter()
    assert adapter.supports_exec_approval_buttons() is True


@pytest.mark.asyncio
async def test_p2_send_exec_approval_prompt_restricts_choices_on_button_only_platform():
    adapter = _make_adapter()
    sent_cards: list[dict] = []

    async def fake_send_card(chat_id, card, **kwargs):
        sent_cards.append(card)
        return SendResult(success=True, message_id="spaces/DM_EMIR/messages/m1")

    adapter.send_card = fake_send_card  # type: ignore[method-assign]
    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        res = await adapter._send_exec_approval_prompt(_sample_prompt())

    assert res.success is True
    assert len(sent_cards) == 1
    buttons = sent_cards[0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    choices = [
        next(p["value"] for p in b["onClick"]["action"]["parameters"] if p["key"] == "choice")
        for b in buttons
    ]
    # Button-only mode MUST strip session/always persistence choices.
    assert choices == ["once", "deny"]
    assert len(adapter._approval_state) == 1


@pytest.mark.asyncio
async def test_p3_authorized_addon_click_resolves_and_updates_card():
    adapter = _make_adapter()
    adapter.send_card = AsyncMock(return_value=SendResult(success=True, message_id="m1"))  # type: ignore[method-assign]

    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        await adapter._send_exec_approval_prompt(_sample_prompt())
        approval_id = next(iter(adapter._approval_state))

        with patch("tools.approval.resolve_gateway_approval_card", return_value=1) as mock_resolve:
            resp = await adapter.dispatch_http_event(
                _addon_click(approval_id=approval_id, choice="once")
            )

    mock_resolve.assert_called_once_with(
        "google_chat:dm:spaces/DM_EMIR",
        "once",
        "req-abc123",
        actor_id="users/109155536057359579735",
        extra=adapter.config.extra,
        admitted=True,
    )
    assert approval_id not in adapter._approval_state
    cards = resp["hostAppDataAction"]["chatDataAction"]["updateMessageAction"]["message"]["cardsV2"]
    assert cards[0]["card"]["header"]["title"] == "Command Approved (once)"
    # Settled card must NOT contain interactive buttonList widgets.
    widgets = cards[0]["card"]["sections"][0]["widgets"]
    assert all("buttonList" not in w for w in widgets)


@pytest.mark.asyncio
async def test_p4_authorized_legacy_card_clicked_deny_resolves():
    adapter = _make_adapter()
    adapter.send_card = AsyncMock(return_value=SendResult(success=True, message_id="m1"))  # type: ignore[method-assign]

    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        await adapter._send_exec_approval_prompt(_sample_prompt())
        approval_id = next(iter(adapter._approval_state))

        with patch("tools.approval.resolve_gateway_approval_card", return_value=1) as mock_resolve:
            resp = await adapter.dispatch_http_event(
                _legacy_click(approval_id=approval_id, choice="deny")
            )

    mock_resolve.assert_called_once()
    assert resp["actionResponse"]["type"] == "UPDATE_MESSAGE"
    assert resp["cardsV2"][0]["card"]["header"]["title"] == "Command Denied"


@pytest.mark.asyncio
async def test_p5_unauthorized_actor_and_bot_click_fail_closed_without_dos():
    adapter = _make_adapter()
    adapter.send_card = AsyncMock(return_value=SendResult(success=True, message_id="m1"))  # type: ignore[method-assign]

    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        await adapter._send_exec_approval_prompt(_sample_prompt())
        approval_id = next(iter(adapter._approval_state))

        with patch("tools.approval.resolve_gateway_approval_card", return_value=1) as mock_resolve:
            # Intruder human
            r1 = await adapter.dispatch_http_event(
                _addon_click(
                    approval_id=approval_id,
                    choice="once",
                    user_id="users/999999999999999999999",
                    user_email="intruder@example.com",
                )
            )
            # Bot spoof even with allowed ID
            r2 = await adapter.dispatch_http_event(
                _addon_click(
                    approval_id=approval_id,
                    choice="once",
                    user_type="BOT",
                )
            )

    assert r1 == {}
    assert r2 == {}
    mock_resolve.assert_not_called()
    # Pending state MUST survive unauthorized click attempts so the owner can still tap.
    assert approval_id in adapter._approval_state


@pytest.mark.asyncio
async def test_p6_cross_space_or_wrong_request_id_or_disallowed_choice_rejected():
    adapter = _make_adapter()
    adapter.send_card = AsyncMock(return_value=SendResult(success=True, message_id="m1"))  # type: ignore[method-assign]

    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        await adapter._send_exec_approval_prompt(_sample_prompt())
        approval_id = next(iter(adapter._approval_state))

        with patch("tools.approval.resolve_gateway_approval_card", return_value=1) as mock_resolve:
            r_space = await adapter.dispatch_http_event(
                _addon_click(approval_id=approval_id, choice="once", space_name="spaces/OTHER")
            )
            r_req = await adapter.dispatch_http_event(
                _addon_click(approval_id=approval_id, choice="once", request_id="req-tampered")
            )
            r_always = await adapter.dispatch_http_event(
                _addon_click(approval_id=approval_id, choice="always")
            )

    assert r_space == {}
    assert r_req == {}
    assert r_always == {}
    mock_resolve.assert_not_called()
    assert approval_id in adapter._approval_state


@pytest.mark.asyncio
async def test_p7_replay_after_resolution_returns_expired_without_resolving_twice():
    adapter = _make_adapter()
    adapter.send_card = AsyncMock(return_value=SendResult(success=True, message_id="m1"))  # type: ignore[method-assign]

    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        await adapter._send_exec_approval_prompt(_sample_prompt())
        approval_id = next(iter(adapter._approval_state))

        with patch("tools.approval.resolve_gateway_approval_card", return_value=1) as mock_resolve:
            await adapter.dispatch_http_event(_addon_click(approval_id=approval_id, choice="once"))
            r2 = await adapter.dispatch_http_event(_addon_click(approval_id=approval_id, choice="once"))

    assert mock_resolve.call_count == 1
    cards = r2["hostAppDataAction"]["chatDataAction"]["updateMessageAction"]["message"]["cardsV2"]
    assert cards[0]["card"]["header"]["title"] == "Approval Expired"


class _DummySlash(GatewaySlashCommandsMixin):
    def __init__(self):
        self.called = False

    def _blocking_approval_or_stale(self, event, stale_key, none_key, *, resolve_all=False, choice="approve"):
        self.called = True
        return "sess-1", None, ["req-1"]

    async def _deliver_approval_confirmation(self, event, text, kind):
        return text


@pytest.mark.asyncio
async def test_p8_forged_text_approve_rejected_on_button_only_platform_but_allowed_elsewhere():
    dummy = _DummySlash()
    gchat_ev = MessageEvent(
        text="/approve",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform("google_chat"),
            chat_id="spaces/DM_EMIR",
            chat_type="dm",
            user_id="users/109155536057359579735",
        ),
    )
    tg_ev = MessageEvent(
        text="/approve",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="12345",
        ),
    )

    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            # Negative polarity: Google Chat text /approve MUST be blocked
            out_gchat = await dummy._handle_approve_command(gchat_ev)
            assert "disabled" in (out_gchat or "").lower()
            assert dummy.called is False
            mock_resolve.assert_not_called()

            # Positive polarity: non-button-only platform still accepts /approve
            out_tg = await dummy._handle_approve_command(tg_ev)
            assert dummy.called is True
            mock_resolve.assert_called_once()
            assert out_tg


def test_p9_benign_terminal_command_still_requires_button_on_button_only_platform():
    """Even a benign command not matching DANGEROUS_PATTERNS (e.g. `touch /tmp/fleet-probe.txt`)
    MUST hit _human_decision with smart=False on require_button_platforms (ignoring permanent allowlist),
    while the same command still auto-approves on platforms NOT in require_button_platforms."""
    cmd = "touch /tmp/fleet-probe.txt"
    with patch.object(approval_context, "_get_approval_config", return_value={"mode": "manual", "require_button_platforms": ["google_chat"]}), \
         patch.object(approval_context, "_get_session_platform", return_value="google_chat"), \
         patch.object(approval_mod, "_presence", return_value=(None, False, True, False)), \
         patch.object(approval_mod, "get_current_session_key", return_value="google_chat:dm:spaces/DM_EMIR"), \
         patch.object(approval_mod, "_command_matches_permanent_allowlist", return_value=True), \
         patch.object(approval_mod, "_human_decision", return_value={"approved": False, "pending": True}) as mock_human:
        res_gchat = approval_mod.check_all_command_guards(cmd, "local")

    assert res_gchat["approved"] is False
    mock_human.assert_called_once()
    assert mock_human.call_args.kwargs["smart"] is False
    assert mock_human.call_args.kwargs["pattern_key"] == "button_required:terminal"

    # Opposite polarity: platform NOT in require_button_platforms auto-approves benign command
    with patch.object(approval_context, "_get_approval_config", return_value={"mode": "manual", "require_button_platforms": ["google_chat"]}), \
         patch.object(approval_context, "_get_session_platform", return_value="cli"), \
         patch.object(approval_mod, "_presence", return_value=(None, True, False, False)), \
         patch.object(approval_mod, "get_current_session_key", return_value="cli:main"), \
         patch.object(approval_mod, "_command_matches_permanent_allowlist", return_value=False), \
         patch.object(approval_mod, "_human_decision") as mock_human_cli:
        res_cli = approval_mod.check_all_command_guards(cmd, "local")

    assert res_cli["approved"] is True
    mock_human_cli.assert_not_called()


def test_p10_readonly_google_chat_toolset_blocks_direct_write_tools():
    spec = TOOLSETS["hermes-google-chat-readonly"]
    tools = set(spec["tools"])
    # Positive polarity: read/search/button-gated terminal present
    assert {"read_file", "search_files", "web_search", "session_search", "terminal"} <= tools
    # Negative polarity: all direct mutation/side-effect tools excluded
    forbidden = {
        "write_file",
        "patch",
        "execute_code",
        "cronjob_manage",
        "browser_exec",
        "computer_use",
        "memory",
        "skill_manage",
        "delegate_task",
    }
    assert tools.isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_p11_e2e_check_dangerous_command_to_google_chat_card_and_button_click(monkeypatch):
    """Real end-to-end test WITHOUT mocking tools.approval or resolve_gateway_approval_card:
    run check_dangerous_command() on a worker thread, deliver the card via register_gateway_notify
    -> BasePlatformAdapter.send_exec_approval -> GoogleChatAdapter._send_exec_approval_prompt,
    tap Approve via GoogleChatAdapter.dispatch_http_event, and verify check_dangerous_command
    unblocks with approved=True."""
    import asyncio
    import threading
    from tools.approval import (
        check_all_command_guards,
        register_gateway_notify,
        unregister_gateway_notify,
    )

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "google_chat")
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "dm")
    session_key = "google_chat:dm:spaces/DM_E2E_LIVE"
    adapter = _make_adapter()
    adapter.send_card = AsyncMock(return_value=SendResult(success=True, message_id="m-e2e"))  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    card_sent = asyncio.Event()
    captured_req_id: dict[str, str] = {}

    def _notify_cb(approval_data: dict) -> None:
        captured_req_id["id"] = str(approval_data.get("request_id") or "")

        async def _deliver() -> None:
            res = await adapter.send_exec_approval(
                chat_id="spaces/DM_E2E_LIVE",
                command=approval_data["command"],
                session_key=session_key,
                description=approval_data["description"],
                request_id=captured_req_id["id"],
            )
            assert res.success is True
            card_sent.set()

        asyncio.run_coroutine_threadsafe(_deliver(), loop)

    register_gateway_notify(session_key, _notify_cb)
    result_holder: dict = {}

    def _worker() -> None:
        tok_s = approval_context.set_current_session_key(session_key)
        try:
            result_holder["res"] = check_all_command_guards("git status", env_type="local")
        finally:
            approval_context.reset_current_session_key(tok_s)

    try:
        with patch.object(
            approval_context,
            "_get_approval_config",
            return_value={
                "mode": "manual",
                "require_button_platforms": ["google_chat"],
                "approvers": ["users/109155536057359579735"],
                "timeout_seconds": 10,
            },
        ):
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            await asyncio.wait_for(card_sent.wait(), timeout=5.0)

            approval_id = next(iter(adapter._approval_state))
            resp = await adapter.dispatch_http_event(
                _addon_click(
                    approval_id=approval_id,
                    choice="once",
                    request_id=captured_req_id["id"],
                    space_name="spaces/DM_E2E_LIVE",
                )
            )
            t.join(timeout=5.0)
        assert not t.is_alive()
        assert result_holder.get("res", {}).get("approved") is True
        cards = resp["hostAppDataAction"]["chatDataAction"]["updateMessageAction"]["message"]["cardsV2"]
        assert cards[0]["card"]["header"]["title"] == "Command Approved (once)"
    finally:
        unregister_gateway_notify(session_key)


def test_p12_fleet_control_disabled_toolsets_preserves_read_tools_and_blocks_writes():
    """Verify the exact fleet-control combination: hermes-google-chat-readonly minus
    disabled_toolsets retains read_file/search_files/skills_list/skill_view/terminal
    and excludes write_file/patch/skill_manage/execute_code/cronjob_manage/browser_exec."""
    from toolsets import resolve_toolset

    enabled = set(resolve_toolset("hermes-google-chat-readonly"))
    disabled_toolsets = [
        "browser", "computer_use", "cronjob", "code_execution",
        "memory", "delegation", "kanban", "image_gen", "video_gen",
    ]
    subtracted: set[str] = set()
    for ts in disabled_toolsets:
        if ts in TOOLSETS:
            subtracted |= set(resolve_toolset(ts))
    effective = enabled - subtracted
    assert {"read_file", "search_files", "skills_list", "skill_view", "terminal", "web_search"} <= effective
    assert effective.isdisjoint({"write_file", "patch", "skill_manage", "execute_code", "cronjob_manage", "memory"})


@pytest.mark.asyncio
async def test_p13_empty_or_deny_only_actions_never_synthesize_approve():
    adapter = _make_adapter()
    sent_cards: list[dict] = []

    async def fake_send_card(chat_id, card, **kwargs):
        sent_cards.append(card)
        return SendResult(success=True, message_id="m-deny")

    adapter.send_card = fake_send_card  # type: ignore[method-assign]
    deny_only_prompt = ExecApprovalPrompt(
        chat_id="spaces/DM_EMIR",
        command="long command",
        description="Command could not be previewed",
        session_key="google_chat:dm:spaces/DM_EMIR",
        request_id="req-denyonly",
        smart_denied=False,
        text="Full command could not be shown",
        actions=[],
    )
    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        res = await adapter._send_exec_approval_prompt(deny_only_prompt)

    assert res.success is True
    buttons = sent_cards[0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    choices = [
        next(p["value"] for p in b["onClick"]["action"]["parameters"] if p["key"] == "choice")
        for b in buttons
    ]
    assert choices == ["deny"]


@pytest.mark.asyncio
async def test_p14_workspace_addon_empty_invoked_function_and_full_url_action_function():
    """Real Google Workspace Add-ons extending Chat require action.function == full HTTPS URL
    and deliver empty commonEventObject.invokedFunction with action name inside parameters."""
    callback_url = "https://fleet-chat.uropenn.se/p/fleet-control/api/platforms/google_chat/events"
    adapter = _make_adapter()
    adapter._http_events_url = callback_url
    sent_cards: list[dict] = []

    async def fake_send_card(chat_id, card, **kwargs):
        sent_cards.append(card)
        return SendResult(success=True, message_id="m-addon-real")

    adapter.send_card = fake_send_card  # type: ignore[method-assign]
    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        res = await adapter._send_exec_approval_prompt(_sample_prompt())
    assert res.success is True

    buttons = sent_cards[0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    # Every button's action.function MUST be the full HTTPS callback URL
    for b in buttons:
        assert b["onClick"]["action"]["function"] == callback_url
        pdict = {p["key"]: p["value"] for p in b["onClick"]["action"]["parameters"]}
        assert pdict["action"] == "hermes_exec_approval"

    # Now simulate real Google Workspace Add-on button click with EMPTY invokedFunction!
    approval_id = next(iter(adapter._approval_state))
    real_addon_event = {
        "commonEventObject": {
            "invokedFunction": "",
            "parameters": {
                "action": "hermes_exec_approval",
                "approval_id": approval_id,
                "choice": "once",
                "request_id": "req-abc123",
            },
        },
        "chat": {
            "user": {"name": "users/109155536057359579735", "email": "emir.saffar@uropenn.se", "type": "HUMAN"},
            "buttonClickedPayload": {
                "space": {"name": "spaces/DM_EMIR", "type": "DM"},
                "message": {"name": "spaces/DM_EMIR/messages/m-addon-real"},
            },
        },
    }
    with patch("tools.approval.resolve_gateway_approval_card", return_value=1) as mock_resolve:
        resp = await adapter.dispatch_http_event(real_addon_event)
    mock_resolve.assert_called_once()
    cards = resp["hostAppDataAction"]["chatDataAction"]["updateMessageAction"]["message"]["cardsV2"]
    assert cards[0]["card"]["header"]["title"] == "Command Approved (once)"


def test_p15_http_only_mode_loads_without_google_cloud_pubsub():
    """HTTP-only callback mode must not fail with missing_deps when google.cloud.pubsub_v1 is absent."""
    import plugins.platforms.google_chat.adapter as mod

    real_import = mod.importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name in ("google.cloud.pubsub_v1", "google.api_core.exceptions"):
            raise ImportError(f"simulated missing {name}")
        return real_import(name, *args, **kwargs)

    orig_loaded = mod._google_modules_loaded
    orig_avail = mod.GOOGLE_CHAT_AVAILABLE
    orig_pubsub = mod.pubsub_v1
    orig_gax = mod.gax_exceptions
    try:
        mod._google_modules_loaded = False
        mod.GOOGLE_CHAT_AVAILABLE = False
        with patch.object(mod.importlib, "import_module", side_effect=fake_import):
            ok = mod._load_google_modules()
        assert ok is True
        assert mod.GOOGLE_CHAT_AVAILABLE is True
        assert mod.pubsub_v1 is None
    finally:
        mod._google_modules_loaded = orig_loaded
        mod.GOOGLE_CHAT_AVAILABLE = orig_avail
        mod.pubsub_v1 = orig_pubsub
        mod.gax_exceptions = orig_gax


@pytest.mark.asyncio
async def test_p16_button_only_slash_command_allowlist_blocks_mutators_and_allows_reads():
    """Two-way polarity on idle + busy slash command dispatch for require_button_platforms:
    /yolo, /approvals, /restart, /platform, /pause, /skills, /memory, /update, /approve
    all fail closed, while /status, /help, /deny, /stop pass."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.run_inbound import GatewayInboundMixin

    class _DummyRunner(GatewayInboundMixin, GatewayBusySessionMixin):
        @asynccontextmanager
        async def _async_profile_scope_for_source(self, source):
            yield

        def _gateway_plain_command_handlers(self):
            return {
                "status": AsyncMock(return_value="status-ok"),
                "deny": AsyncMock(return_value="denied-ok"),
                "yolo": AsyncMock(return_value="SHOULD_NEVER_RUN"),
                "approvals": AsyncMock(return_value="SHOULD_NEVER_RUN"),
                "restart": AsyncMock(return_value="SHOULD_NEVER_RUN"),
                "platform": AsyncMock(return_value="SHOULD_NEVER_RUN"),
                "pause": AsyncMock(return_value="SHOULD_NEVER_RUN"),
                "skills": AsyncMock(return_value="SHOULD_NEVER_RUN"),
                "memory": AsyncMock(return_value="SHOULD_NEVER_RUN"),
                "update": AsyncMock(return_value="SHOULD_NEVER_RUN"),
                "approve": AsyncMock(return_value="SHOULD_NEVER_RUN"),
            }

        def _gateway_idle_command_handlers(self):
            return {}

    runner = _DummyRunner()
    source = SessionSource(
        platform=Platform("google_chat"),
        chat_id="spaces/DM_EMIR",
        chat_type="dm",
        user_id="users/109155536057359579735",
    )
    event = MessageEvent(text="", source=source)

    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        # Negative polarity (must block every mutating/admin command on idle AND busy paths)
        for cmd in ("yolo", "approvals", "restart", "platform", "pause", "skills", "memory", "update", "approve"):
            handled, reply = await runner._hm_dispatch_canonical_command(event, source, "k", cmd)
            assert handled is True
            assert "disabled on button-only platform" in (reply or "")
            busy_reply = await runner._dispatch_busy_slash_command(
                event, SimpleNamespace(name=cmd, busy_policy="dispatch", busy_handler=None), "k", source
            )
            assert "disabled on button-only platform" in (busy_reply or "")

        # Positive polarity (read-only & fail-safe commands pass)
        for cmd, expected in (("status", "status-ok"), ("deny", "denied-ok")):
            handled, reply = await runner._hm_dispatch_canonical_command(event, source, "k", cmd)
            assert handled is True
            assert reply == expected


@pytest.mark.asyncio
async def test_p17_clarify_excluded_and_added_to_space_http_caches_bot_id():
    assert "clarify" not in set(TOOLSETS["hermes-google-chat-readonly"]["tools"])
    adapter = _make_adapter()
    adapter._bot_user_id = ""
    adapter._save_cached_bot_id = MagicMock()  # type: ignore[method-assign]
    resp = await adapter.dispatch_http_event({
        "chat": {
            "addedToSpacePayload": {
                "space": {"name": "spaces/DM_EMIR", "type": "DM"},
                "membership": {"member": {"name": "users/999999999", "type": "BOT"}},
            }
        }
    })
    assert resp == {}
    assert adapter._bot_user_id == "users/999999999"
    adapter._save_cached_bot_id.assert_called_once_with("users/999999999")


@pytest.mark.asyncio
async def test_p18_skill_and_bundle_slash_rewrites_blocked_on_button_only_platform():
    """Forged text DM '/some-skill' or '/deep-research' must fail closed BEFORE
    quick/plugin/bundle/skill slash rewrites on button-only platforms."""
    from contextlib import asynccontextmanager
    from gateway.run_inbound import GatewayInboundMixin

    class _DummyRunner(GatewayInboundMixin):
        @asynccontextmanager
        async def _async_profile_scope_for_source(self, source):
            yield

        def _route_stop_epoch(self, quick_key):
            return 0

        async def _hm_resolve_command(self, event, source, quick_key):
            return False, None, event.get_command(), None

        async def _hm_dispatch_canonical_command(self, event, source, quick_key, canonical):
            return False, None

        async def _hm_dispatch_quick_and_plugin_commands(self, event, source, command):
            raise AssertionError("quick/plugin dispatch must never run on blocked slash command")

        def _hm_skill_slash_rewrite(self, event, source, quick_key, command):
            raise AssertionError("skill/bundle rewrite must never run on blocked slash command")

    runner = _DummyRunner()
    source = SessionSource(
        platform=Platform("google_chat"),
        chat_id="spaces/DM_EMIR",
        chat_type="dm",
        user_id="users/109155536057359579735",
    )
    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        for text in ("/deep-research", "/some_skill foo", "/bundle-x"):
            handled, reply = await runner._hm_dispatch_idle_commands(
                MessageEvent(text=text, source=source), source, "k"
            )
            assert handled is True
            assert "disabled on button-only platform" in (reply or "")


@pytest.mark.asyncio
async def test_p19_setup_files_intercept_blocked_on_button_only_platform():
    """Adapter-level /setup-files OAuth shortcut must NOT fire on button-only platforms."""
    adapter = _make_adapter()
    adapter._handle_setup_files_command = AsyncMock(return_value=True)  # type: ignore[method-assign]
    adapter.handle_message = AsyncMock()  # type: ignore[method-assign]
    source = SessionSource(
        platform=Platform("google_chat"),
        chat_id="spaces/DM_EMIR",
        chat_type="dm",
        user_id="users/109155536057359579735",
        user_id_alt="emir.saffar@uropenn.se",
    )
    adapter._build_message_event = AsyncMock(  # type: ignore[method-assign]
        return_value=MessageEvent(text="/setup-files", source=source)
    )
    with patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}):
        await adapter._dispatch_message({"text": "/setup-files"}, {})
    adapter._handle_setup_files_command.assert_not_called()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_p20_addon_click_with_url_invoked_function_accepts_own_url_and_rejects_foreign():
    """If invokedFunction or actionMethodName echoes _http_events_url alongside
    params['action']=='hermes_exec_approval', accept; reject any foreign function/URL."""
    adapter = _make_adapter()
    adapter._http_events_url = "https://fleet-chat.uropenn.se/p/fleet-control/api/platforms/google_chat/events"
    adapter._approval_state["ap-url"] = {
        "session_key": "sess-url",
        "request_id": "req-url",
        "chat_id": "spaces/DM_EMIR",
        "command": "echo hi",
        "description": "test",
        "card_id": "approval-ap-url",
    }

    with (
        patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}),
        patch("tools.approval.resolve_gateway_approval_card", return_value=1) as mock_resolve,
    ):
        # Foreign URL must be rejected without popping state
        foreign = _addon_click(
            approval_id="ap-url", choice="once", request_id="req-url",
            invoked_function="https://evil.example/hook",
        )
        foreign["commonEventObject"]["parameters"]["action"] = "hermes_exec_approval"
        assert await adapter.dispatch_http_event(foreign) == {}
        assert "ap-url" in adapter._approval_state
        mock_resolve.assert_not_called()

        # Own _http_events_url + params["action"] == "hermes_exec_approval" must succeed
        own = _addon_click(
            approval_id="ap-url", choice="once", request_id="req-url",
            invoked_function=adapter._http_events_url,
        )
        own["commonEventObject"]["parameters"]["action"] = "hermes_exec_approval"
        resp = await adapter.dispatch_http_event(own)
        assert "hostAppDataAction" in resp
        mock_resolve.assert_called_once()


@pytest.mark.asyncio
async def test_p21_scope_ctx_isolation_and_container_guard_no_bypass():
    """_is_button_only honors adapter._scope_ctx even when ambient context differs,
    and container env_type cannot bypass button-only approval."""
    import contextvars

    cv: contextvars.ContextVar[bool] = contextvars.ContextVar("test_btn_scope", default=False)
    adapter = _make_adapter()

    def _capture_scope():
        cv.set(True)
        return contextvars.copy_context()

    adapter._scope_ctx = contextvars.Context().run(_capture_scope)
    with patch.object(approval_context, "is_button_only_approval_platform", side_effect=lambda _p=None: cv.get()):
        assert cv.get() is False
        assert adapter._is_button_only() is True

    # Container backend must NOT skip approval on button_only platform
    with (
        patch.object(approval_context, "_get_session_platform", return_value="google_chat"),
        patch.object(approval_context, "_get_approval_config", return_value={"require_button_platforms": ["google_chat"]}),
        patch.object(approval_mod, "_yolo_active", return_value=True),
        patch.object(approval_mod, "_presence", return_value=(None, False, False, False)),
    ):
        res = approval_mod.check_all_command_guards("ls -la", env_type="docker", has_host_access=False)
        assert res["approved"] is False
