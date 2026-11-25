from types import SimpleNamespace

from agent import system_prompt as sp


def test_standalone_skills_compact_categories_applies_outside_coding_focus(monkeypatch):
    captured = {}

    def fake_build_skills_system_prompt(**kwargs):
        captured.update(kwargs)
        return "<available_skills>ok</available_skills>"

    monkeypatch.setattr(sp._pb, "build_skills_system_prompt", fake_build_skills_system_prompt)
    monkeypatch.setattr(
        "agent.coding_context.coding_compact_skill_categories",
        lambda **kwargs: frozenset(),
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"skills": {"compact_categories": ["finance", "creative", "media"]}},
    )

    fake_agent = SimpleNamespace(
        valid_tool_names={"skills_list", "skill_view", "terminal"},
        platform="cron",
    )
    out = sp._skills_prompt(fake_agent)
    assert out == "<available_skills>ok</available_skills>"
    assert captured["compact_categories"] == frozenset({"finance", "creative", "media"})


def test_standalone_skills_compact_categories_unions_with_coding_context(monkeypatch):
    captured = {}

    def fake_build_skills_system_prompt(**kwargs):
        captured.update(kwargs)
        return "<available_skills>ok</available_skills>"

    monkeypatch.setattr(sp._pb, "build_skills_system_prompt", fake_build_skills_system_prompt)
    monkeypatch.setattr(
        "agent.coding_context.coding_compact_skill_categories",
        lambda **kwargs: frozenset({"apple"}),
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"skills": {"compact_categories": ["finance"]}},
    )

    fake_agent = SimpleNamespace(
        valid_tool_names={"skills_list", "skill_view"},
        platform="desktop",
    )
    sp._skills_prompt(fake_agent)
    assert captured["compact_categories"] == frozenset({"apple", "finance"})
