"""Regression tests for Wave 1B (subagent parent skill inheritance across compactions)
and Wave 2B (stacked slash-skill and bundle already_loaded deduplication)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import pytest

import tools.skills_tool as skills_tool_mod
from tools.skills_tool_dedup import reset_skill_view_dedup


@pytest.fixture(autouse=True)
def _clean_dedup():
    reset_skill_view_dedup()
    yield
    reset_skill_view_dedup()


def _setup_skill(tmp_path: Path, monkeypatch, name: str, body: str) -> Path:
    monkeypatch.setattr(skills_tool_mod, "SKILLS_DIR", tmp_path)
    sdir = tmp_path / name
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n{body}",
        encoding="utf-8",
    )
    return sdir


def test_collect_parent_loaded_skills_recovers_compacted_pruned_and_tracker_skills():
    from agent.review_engine import collect_parent_loaded_skills
    from tools.delegate_tool_progress import child_task_message

    parent = SimpleNamespace(
        ephemeral_system_prompt='[IMPORTANT: launched with the "always-verify" skill preloaded.]',
        _current_task_id="sess-parent-inherit",
        session_id="sess-parent-inherit",
    )
    compacted_messages = [
        {
            "role": "user",
            "content": (
                "[CONTEXT COMPACTION — REFERENCE ONLY]\n"
                "## Pruned Skills\n"
                "[SKILL_PRUNED: content lost in compression; reload with skill_view(name='systematic-debugging')]\n"
                "[SKILL_PRUNED: content lost in compression; reload with skill_view(name='hermes-config-ops')]\n"
            ),
        }
    ]
    skills = collect_parent_loaded_skills(parent, compacted_messages, limit=5)
    assert skills == ["always-verify", "systematic-debugging", "hermes-config-ops"]

    # Empty inherited_skills keeps child_task_message byte-identical for sibling cache tests
    assert child_task_message("count the files", "The parent is auditing numbers.") == (
        "CONTEXT:\nThe parent is auditing numbers.\n\nTASK:\ncount the files"
    )
    # Non-empty inherited_skills appends Skills loaded footer to the task turn
    with_skills = child_task_message(
        "count the files",
        "The parent is auditing numbers.",
        inherited_skills=skills,
    )
    assert with_skills.startswith("CONTEXT:\nThe parent is auditing numbers.\n\nTASK:\ncount the files\n\nSkills loaded: ")
    assert "always-verify, systematic-debugging, hermes-config-ops" in with_skills


def test_stacked_skill_and_bundle_skip_already_loaded_bodies_in_same_task(tmp_path, monkeypatch):
    from agent.skill_commands import build_stacked_skill_invocation_message, reload_skills

    _setup_skill(tmp_path, monkeypatch, "skill-alpha", body="# Alpha Body\n" + ("Alpha line.\n" * 80))
    _setup_skill(tmp_path, monkeypatch, "skill-beta", body="# Beta Body\n" + ("Beta line.\n" * 80))
    reload_skills()

    tid = "sess-stacked-dedup"
    first = build_stacked_skill_invocation_message(["/skill-alpha", "/skill-beta"], "run pass 1", task_id=tid)
    assert first is not None
    msg1, loaded1, _ = first
    assert loaded1 == ["skill-alpha", "skill-beta"]
    assert "# Alpha Body" in msg1 and "# Beta Body" in msg1

    # Second stacked invocation in the same task_id keeps header + instruction but skips duplicate bodies
    second = build_stacked_skill_invocation_message(["/skill-alpha", "/skill-beta"], "run pass 2", task_id=tid)
    assert second is not None
    msg2, loaded2, _ = second
    assert loaded2 == ["skill-alpha", "skill-beta"]
    assert "User instruction: run pass 2" in msg2
    assert "# Alpha Body" not in msg2 and "# Beta Body" not in msg2
    assert len(msg2) < len(msg1) // 3


def test_child_system_prompt_injects_boundary_floor_and_active_blackboard_vetoes(tmp_path, monkeypatch):
    import sqlite3
    from tools.delegate_tool_progress import _build_child_system_prompt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "blackboard.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE estate_vetoes (id INTEGER PRIMARY KEY, scope TEXT, rule TEXT, active INTEGER)"
    )
    conn.execute(
        "INSERT INTO estate_vetoes (id, scope, rule, active) VALUES (1, 'global', 'Never use --allow-unauthenticated without GO', 1)"
    )
    conn.execute(
        "INSERT INTO estate_vetoes (id, scope, rule, active) VALUES (2, 'legacy', 'Retired rule', 0)"
    )
    conn.commit()
    conn.close()

    prompt_a = _build_child_system_prompt(workspace_path=str(tmp_path))
    prompt_b = _build_child_system_prompt(workspace_path=str(tmp_path))
    assert prompt_a == prompt_b
    assert "Subagent Safety & Verification Boundaries" in prompt_a
    assert "Active Estate Vetoes (blackboard.db):" in prompt_a
    assert "[global] Never use --allow-unauthenticated without GO" in prompt_a
    assert "Retired rule" not in prompt_a

