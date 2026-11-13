"""Tests for StreamingThinkScrubber.

These tests lock in the contract the scrubber must satisfy so downstream
consumers (ACP, api_server, TTS, CLI, gateway) never see reasoning
blocks leaking through the stream_delta_callback.  The scenarios map
directly to the MiniMax-M2.7 / DeepSeek / Qwen3 streaming patterns that
break the older per-delta regex strip.
"""

from __future__ import annotations

import pytest

from agent.think_scrubber import StreamingThinkScrubber


def _drive(scrubber: StreamingThinkScrubber, deltas: list[str]) -> str:
    """Feed a sequence of deltas and return the concatenated visible output."""
    out = [scrubber.feed(d) for d in deltas]
    out.append(scrubber.flush())
    return "".join(out)


class TestClosedPairs:
    """Closed <tag>...</tag> pairs are always stripped, regardless of boundary."""

    def test_closed_pair_single_delta(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["<think>reasoning</think>Hello world"]) == "Hello world"


    @pytest.mark.parametrize(
        "tag",
        ["think", "thinking", "reasoning", "thought", "REASONING_SCRATCHPAD"],
    )
    def test_all_tag_variants(self, tag: str) -> None:
        s = StreamingThinkScrubber()
        delta = f"<{tag}>x</{tag}>Hello"
        assert _drive(s, [delta]) == "Hello"



class TestUnterminatedOpen:
    """Unterminated open tag discards all subsequent content to end of stream."""

    def test_open_at_stream_start(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["<think>reasoning text with no close"]) == ""



    def test_prose_mentioning_tag_not_stripped(self) -> None:
        """Mid-line '<think>' in prose is preserved (no boundary)."""
        s = StreamingThinkScrubber()
        text = "Use the <think> element for reasoning"
        assert _drive(s, [text]) == text


class TestOrphanClose:
    """Orphan close tags (no prior open) are stripped without boundary check."""

    def test_orphan_close_alone(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["Hello</think>world"]) == "Helloworld"




class TestPartialTagsAcrossDeltas:
    """Partial tags at delta boundaries must be held back, not emitted raw."""

    def test_split_open_tag_held_back(self) -> None:
        """'<' arrives alone, 'think>' completes it on next delta."""
        s = StreamingThinkScrubber()
        # At stream start, last_emitted_ended_newline=True, so <think> at 0 is boundary
        assert (
            _drive(s, ["<", "think>reasoning</think>done"])
            == "done"
        )

    def test_split_open_tag_not_at_boundary(self) -> None:
        """Mid-line split '<' + 'think>X</think>' is a closed pair.

        Closed pairs are always stripped (matching
        ``_strip_think_blocks`` case 1), even without a block
        boundary — a closed pair is an intentional bounded construct.
        """
        s = StreamingThinkScrubber()
        out = _drive(s, ["word<", "think>prose</think>more"])
        assert out == "wordmore"




class TestTheMiniMaxScenario:
    """The exact pattern run_agent per-delta regex strip breaks."""

    def test_minimax_split_open(self) -> None:
        """delta1='<think>', delta2='Let me check', delta3='</think>done'."""
        s = StreamingThinkScrubber()
        out = _drive(s, ["<think>", "Let me check their config", "</think>", "done"])
        assert out == "done"


    def test_minimax_unterminated_reasoning_at_end(self) -> None:
        """Unclosed reasoning at stream end is dropped entirely."""
        s = StreamingThinkScrubber()
        out = _drive(s, ["<think>", "The user wants", " to know something"])
        assert out == ""


class TestResetAndReentry:
    def test_reset_clears_in_block_state(self) -> None:
        s = StreamingThinkScrubber()
        s.feed("<think>hanging")
        s.reset()
        # After reset, a new turn works cleanly
        assert _drive(s, ["Hello world"]) == "Hello world"

    def test_reset_clears_buffered_partial_tag(self) -> None:
        s = StreamingThinkScrubber()
        s.feed("word<")
        s.reset()
        assert _drive(s, ["fresh content"]) == "fresh content"


class TestFlushBehaviour:



    def test_flush_restores_stream_start_boundary(self) -> None:
        """End-of-stream flush must re-arm block-boundary gating.

        Thinking-only / empty-response retries flush then stream again
        without ``reset()``.  If flush left ``_last_emitted_ended_newline``
        False (e.g. after emitting a held-back ``<``), the next stream's
        opening ``<think>`` looked mid-line and leaked into the UI.
        """
        s = StreamingThinkScrubber()
        assert s.feed("word") == "word"
        assert s.flush() == ""
        assert (
            _drive(s, ["<think>", "secret reasoning", "</think>", "Visible answer"])
            == "Visible answer"
        )

    def test_flush_partial_tag_tail_does_not_poison_next_stream(self) -> None:
        """Flushing a held-back ``<`` must not make the next open tag leak."""
        s = StreamingThinkScrubber()
        s.feed("word<")
        assert s.flush() == "<"
        assert _drive(s, ["<think>hidden</think>Hello"]) == "Hello"


class TestRealisticStreaming:
    """Character-by-character streaming must work as well as larger chunks."""

    def test_char_by_char_closed_pair(self) -> None:
        s = StreamingThinkScrubber()
        deltas = list("<think>x</think>Hello world")
        assert _drive(s, deltas) == "Hello world"


    def test_reasoning_then_real_response_first_word_preserved(self) -> None:
        """Regression: the first word of the final response must NOT be eaten.

        Stefan's screenshot bug — 'Let me check' was being rendered as
        ' me check'.  The scrubber must not consume any character of
        post-close content.
        """
        s = StreamingThinkScrubber()
        deltas = [
            "<think>",
            "User wants to know things",
            "</think>",
            "Let me check their config.",
        ]
        assert _drive(s, deltas) == "Let me check their config."

    def test_no_tag_passthrough_is_identical(self) -> None:
        """Streams without any reasoning tags pass through byte-for-byte."""
        s = StreamingThinkScrubber()
        deltas = ["Hello ", "world ", "how ", "are ", "you?"]
        assert _drive(s, deltas) == "Hello world how are you?"


class TestChineseReasoningTags:
    """MiniMax-M3 emits Chinese reasoning tags (#43827); both surfaces must hide them."""

    def test_split_chinese_tag_scrubbed_in_stream(self) -> None:
        s = StreamingThinkScrubber()
        deltas = ["<思", "考>让我想想", "……</思考>", "\n答案是 42"]
        assert _drive(s, deltas) == "\n答案是 42"

    def test_final_response_strip_hides_chinese_tags(self) -> None:
        from agent.agent_runtime_helpers import strip_think_blocks

        out = strip_think_blocks(None, "<反思>内部推理</反思>最终答案\n<推理>未闭合的推理")
        assert "内部推理" not in out and "未闭合" not in out
        assert "最终答案" in out

    def test_cli_replay_strip_hides_chinese_tags(self) -> None:
        from cli import _strip_reasoning_tags

        assert _strip_reasoning_tags("<思考>secret</思考>答案") == "答案"


class TestNamespacedReasoningTags:
    """MiniMax-M3 also emits the namespaced ``<mm:think>``; neither surface may deliver it."""

    def test_split_namespaced_tag_scrubbed_in_stream(self) -> None:
        s = StreamingThinkScrubber()
        deltas = ["<mm:th", "ink>secret plan", "</mm:think>", "Answer."]
        assert _drive(s, deltas) == "Answer."

    def test_final_response_strip_hides_block_orphan_and_unterminated(self) -> None:
        from agent.agent_runtime_helpers import strip_think_blocks

        out = strip_think_blocks(None, "a <mm:think>hidden</mm:think> b. Report.</mm:think>Fixed.\n<mm:think>open")
        assert out == "a  b. Report.Fixed."


class TestCodeFenceAndVertexGoogleThoughtGuards:
    """Sceptic hardening for backticked tag mentions in visible prose and Vertex extra_content.google.thought."""

    def test_code_span_and_fence_mentions_preserved_across_all_strippers(self) -> None:
        from types import SimpleNamespace
        from agent.agent_runtime_helpers import extract_reasoning, strip_think_blocks
        from agent.turn_response_intake import _reasoning_preview
        from cli import _strip_reasoning_tags

        t_open = "<" + "think>"
        t_close = "</" + "think>"

        # 1. Inline code mentions of open + close tags must NOT truncate visible prose between them
        prose_inline = f"The model wraps thoughts in `{t_open}` and closes with `{t_close}`, keeping all middle text."
        assert strip_think_blocks(None, prose_inline) == prose_inline
        assert _strip_reasoning_tags(prose_inline) == prose_inline
        assert extract_reasoning(None, SimpleNamespace(content=prose_inline)) is None
        assert _reasoning_preview(None, prose_inline) == ""

        # 2. Fenced code block containing tags in visible prose must survive intact
        prose_fenced = f"Example:\n```xml\n{t_open}\ninternal\n{t_close}\n```\nVisible tail."
        assert strip_think_blocks(None, prose_fenced) == prose_fenced
        assert _strip_reasoning_tags(prose_fenced) == prose_fenced
        assert extract_reasoning(None, SimpleNamespace(content=prose_fenced)) is None

        # 3. Real thought block containing a fenced code block MUST be stripped from visible text and extracted as reasoning
        real_thought_with_code = f"{t_open}\nLet me check:\n```python\nx = 1\n```\n{t_close}\nVisible answer."
        assert strip_think_blocks(None, real_thought_with_code).strip() == "Visible answer."
        assert _strip_reasoning_tags(real_thought_with_code).strip() == "Visible answer."
        extracted = extract_reasoning(None, SimpleNamespace(content=real_thought_with_code))
        assert extracted is not None and "x = 1" in extracted

        # 4. Single unbalanced backtick on an earlier line must NOT shield a real thought block on a later line
        unbalanced = f"Here is a stray ` backtick.\n{t_open}secret thought{t_close}\nVisible tail."
        assert strip_think_blocks(None, unbalanced) == "Here is a stray ` backtick.\n\nVisible tail."

    def test_vertex_google_thought_routing_and_signature_preservation(self) -> None:
        from types import SimpleNamespace
        from agent.chat_completion_helpers_relay import RelayChatAccumulator
        from agent.reasoning_carriers import StreamCarriers, is_google_thought

        thought_delta = SimpleNamespace(
            content="** Analyzing request **\nChecking files.",
            extra_content={"google": {"thought": True, "thought_signature": "sig-vertex-123"}},
        )
        visible_delta = SimpleNamespace(
            content="Final visible reply.",
            extra_content={"google": {"thought_signature": "sig-vertex-456"}},
        )

        assert is_google_thought(thought_delta) is True
        assert is_google_thought(visible_delta) is False

        carriers = StreamCarriers()
        r_text = thought_delta.content if is_google_thought(thought_delta) else None
        r_text = carriers.feed(thought_delta, r_text)
        assert r_text == "** Analyzing request **\nChecking files."
        assert carriers.signature == "sig-vertex-123"

        # RelayChatAccumulator (dict chunks) routes google.thought content to reasoning_content, not content
        acc = RelayChatAccumulator()
        acc.observe({
            "model": "google/gemini-4-argon",
            "choices": [{
                "delta": {
                    "role": "assistant",
                    "content": "Internal Vertex thought",
                    "extra_content": {"google": {"thought": True}},
                },
            }],
        })
        acc.observe({
            "model": "google/gemini-4-argon",
            "choices": [{
                "delta": {"content": "Visible answer"},
                "finish_reason": "stop",
            }],
        })
        finalized = acc.finalize()
        msg = finalized["choices"][0]["message"]
        assert msg["content"] == "Visible answer"
        assert msg["reasoning_content"] == "Internal Vertex thought"

        # End-to-end _interruptible_streaming_api_call consumer test
        from typing import Any
        from unittest.mock import MagicMock, patch
        from run_agent import AIAgent

        agent: Any = AIAgent(
            api_key="test-key",
            base_url="https://aiplatform.googleapis.com/v1beta1/projects/uop-atlas/locations/global/endpoints/openapi",
            model="google/gemini-4-argon",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.api_mode = "chat_completions"
        emitted_text: list[str] = []
        emitted_reasoning: list[str] = []
        agent.stream_delta_callback = lambda t: emitted_text.append(t)

        def _make_chunk(content, extra_content=None, finish_reason=None):
            return SimpleNamespace(
                id="chatcmpl-vertex-1",
                choices=[
                    SimpleNamespace(
                        index=0,
                        delta=SimpleNamespace(
                            role="assistant",
                            content=content,
                            tool_calls=None,
                            reasoning_content=None,
                            reasoning=None,
                            extra_content=extra_content,
                        ),
                        finish_reason=finish_reason,
                    )
                ],
                model="google/gemini-4-argon",
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

        class _StreamStandIn:
            response = None
            def __iter__(self):
                yield _make_chunk(
                    "** Analyzing **\nInternal Vertex thought.",
                    extra_content={"google": {"thought": True, "thought_signature": "sig-e2e-999"}},
                )
                yield _make_chunk("Visible final answer.", finish_reason="stop")
            def close(self):
                pass

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _StreamStandIn()
        with patch.object(agent, "_create_request_openai_client", return_value=mock_client), \
             patch.object(agent, "_close_request_openai_client"), \
             patch.object(agent, "_fire_reasoning_delta", side_effect=lambda r: emitted_reasoning.append(r)):
            response = agent._interruptible_streaming_api_call({})

        assert "".join(emitted_text) == "Visible final answer."
        assert "".join(emitted_reasoning) == "** Analyzing **\nInternal Vertex thought."
        msg_obj = response.choices[0].message
        assert msg_obj.content == "Visible final answer."
        assert msg_obj.reasoning_content == "** Analyzing **\nInternal Vertex thought."
        assert msg_obj.extra_content == {"google": {"thought_signature": "sig-e2e-999"}}
