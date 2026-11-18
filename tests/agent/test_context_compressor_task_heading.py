"""Task-snapshot heading identity across the summarizer prompt, the template, and grounding.

The iterative-update instruction, the emitted template, and ``_ground_historical_task_snapshot`` must agree on
one heading. A leftover ``## Active Task`` section is not disclaimed by SUMMARY_PREFIX and reads as live work,
so grounding must replace it (and any duplicate task section) rather than prepend a second one.
"""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor, HISTORICAL_TASK_HEADING

_LEGACY_ACTIVE_TASK_HEADING = "## Active Task"


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]




def test_grounding_collapses_alias_and_duplicate_task_sections():
    """A summarizer that emits the legacy alias, or both headings, ends up with exactly one grounded section."""
    body = (
        f"{HISTORICAL_TASK_HEADING}\nUser asked: 'stale canonical'\n\n"
        "## Goal\nthing\n\n"
        f"{_LEGACY_ACTIVE_TASK_HEADING}\nUser asked: 'stale alias'\n\n"
        "## Constraints & Preferences\n- none\n"
    )
    grounded = ContextCompressor._ground_historical_task_snapshot.__func__(
        ContextCompressor, body, [{"role": "user", "content": "fresh ask"}]
    )

    headings = _headings(grounded)
    assert headings.count(HISTORICAL_TASK_HEADING) == 1
    assert _LEGACY_ACTIVE_TASK_HEADING not in headings
    assert headings[1:] == ["## Goal", "## Constraints & Preferences"]
    assert "fresh ask" in grounded
    assert "stale" not in grounded


def test_long_user_request_is_not_quoted_by_summary_model():
    """Long quotes stall Codex output; the postprocessor preserves source wording."""
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        compressor = ContextCompressor(model="test", quiet_mode=True)
    section = "\n".join(m["content"] for m in compressor._build_summary_prompt(
        "A long user request", 500, None, "", True
    ))
    assert "summarize it in your own words rather than copying long" in section
    for removed_directive in (
        "input verbatim — the exact words",
        "<exact latest user request>",
        "write the reverse signal verbatim",
    ):
        assert removed_directive not in section

    latest_request = "Please check this issue carefully. " * 20
    generated = (
        f"{HISTORICAL_TASK_HEADING}\nUser asked for an issue check\n\n"
        "## Goal\nCheck an issue"
    )
    grounded = ContextCompressor._ground_historical_task_snapshot(
        generated, [{"role": "user", "content": latest_request}]
    )
    assert latest_request.strip() in grounded
    assert "## Goal\nCheck an issue" in grounded


def test_protected_head_user_turn_grounds_snapshot_and_survives_merged_inflight_replay():
    """Regression (512 Garmin RCA 2026-11-18): when the sole user turn sits in protected_head
    (compress_start == 1) on Compaction #1 and is then merged onto the summary carrier via
    _INFLIGHT_TASK_REPLAY_HEADER on Compaction #2+, turns_to_summarize has zero user rows.
    _generate_summary must still ground ## Historical Task Snapshot and ## User Messages (verbatim)
    from messages[:scan.tail_start], overwriting any hallucinated summarizer task list.
    """
    user_directive = (
        "skapa en sales order så att det dras från saldot som personalköp i one, "
        "skriv inte att det är till Alina, bara Personalköp Emir Saffar."
    )
    hallucinated_llm_summary = (
        f"{HISTORICAL_TASK_HEADING}\n"
        "User instructed:\n"
        "1. Skapa en sales order i ERP One.\n"
        "2. Skriv inte att det är till Alina.\n"
        "3. Skriv ett mejl/meddelande till butik 512 (512@uropenn.se).\n\n"
        "## Goal\nCreate order.\n\n"
        "## Completed Actions\n1. READ schema [tool: terminal]\n"
    )
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="google/gemini-4-argon",
            threshold_percent=0.18,
            protect_first_n=1,
            protect_last_n=4,
            quiet_mode=True,
        )

    def _tool_pair(idx: int, chars: int = 8_000) -> list[dict]:
        cid = f"call_{idx}"
        return [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": cid, "type": "function", "function": {"name": "terminal", "arguments": '{"command":"roq"}'}}],
            },
            {"role": "tool", "tool_call_id": cid, "name": "terminal", "content": f"row-{idx}-" + ("x" * chars)},
        ]

    messages: list[dict] = [{"role": "user", "content": user_directive}]
    for i in range(12):
        messages.extend(_tool_pair(i))

    with patch.object(compressor, "_call_summary_llm", return_value=hallucinated_llm_summary):
        compacted_1 = compressor.compress(messages, current_tokens=90_000, force=True)

    summaries_1 = [m for m in compacted_1 if compressor._is_context_summary_message(m)]
    assert len(summaries_1) == 1
    s1_text = str(summaries_1[0]["content"])
    assert "User asked (deterministic, from compacted turns):" in s1_text
    assert "3. Skriv ett mejl/meddelande till butik 512" not in s1_text
    assert "## User Messages (verbatim, newest first)" in s1_text
    assert user_directive in s1_text

    # Compaction #2: user_directive now rides only on the merged in-flight replay carrier.
    messages_2 = list(compacted_1)
    for i in range(12, 24):
        messages_2.extend(_tool_pair(i))

    with patch.object(compressor, "_call_summary_llm", return_value=hallucinated_llm_summary):
        compacted_2 = compressor.compress(messages_2, current_tokens=90_000, force=True)

    summaries_2 = [m for m in compacted_2 if compressor._is_context_summary_message(m)]
    assert len(summaries_2) == 1
    s2_text = str(summaries_2[0]["content"])
    assert "User asked (deterministic, from compacted turns):" in s2_text
    assert "3. Skriv ett mejl/meddelande till butik 512" not in s2_text
    assert "## User Messages (verbatim, newest first)" in s2_text
    assert user_directive in s2_text


def test_verbatim_user_section_excludes_max_iterations_nudges():
    """MAX_ITERATIONS_SUMMARY_REQUEST (current and legacy) must never be quoted as user words."""
    from agent.context_compressor import (
        MAX_ITERATIONS_SUMMARY_REQUEST,
        _LEGACY_MAX_ITERATIONS_SUMMARY_REQUEST,
        _build_verbatim_user_section,
    )

    turns = [
        {"role": "user", "content": "check past orders and logs"},
        {"role": "user", "content": _LEGACY_MAX_ITERATIONS_SUMMARY_REQUEST},
        {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST},
    ]
    section = _build_verbatim_user_section(turns)
    assert "check past orders and logs" in section
    assert "maximum number of tool-calling iterations allowed" not in section
    assert "do NOT guess, reconstruct, or infer" in MAX_ITERATIONS_SUMMARY_REQUEST
    assert ContextCompressor._is_synthetic_compression_user_turn({"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST})
    assert ContextCompressor._is_synthetic_compression_user_turn({"role": "user", "content": _LEGACY_MAX_ITERATIONS_SUMMARY_REQUEST})
