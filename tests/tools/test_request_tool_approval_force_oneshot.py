"""Tests for pre_tool_call action='approve' force_prompt, one_shot, and args merge."""
from __future__ import annotations

from agent.shell_hooks import _parse_pre_tool_call
from hermes_cli import plugins
from tools import approval, approval_context


class TestShellHookParseApproveExtensions:
    def test_parses_force_prompt_one_shot_and_args(self):
        parsed = _parse_pre_tool_call({
            "action": "approve",
            "message": "Send email to a@b.se",
            "rule_key": "outbound_email:abc123",
            "force_prompt": True,
            "one_shot": True,
            "args": {"expected_v2_hash": "deadbeef"},
        })
        assert parsed == {
            "action": "approve",
            "message": "Send email to a@b.se",
            "rule_key": "outbound_email:abc123",
            "force_prompt": True,
            "one_shot": True,
            "args": {"expected_v2_hash": "deadbeef"},
        }

    def test_ignores_non_bool_flags_and_empty_args(self):
        parsed = _parse_pre_tool_call({
            "action": "approve",
            "message": "ok",
            "force_prompt": "yes",
            "one_shot": 1,
            "args": {},
        })
        assert parsed == {"action": "approve", "message": "ok"}


class TestPluginsDispatchApproveExtensions:
    def test_merges_args_and_forwards_force_prompt_one_shot(self, monkeypatch):
        seen = {}

        def fake_invoke(*a, **kw):
            return [{
                "action": "approve",
                "message": "Confirm send",
                "rule_key": "outbound_email:112233",
                "force_prompt": True,
                "one_shot": True,
                "args": {"expected_v2_hash": "abc999"},
            }]

        def fake_request(tool_name, reason, **kw):
            seen["tool_name"] = tool_name
            seen["reason"] = reason
            seen.update(kw)
            return {"approved": True}

        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", fake_invoke)
        monkeypatch.setattr("tools.approval.request_tool_approval", fake_request)

        block_msg, modified = plugins._dispatch_pre_tool_call_hooks(
            "mcp__gmail_selfhosted__send_draft", {"draft_id": "r-123"},
        )
        assert block_msg is None
        assert modified == {"draft_id": "r-123", "expected_v2_hash": "abc999"}
        assert seen == {
            "tool_name": "mcp__gmail_selfhosted__send_draft",
            "reason": "Confirm send",
            "rule_key": "outbound_email:112233",
            "force_prompt": True,
            "one_shot": True,
        }

    def test_later_hook_can_only_strengthen_flags_and_block_still_wins(self, monkeypatch):
        def fake_invoke(*a, **kw):
            return [
                {"action": "approve", "message": "soft", "force_prompt": False},
                {"action": "approve", "message": "hard", "force_prompt": True, "one_shot": True},
            ]

        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", fake_invoke)
        details = plugins._get_pre_tool_call_directive_details("send_draft", {"draft_id": "1"})
        assert details.action == "approve"
        assert details.force_prompt is True
        assert details.one_shot is True

        def fake_invoke_block(*a, **kw):
            return [
                {"action": "approve", "message": "hard", "force_prompt": True, "one_shot": True},
                {"action": "block", "message": "hard veto"},
            ]

        monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", fake_invoke_block)
        details2 = plugins._get_pre_tool_call_directive_details("send_draft", {"draft_id": "1"})
        assert details2.action == "block"
        assert details2.message == "hard veto"


class TestRequestToolApprovalForcePromptOneShot:
    def test_force_prompt_ignores_mode_off_and_yolo_and_fails_closed_unattended(self, monkeypatch):
        monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "off")
        monkeypatch.setattr(approval, "_yolo_active", lambda: True)
        monkeypatch.setattr(approval, "_presence", lambda cb=None: (None, False, False, False))

        # Baseline (force_prompt=False) still auto-approves under mode=off
        baseline = approval.request_tool_approval("some_tool", "normal rule", rule_key="normal")
        assert baseline["approved"] is True

        # force_prompt=True ignores mode=off + yolo and fails closed when no human present
        forced = approval.request_tool_approval(
            "mcp__gmail_selfhosted__send_draft",
            "Send email",
            rule_key="outbound_email:test1",
            force_prompt=True,
            one_shot=True,
        )
        assert forced["approved"] is False
        assert "BLOCKED" in forced["message"]

    def test_force_prompt_and_one_shot_gateway_card_and_no_persistence(self, monkeypatch):
        monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "off")
        monkeypatch.setattr(approval, "_yolo_active", lambda: True)
        monkeypatch.setattr(approval, "_presence", lambda cb=None: (None, False, True, True))
        monkeypatch.setattr(approval, "get_current_session_key", lambda: "sess-force-test")
        monkeypatch.setattr(approval, "_gateway_notify_cb", lambda sk: (lambda d: None))

        # Pre-seed session approval to prove force_prompt + one_shot ignores cache
        approval.approve_session("sess-force-test", "plugin_rule:outbound_email:samekey")

        seen_payloads = []
        seen_identities = []

        def fake_await(sk, cb, data, *, surface, identity):
            seen_payloads.append(dict(data))
            seen_identities.append(identity)
            # Even if a rogue client answered "always", one_shot must NOT persist
            return {"resolved": True, "choice": "always", "reason": None, "cancelled": None}

        persist_calls = []
        monkeypatch.setattr(approval, "_await_gateway_decision", fake_await)
        monkeypatch.setattr(approval, "_persist_choice", lambda *a, **kw: persist_calls.append((a, kw)))

        res1 = approval.request_tool_approval(
            "mcp__gmail_selfhosted__send_draft",
            "[INTERNAL] Send to team",
            rule_key="outbound_email:samekey",
            force_prompt=True,
            one_shot=True,
        )
        res2 = approval.request_tool_approval(
            "mcp__gmail_selfhosted__send_draft",
            "[INTERNAL] Send to team",
            rule_key="outbound_email:samekey",
            force_prompt=True,
            one_shot=True,
        )
        assert res1["approved"] is True
        assert res2["approved"] is True
        assert len(seen_payloads) == 2
        assert seen_payloads[0]["allow_permanent"] is False
        assert seen_payloads[0]["allow_session"] is False
        assert seen_payloads[0]["description"] == "[INTERNAL] Send to team"
        assert persist_calls == []
        # Unique one-shot identities prevent concurrent send coalescing
        assert seen_identities[0] != seen_identities[1]
        assert "#oneshot:" in seen_identities[0]

    def test_force_prompt_deny_blocks(self, monkeypatch):
        monkeypatch.setattr(approval_context, "_get_approval_mode", lambda: "off")
        monkeypatch.setattr(approval, "_presence", lambda cb=None: (None, False, True, True))
        monkeypatch.setattr(approval, "get_current_session_key", lambda: "sess-deny-test")
        monkeypatch.setattr(approval, "_gateway_notify_cb", lambda sk: (lambda d: None))
        monkeypatch.setattr(
            approval,
            "_await_gateway_decision",
            lambda *a, **kw: {"resolved": True, "choice": "deny", "reason": "wrong recipient", "cancelled": None},
        )
        res = approval.request_tool_approval(
            "mcp__gmail_selfhosted__send_draft",
            "[EXTERNAL SEND WARNING] Send to vendor@ext.com",
            rule_key="outbound_email:deny1",
            force_prompt=True,
            one_shot=True,
        )
        assert res["approved"] is False
        assert "wrong recipient" in res["message"]
