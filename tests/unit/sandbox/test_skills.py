from email_agent.sandbox.inmemory_environment import InMemoryEnvironment
from email_agent.sandbox.skills import (
    ensure_starter_files,
    load_skills,
    read_context,
    render_context_block,
    render_skills_block,
)
from email_agent.sandbox.workspace import AssistantWorkspace


async def test_load_skills_returns_empty_when_skills_dir_missing() -> None:
    env = InMemoryEnvironment()
    assert await load_skills(env) == []


async def test_load_skills_parses_frontmatter_and_body() -> None:
    env = InMemoryEnvironment()
    await env.mkdir("/workspace/skills/triage", parents=True)
    await env.write_text(
        "/workspace/skills/triage/SKILL.md",
        "---\nname: triage\ndescription: How to triage incoming mail.\n---\n\nBody here.\n",
    )

    skills = await load_skills(env)

    assert len(skills) == 1
    assert skills[0].name == "triage"
    assert skills[0].description == "How to triage incoming mail."
    assert "Body here." in skills[0].body
    assert skills[0].path == "/workspace/skills/triage/SKILL.md"


async def test_load_skills_skips_dirs_without_skill_md() -> None:
    env = InMemoryEnvironment()
    await env.mkdir("/workspace/skills/empty", parents=True)
    await env.mkdir("/workspace/skills/real", parents=True)
    await env.write_text("/workspace/skills/real/SKILL.md", "# real")

    names = [s.name for s in await load_skills(env)]
    assert names == ["real"]


async def test_load_skills_falls_back_to_dirname_when_no_frontmatter() -> None:
    env = InMemoryEnvironment()
    await env.mkdir("/workspace/skills/fallback-name", parents=True)
    await env.write_text("/workspace/skills/fallback-name/SKILL.md", "no frontmatter here")

    skills = await load_skills(env)
    assert skills[0].name == "fallback-name"
    assert skills[0].body.startswith("no frontmatter")


async def test_read_context_returns_none_when_missing_or_empty() -> None:
    env = InMemoryEnvironment()
    assert await read_context(env) is None

    await env.write_text("/workspace/CONTEXT.md", "   \n  ")
    assert await read_context(env) is None


async def test_read_context_returns_stripped_content() -> None:
    env = InMemoryEnvironment()
    await env.write_text("/workspace/CONTEXT.md", "\nuser likes brevity\n")

    assert await read_context(env) == "user likes brevity"


async def test_ensure_starter_files_creates_context_and_writing_skill_once() -> None:
    env = InMemoryEnvironment()
    await ensure_starter_files(env)

    assert await env.exists("/workspace/CONTEXT.md")
    assert await env.exists("/workspace/skills/writing-skills/SKILL.md")
    assert await env.exists("/workspace/skills/managing-context/SKILL.md")
    assert await env.exists("/workspace/skills/scheduling-tasks/SKILL.md")

    # Idempotent: customising and re-running must not overwrite user edits.
    await env.write_text("/workspace/CONTEXT.md", "custom content")
    await ensure_starter_files(env)
    assert await env.read_text("/workspace/CONTEXT.md") == "custom content"


async def test_scheduling_tasks_starter_skill_shows_in_rendered_manifest() -> None:
    """The seeded scheduling-tasks skill must surface in the prompt manifest
    (name + full path) so the agent uses the dedicated tools instead of bash."""
    env = InMemoryEnvironment()
    await ensure_starter_files(env)

    rendered = render_skills_block(await load_skills(env))

    assert "scheduling-tasks" in rendered
    assert "/workspace/skills/scheduling-tasks/SKILL.md" in rendered
    # The description hint that nudges the agent toward the right tools.
    assert "synthetic inbound" in rendered.lower() or "reminder" in rendered.lower()


async def test_skill_written_during_one_run_is_visible_to_next_load() -> None:
    """A new skill created by the agent during a run must appear on next load."""
    env = InMemoryEnvironment()
    await ensure_starter_files(env)
    starter_names = {s.name for s in await load_skills(env)}

    # Simulate agent adding a skill mid-run.
    await env.mkdir("/workspace/skills/drafting-replies", parents=True)
    await env.write_text(
        "/workspace/skills/drafting-replies/SKILL.md",
        "---\nname: drafting-replies\ndescription: how to draft\n---\n\nbody",
    )

    next_names = {s.name for s in await load_skills(env)}
    assert "drafting-replies" in next_names - starter_names


async def test_render_skills_block_is_manifest_only() -> None:
    env = InMemoryEnvironment()
    await env.mkdir("/workspace/skills/triage", parents=True)
    await env.write_text(
        "/workspace/skills/triage/SKILL.md",
        "---\nname: triage\ndescription: triage mail\n---\n\nsecret body text",
    )

    rendered = render_skills_block(await load_skills(env))
    assert "triage" in rendered
    assert "triage mail" in rendered
    assert "/workspace/skills/triage/SKILL.md" in rendered
    assert "secret body text" not in rendered


def test_render_skills_block_empty_when_no_skills() -> None:
    assert render_skills_block([]) == ""


def test_render_context_block_empty_when_none() -> None:
    assert render_context_block(None) == ""


def test_render_context_block_labels_section() -> None:
    block = render_context_block("user is in AEST")
    assert "CONTEXT.md" in block
    assert "user is in AEST" in block


async def test_workspace_proxies_skills_and_context() -> None:
    env = InMemoryEnvironment()
    workspace = AssistantWorkspace(env)
    await workspace.ensure_starter_files()

    assert await workspace.read_context() is not None
    skill_names = {s.name for s in await workspace.load_skills()}
    assert {"writing-skills", "managing-context", "scheduling-tasks"} <= skill_names
