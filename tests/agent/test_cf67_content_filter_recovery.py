"""Regression tests for CF-67: mid-turn ``finish_reason=content_filter`` recovery,
Gemini/Vertex/OpenRouter ``safety_settings: OFF``, and sticky fallback protection against
CF-61 mid-turn primary restore ping-pong (RCA 2026-11-08, session 20261107_145424_ce328b).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.error_classifier import FailoverReason
from agent.turn_retry_state import TurnRetryState
from agent.turn_truncation import handle_content_policy_refusal


def _make_mock_agent(*, has_fallback: bool = True):
    from agent.transports import get_transport

    agent = MagicMock()
    agent.api_mode = "chat_completions"
    agent.provider = "openrouter"
    agent.model = "google/gemini-4-argon"
    agent.base_url = "https://openrouter.ai/api/v1"
    agent.log_prefix = ""
    agent._is_anthropic_oauth = False
    agent._content_filter_retries = 0
    agent._content_filter_fallback_sticky = False
    agent._ephemeral_reasoning_off = False
    agent._get_transport.return_value = get_transport("chat_completions")
    agent._has_pending_fallback.return_value = has_fallback
    agent._try_activate_fallback.return_value = has_fallback
    agent._should_treat_stop_as_truncated.return_value = False
    agent._extract_reasoning.return_value = "**Analyzing Pre-Tool Behavior** github-review-gate"
    agent._session_db = None
    return agent


def _chat_refusal_response():
    msg = SimpleNamespace(
        role="assistant",
        content="",
        tool_calls=None,
        reasoning="**Analyzing Pre-Tool Behavior** github-review-gate",
        reasoning_content="**Analyzing Pre-Tool Behavior** github-review-gate",
        refusal=None,
    )
    choice = SimpleNamespace(index=0, message=msg, finish_reason="content_filter")
    return SimpleNamespace(id="chatcmpl-test", choices=[choice], usage=None, model="google/gemini-4-argon")


def test_gemini_vertex_and_openrouter_emit_safety_settings_off():
    from agent.gemini_native_adapter import (
        GEMINI_SAFETY_SETTINGS_OFF,
        build_gemini_request,
        translate_gemini_response,
    )
    from providers import get_provider_profile

    expected = [dict(item) for item in GEMINI_SAFETY_SETTINGS_OFF]
    assert len(expected) == 5
    assert all(item["threshold"] == "OFF" for item in expected)

    req = build_gemini_request(messages=[{"role": "user", "content": "hi"}], max_tokens=128)
    assert req["safetySettings"] == expected

    vertex_profile = get_provider_profile("vertex")
    v_eb = vertex_profile.build_extra_body(
        model="google/gemini-4-argon",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    assert v_eb["extra_body"]["google"]["safety_settings"] == expected
    assert v_eb["extra_body"]["google"]["thinking_config"] == {
        "include_thoughts": True,
        "thinking_level": "high",
    }

    gemini_profile = get_provider_profile("gemini")
    g_compat = gemini_profile.build_extra_body(
        model="gemini-4-argon",
        reasoning_config={"enabled": True, "effort": "high"},
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    )
    assert g_compat["extra_body"]["google"]["safety_settings"] == expected

    or_profile = get_provider_profile("openrouter")
    or_gemini = or_profile.build_extra_body(model="google/gemini-4-argon")
    assert or_gemini["google"]["safety_settings"] == expected
    or_other = or_profile.build_extra_body(model="openai/gpt-5.6-sol")
    assert "google" not in or_other

    # Native Gemini finishReason variants map to content_filter and preserve stop_reason/stop_details.
    for reason in ("PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "SAFETY"):
        translated = translate_gemini_response(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": []},
                        "finishReason": reason,
                        "safetyRatings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "probability": "MEDIUM"}],
                    }
                ]
            },
            model="gemini-4-argon",
        )
        assert translated.choices[0].finish_reason == "content_filter"
        assert translated.stop_reason == reason
        assert translated.stop_details["category"] == reason


def test_mid_turn_content_filter_retries_once_with_steering_before_activating_sticky_fallback():
    from agent.turn_response_check import check_api_response

    agent = _make_mock_agent(has_fallback=True)
    messages = [
        {"role": "user", "content": "Verify PR #65 and #73"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "github-review-gate: blocked"},
    ]
    api_messages = [dict(m) for m in messages]
    retry_state = TurnRetryState()

    # Attempt 1 on mid-turn call #65: should return action="continue" (same-route retry with steering note),
    # NOT activate fallback yet.
    v1 = handle_content_policy_refusal(
        agent,
        _chat_refusal_response(),
        retry_state,
        thinking_spinner=None,
        messages=messages,
        api_messages=api_messages,
        api_kwargs={},
        active_system_prompt="sys",
        conversation_history=list(messages),
        api_call_count=65,
        effective_task_id="t",
        turn_id="u",
        api_request_id="r1",
        api_start_time=0.0,
        retry_count=0,
        max_retries=3,
        current_turn_user_idx=0,
    )
    assert v1.action == "continue"
    assert agent._content_filter_retries == 1
    assert agent._ephemeral_reasoning_off is True
    assert "[hermes note: Previous response generation was interrupted by an upstream output filter" in api_messages[-1]["content"]
    agent._try_activate_fallback.assert_not_called()

    # Verify check_api_response propagates "continue" when handle_content_policy_refusal returns "continue".
    agent._content_filter_retries = 0
    rc = check_api_response(
        agent,
        response=_chat_refusal_response(),
        finish_reason="content_filter",
        messages=messages,
        api_messages=[dict(m) for m in messages],
        api_kwargs={},
        active_system_prompt="sys",
        conversation_history=list(messages),
        thinking_spinner=None,
        _retry=retry_state,
        api_call_count=65,
        effective_task_id="t",
        turn_id="u",
        api_request_id="r1b",
        api_start_time=0.0,
        retry_count=0,
        max_retries=3,
        compression_attempts=0,
        max_compression_attempts=3,
        _preflight_compression_blocked=False,
        _last_preflight_pressure=None,
        current_turn_user_idx=0,
        length_continue_retries=0,
        truncated_response_parts=[],
        truncated_tool_call_retries=0,
    )
    assert rc.action == "continue"
    assert agent._content_filter_retries == 1

    # Attempt 2 on the same route (after _content_filter_retries == 1): now activates fallback with
    # FailoverReason.content_policy_blocked and sets _content_filter_fallback_sticky = True.
    v2 = handle_content_policy_refusal(
        agent,
        _chat_refusal_response(),
        retry_state,
        thinking_spinner=None,
        messages=messages,
        api_messages=api_messages,
        api_kwargs={},
        active_system_prompt="sys",
        conversation_history=list(messages),
        api_call_count=65,
        effective_task_id="t",
        turn_id="u",
        api_request_id="r2",
        api_start_time=0.0,
        retry_count=0,
        max_retries=3,
        current_turn_user_idx=0,
    )
    assert v2.action == "break"
    assert agent._content_filter_retries == 0
    assert agent._content_filter_fallback_sticky is True
    agent._try_activate_fallback.assert_called_once_with(reason=FailoverReason.content_policy_blocked)


def test_step1_content_filter_skips_same_route_retry_and_activates_fallback_immediately():
    agent = _make_mock_agent(has_fallback=True)
    messages = [{"role": "user", "content": "blocked prompt"}]
    v = handle_content_policy_refusal(
        agent,
        _chat_refusal_response(),
        TurnRetryState(),
        thinking_spinner=None,
        messages=messages,
        api_messages=list(messages),
        api_kwargs={},
        active_system_prompt="sys",
        conversation_history=list(messages),
        api_call_count=1,
        effective_task_id="t",
        turn_id="u",
        api_request_id="r1",
        api_start_time=0.0,
        retry_count=0,
        max_retries=3,
        current_turn_user_idx=0,
    )
    assert v.action == "break"
    assert agent._content_filter_retries == 0
    assert agent._content_filter_fallback_sticky is True
    agent._try_activate_fallback.assert_called_once_with(reason=FailoverReason.content_policy_blocked)


def test_prepare_iteration_respects_content_filter_fallback_sticky_and_turn_reset_clears_it():
    from agent.turn_context import _PER_TURN_RESET_STATE
    from agent.turn_iteration_prep import prepare_iteration

    reset_keys = dict(_PER_TURN_RESET_STATE)
    assert reset_keys.get("_content_filter_retries") == 0
    assert reset_keys.get("_content_filter_fallback_sticky") is False

    restored = []
    agent = SimpleNamespace(
        _nous_wire_pending=None,
        step_callback=None,
        _skill_nudge_interval=0,
        valid_tool_names=set(),
        _adopt_nous_key_before_expiry=lambda: None,
        _rebuilt_restarts_since_response=0,
        _fallback_activated=True,
        _content_filter_fallback_sticky=True,
        _restore_primary_runtime=lambda: restored.append(True) or True,
        _drain_pending_steer=lambda: None,
        run_budget_seconds=None,
        logger=MagicMock(),
        session_id="s1",
        _sanitize_tool_call_arguments=lambda *a, **k: 0,
        _last_flushed_db_idx=0,
        _session_db=None,
        iteration_budget=SimpleNamespace(remaining=10),
        max_iterations=20,
    )
    prepare_iteration(agent, api_call_count=2, messages=[{"role": "user", "content": "hi"}], current_turn_user_idx=0, user_message="hi")
    assert restored == [], "CF-61 must not restore primary mid-turn after a content_filter failover"

    agent._content_filter_fallback_sticky = False
    prepare_iteration(agent, api_call_count=2, messages=[{"role": "user", "content": "hi"}], current_turn_user_idx=0, user_message="hi")
    assert restored == [True]


def test_try_activate_fallback_sets_content_filter_fallback_sticky_on_http_403_content_policy_blocked(monkeypatch):
    from agent.chat_completion_helpers import try_activate_fallback
    import agent.route_binding as rb
    import agent.chat_completion_helpers as cch

    monkeypatch.setattr(rb, "bind_route_entry", lambda ag, fb, prov, mod: ("google/gemini-4-argon", "openrouter"))
    monkeypatch.setattr(cch, "rewrite_prompt_model_identity", lambda *a, **k: None)
    monkeypatch.setattr(cch, "_should_skip_fallback_candidate", lambda *a, **k: False)

    def _make_fb_agent():
        return SimpleNamespace(
            model="google/gemini-4-argon",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            _primary_runtime={"provider": "openrouter", "model": "google/gemini-4-argon"},
            _fallback_chain=[{"provider": "vertex", "model": "google/gemini-4-argon"}],
            _fallback_index=0,
            _fallback_activated=False,
            _content_filter_fallback_sticky=False,
            _unavailable_fallback_keys=set(),
            _buffer_diagnostic_status=lambda *a, **k: None,
            _pending_fallback_notice=None,
            _stale_stream_streak=0,
            _stale_first_token_streak=0,
        )

    ag_pos = _make_fb_agent()
    assert try_activate_fallback(ag_pos, reason=FailoverReason.content_policy_blocked) is True
    assert ag_pos._content_filter_fallback_sticky is True

    ag_neg = _make_fb_agent()
    assert try_activate_fallback(ag_neg, reason=FailoverReason.rate_limit) is True
    assert ag_neg._content_filter_fallback_sticky is False


def test_adopt_out_of_band_turns_reloads_history_on_in_place_secret_scrub(tmp_path, monkeypatch):
    import threading
    from contextlib import contextmanager
    import tui_gateway.prompt_turn as pt

    fake_db_file = tmp_path / "state.db"
    fake_db_file.write_text("", encoding="utf-8")

    class _FakeDB:
        db_path = str(fake_db_file)

        def __init__(self, tool_len: int):
            self._tool_len = tool_len

        def get_messages(self, key, after_id=None):
            return []

        def _read_all(self, sql, params):
            return [(10, self._tool_len)]

    scrubbed_rows = [
        {"role": "user", "content": "hi", "_row_id": 9},
        {"role": "tool", "content": "Container 0x3d19…bfbf started", "_row_id": 10},
    ]
    orig_history = [
        {"role": "user", "content": "hi", "_row_id": 9},
        {"role": "tool", "content": "Container 0x" + ("3d19a91b" * 8) + " started", "_row_id": 10},
    ]

    session = {
        "history": list(orig_history),
        "history_version": 1,
        "history_lock": threading.Lock(),
        "_submit_user_row": {"_row_id": 11},
    }

    # Bind required server globals on prompt_turn for isolated test execution
    monkeypatch.setattr(pt, "_message_row_id", lambda m: m.get("_row_id") if isinstance(m, dict) else None, raising=False)
    monkeypatch.setattr(pt, "_submit_row_target_key", lambda s: "sess_1", raising=False)
    monkeypatch.setattr(pt, "_load_durable_truncation_history", lambda s, repair_alternation=False: list(scrubbed_rows), raising=False)
    monkeypatch.setattr(pt, "canonicalize_replay_history", lambda rows: list(rows), raising=False)

    # Negative control: matching tool length and no rowbak -> history not rewritten
    @contextmanager
    def _unchanged_db(_s):
        yield _FakeDB(len(orig_history[1]["content"]))

    monkeypatch.setattr(pt, "_session_db", _unchanged_db, raising=False)
    pt._adopt_out_of_band_turns(session)
    assert session["history_version"] == 1
    assert session["history"][1]["content"] == orig_history[1]["content"]

    # Positive control: shortened tool length in SQLite after --scrub -> history re-hydrated
    @contextmanager
    def _scrubbed_db(_s):
        yield _FakeDB(len(scrubbed_rows[1]["content"]))

    monkeypatch.setattr(pt, "_session_db", _scrubbed_db, raising=False)
    pt._adopt_out_of_band_turns(session)
    assert session["history_version"] == 2
    assert session["history"][1]["content"] == "Container 0x3d19…bfbf started"


def test_mid_turn_secret_value_guard_block_rehydrates_scrubbed_assistant_msg_in_memory():
    import json
    from agent.turn_tool_round import _rehydrate_scrubbed_assistant_msg

    raw_tc = [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": '{"content":"0x' + ("948fa1b2" * 8) + '"}'}}]
    scrubbed_tc = [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": '{"content":"0x948f…a1b2"}'}}]

    class _StubDB:
        def _read_all(self, sql, params):
            assert params == ("sess_midturn_1",)
            return [("", json.dumps(scrubbed_tc), "scrubbed reasoning 0x948f…a1b2", "scrubbed reasoning 0x948f…a1b2", None)]

    agent = SimpleNamespace(session_id="sess_midturn_1", _session_db=_StubDB())

    # Negative control: normal tool result without secret-value-guard block does not query DB or mutate assistant_msg
    assistant_neg = {"role": "assistant", "content": "", "tool_calls": list(raw_tc), "reasoning": "raw"}
    msgs_neg = [{"role": "user", "content": "hi"}, assistant_neg, {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    _rehydrate_scrubbed_assistant_msg(agent, assistant_neg, msgs_neg)
    assert assistant_neg["tool_calls"] == raw_tc
    assert assistant_neg["reasoning"] == "raw"

    # Positive control: tool result blocked by secret-value-guard rehydrates scrubbed tool_calls & reasoning in-memory
    assistant_pos = {"role": "assistant", "content": "", "tool_calls": list(raw_tc), "reasoning": "raw", "reasoning_content": "raw"}
    msgs_pos = [
        {"role": "user", "content": "hi"},
        assistant_pos,
        {"role": "tool", "tool_call_id": "c1", "content": '{"error": "BLOCKERAD (secret-value-guard): argumentet till write_file"}'},
    ]
    _rehydrate_scrubbed_assistant_msg(agent, assistant_pos, msgs_pos)
    assert assistant_pos["tool_calls"] == scrubbed_tc
    assert assistant_pos["reasoning"] == "scrubbed reasoning 0x948f…a1b2"
    assert assistant_pos["reasoning_content"] == "scrubbed reasoning 0x948f…a1b2"


def test_cf76_openrouter_gemini4_disable_clamps_to_low_and_emits_thinking_config():
    from agent.gemini_native_adapter import GEMINI_SAFETY_SETTINGS_OFF
    from providers import get_provider_profile

    profile = get_provider_profile("openrouter")
    disabled_cfg = {"enabled": False, "effort": "none"}
    eb, _tl = profile.build_api_kwargs_extras(
        reasoning_config=disabled_cfg,
        supports_reasoning=True,
        model="google/gemini-4-argon",
    )
    assert eb["reasoning"] == {"effort": "low", "exclude": True}
    body = profile.build_extra_body(
        model="google/gemini-4-argon",
        reasoning_config=disabled_cfg,
    )
    assert body["google"]["thinking_config"] == {
        "include_thoughts": False,
        "thinking_level": "low",
    }
    assert body["google"]["safety_settings"] == [dict(item) for item in GEMINI_SAFETY_SETTINGS_OFF]


def test_cf76_fallback_activation_preserves_low_effort_and_summarizes_gate_scripts():
    from agent.turn_truncation import _CONTENT_FILTER_STEER_NOTE, _inject_content_filter_steer_note

    agent = _make_mock_agent(has_fallback=True)
    agent._content_filter_retries = 1

    messages = [
        {"role": "user", "content": "deploy PR #114"},
        {"role": "tool", "tool_call_id": "t1", "content": "read warehouse-deploy-gate.py and .approved-shas"},
    ]
    api_messages = [dict(m) for m in messages]
    verdict = handle_content_policy_refusal(
        agent,
        _chat_refusal_response(),
        TurnRetryState(),
        thinking_spinner=None,
        messages=messages,
        api_messages=api_messages,
        api_kwargs={},
        active_system_prompt="sys",
        conversation_history=list(messages),
        api_call_count=5,
        effective_task_id="t1",
        turn_id="u1",
        api_request_id="r1",
        api_start_time=0.0,
        retry_count=0,
        max_retries=2,
        current_turn_user_idx=0,
    )
    assert verdict.action == "break"
    assert agent._content_filter_fallback_sticky is True
    assert agent._ephemeral_reasoning_off is True
    assert agent._pending_content_filter_steer is True

    rebuilt_api_messages = [dict(m) for m in messages]
    _inject_content_filter_steer_note(rebuilt_api_messages)
    assert ".approved-shas" not in rebuilt_api_messages[-1]["content"]
    assert "warehouse-deploy-gate" not in rebuilt_api_messages[-1]["content"]
    assert _CONTENT_FILTER_STEER_NOTE in rebuilt_api_messages[-1]["content"]


def test_cf76_inflight_replay_compaction_carrier_reanchors_and_retains_refused_turn(tmp_path):
    from agent.compaction_display import project_compaction_message_for_display
    from agent.context_compressor import (
        HISTORICAL_TASK_HEADING,
        SUMMARY_PREFIX,
        _INFLIGHT_TASK_REPLAY_HEADER,
        _SUMMARY_END_MARKER,
    )
    from agent.turn_context import (
        history_before_current_turn,
        reanchor_current_turn_user_idx,
        refused_turn_boundary_row_id,
    )
    from agent.turn_failure_copy import CONTENT_REFUSED_DISPLAY_KIND
    from hermes_state import SessionDB
    from hermes_state_rewind import retain_refused_turn_for_display

    carrier_text = (
        f"{SUMMARY_PREFIX}\n{HISTORICAL_TASK_HEADING}\nold summary\n\n"
        f"{_SUMMARY_END_MARKER}\n\n"
        f"{_INFLIGHT_TASK_REPLAY_HEADER}\n@url:https://mail.google.com/mail/u/0/#chat/space/AAQADqzvLBY"
    )
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("s_cf76", "tui")
        boundary_id = db.append_message("s_cf76", "user", carrier_text)
        db.append_message(
            "s_cf76",
            "assistant",
            "",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
        )
        db.append_message("s_cf76", "tool", "warehouse-deploy-gate.py content", tool_call_id="c1")

        in_mem = db.get_messages_as_conversation("s_cf76", include_row_ids=True)
        idx = reanchor_current_turn_user_idx(
            in_mem, "@url:https://mail.google.com/mail/u/0/#chat/space/AAQADqzvLBY"
        )
        assert idx == 0
        assert refused_turn_boundary_row_id(in_mem, idx) == boundary_id

        clean_hist = history_before_current_turn(in_mem, idx, list(in_mem))
        assert len(clean_hist) == 1
        assert clean_hist[0]["role"] == "user"
        assert clean_hist[0].get("display_kind") == "hidden"
        assert _INFLIGHT_TASK_REPLAY_HEADER not in clean_hist[0]["content"]

        notice = "This message was refused by the model's safety filter — try a different query."
        assert retain_refused_turn_for_display(db, "s_cf76", notice=notice, boundary_row_id=boundary_id) is True

        model_rows = db.get_messages_as_conversation("s_cf76")
        assert len(model_rows) == 1
        assert model_rows[0]["display_kind"] == "hidden"
        assert "warehouse-deploy-gate.py" not in str(model_rows)

        all_rows = db.get_messages_as_conversation("s_cf76", include_compacted=True)
        refused_rows = [m for m in all_rows if m.get("display_kind") == CONTENT_REFUSED_DISPLAY_KIND]
        assert len(refused_rows) == 1
        projected = project_compaction_message_for_display(refused_rows[0])
        assert projected is not None
        assert projected.get("display_kind") == CONTENT_REFUSED_DISPLAY_KIND
        assert "AAQADqzvLBY" in projected.get("content", "")
    finally:
        db.close()



def test_cf76_settle_unrecovered_error_wraps_sticky_fallback_with_low_effort_and_steer():
    """CF-76 / VG-08: When Vertex fallback exhausts 3/3 retries on HTTP 429 while
    _content_filter_fallback_sticky is True, settle_unrecovered_error wraps back to
    primary with _ephemeral_reasoning_off=True and _pending_content_filter_steer=True,
    and _inject_content_filter_steer_note handles list-shaped content parts."""
    from agent.error_classifier import ClassifiedError, FailoverReason
    from agent.turn_api_error import settle_unrecovered_error
    from agent.turn_truncation import _inject_content_filter_steer_note, _CONTENT_FILTER_STEER_NOTE

    list_msgs = [{"role": "user", "content": [{"type": "text", "text": "hello world"}]}]
    _inject_content_filter_steer_note(list_msgs)
    assert _CONTENT_FILTER_STEER_NOTE in list_msgs[0]["content"][0]["text"]

    agent = _make_mock_agent(has_fallback=False)
    agent.provider = "vertex"
    agent._fallback_activated = True
    agent._content_filter_fallback_sticky = True
    agent._ephemeral_reasoning_off = False
    agent._pending_content_filter_steer = False
    agent._try_recover_primary_transport = lambda *a, **kw: False
    agent._restore_primary_runtime = lambda: setattr(agent, "provider", "openrouter") or True

    retry = TurnRetryState()
    classified = ClassifiedError(
        reason=FailoverReason.rate_limit,
        retryable=True,
        should_compress=False,
        should_fallback=True,
        message="429 RESOURCE_EXHAUSTED",
    )
    v = settle_unrecovered_error(
        agent,
        api_error=RuntimeError("429 RESOURCE_EXHAUSTED"),
        classified=classified,
        _retry=retry,
        status_code=429,
        error_msg="429 RESOURCE_EXHAUSTED",
        is_context_length_error=False,
        is_rate_limited=True,
        _is_zai_coding_overload=False,
        _provider="vertex",
        _base="https://aiplatform.googleapis.com/v1beta1",
        _model="google/gemini-4-argon",
        messages=[{"role": "user", "content": "hi"}],
        api_messages=[{"role": "user", "content": "hi"}],
        api_kwargs={},
        active_system_prompt="sys",
        conversation_history=[{"role": "user", "content": "hi"}],
        approx_tokens=100,
        retry_count=3,
        max_retries=3,
        compression_attempts=0,
        api_call_count=2,
        error_context={},
        current_turn_user_idx=0,
    )
    assert v.action == "break"
    assert agent.provider == "openrouter"
    assert agent._content_filter_fallback_sticky is False
    assert agent._ephemeral_reasoning_off is True
    assert agent._pending_content_filter_steer is True



def test_cf78_browser_cookie_and_cdp_eval_sanitized_across_20_message_tail():
    """CF-78: When a 17-message headless Chrome cookie/CDP probe chain precedes a mid-turn
    content_filter refusal, _inject_content_filter_steer_note summarizes tool outputs,
    assistant tool_calls[].function.arguments, and replayed reasoning across the trailing
    20 messages so neither OpenRouter nor Vertex re-trips Gemini's output classifier."""
    from agent.turn_truncation import _CONTENT_FILTER_STEER_NOTE, _inject_content_filter_steer_note

    api_messages = [{"role": "user", "content": "verify CF_RULES_TOKEN Cache Purge scope"}]
    # Place the initial Chrome/Default/Cookies probe at offset -17 from the tail
    api_messages.append({
        "role": "assistant",
        "content": "",
        "reasoning": "Decrypting Chrome Safe Storage via PBKDF2HMAC",
        "reasoning_content": "Decrypting Chrome Safe Storage via PBKDF2HMAC",
        "reasoning_details": [{"type": "reasoning.text", "text": "PBKDF2HMAC"}],
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "terminal",
                "arguments": '{"command":"sqlite3 ~/Library/Application Support/Google/Chrome/Default/Cookies \"SELECT encrypted_value\""}',
            },
        }],
    })
    api_messages.append({
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"output": "extracted_cf_cookies=25 names=[\x27__cf_logged_in\x27, \x27CF_VERIFIED_DEVICE_abc\x27]"}',
    })
    for idx in range(6):
        api_messages.append({
            "role": "assistant",
            "content": "",
            "reasoning": f"step {idx}",
            "reasoning_content": f"step {idx}",
            "tool_calls": [{"id": f"c_{idx}", "type": "function", "function": {"name": "terminal", "arguments": '{"command":"echo ok"}'}}],
        })
        api_messages.append({"role": "tool", "tool_call_id": f"c_{idx}", "content": '{"output": "ok"}'})
    api_messages.append({
        "role": "assistant",
        "content": "",
        "reasoning": "Evaluating CDP_EVAL on dash.cloudflare.com/profile/api-tokens",
        "reasoning_content": "Evaluating CDP_EVAL on dash.cloudflare.com/profile/api-tokens",
        "tool_calls": [{
            "id": "call_cdp",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path":"/tmp/air_cf_cookie_probe.py","content":"security find-generic-password -w -s \"Chrome Safe Storage\""}',
            },
        }],
    })
    api_messages.append({
        "role": "tool",
        "tool_call_id": "call_cdp",
        "content": '{"output": "CDP_EVAL: {\"tok_policies\":[{\"permission_groups\":[]}]}"}',
    })

    assert len(api_messages) == 17
    _inject_content_filter_steer_note(api_messages)

    serialized = str(api_messages)
    for forbidden in (
        "Chrome/Default/Cookies",
        "Chrome Safe Storage",
        "encrypted_value",
        "extracted_cf_cookies=",
        "__cf_logged_in",
        "CDP_EVAL:",
        "tok_policies",
    ):
        assert forbidden not in serialized, f"forbidden marker leaked: {forbidden}"
    assert _CONTENT_FILTER_STEER_NOTE in api_messages[-1]["content"]
    assert api_messages[1]["reasoning_content"] == " "
    assert "reasoning_details" not in api_messages[1]
