"""Filesystem-backed agent skills + CONTEXT.md.

A skill is a directory under `/workspace/skills/<name>/` containing a
`SKILL.md` file. The file may begin with a YAML frontmatter block:

    ---
    name: managing-context
    description: Maintain the long-term CONTEXT.md notes file.
    ---

    # body...

Skills are loaded on every run so a skill the agent writes during a run is
visible on the next run with no restart. CONTEXT.md sits at the workspace
root and is injected verbatim into the system prompt so the agent has a
durable place to record long-term knowledge about the user.
"""

from dataclasses import dataclass

from email_agent.sandbox.environment import SandboxEnvironment

SKILLS_DIR = "/workspace/skills"
CONTEXT_PATH = "/workspace/CONTEXT.md"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: str


async def load_skills(env: SandboxEnvironment) -> list[Skill]:
    if not await env.exists(SKILLS_DIR):
        return []
    entries = await env.readdir(SKILLS_DIR)
    skills: list[Skill] = []
    for entry in entries:
        skill_path = f"{SKILLS_DIR}/{entry}/SKILL.md"
        if not await env.exists(skill_path):
            continue
        raw = await env.read_text(skill_path)
        name, description, body = _parse_skill(raw, default_name=entry)
        skills.append(Skill(name=name, description=description, body=body, path=skill_path))
    return sorted(skills, key=lambda s: s.name)


async def read_context(env: SandboxEnvironment) -> str | None:
    if not await env.exists(CONTEXT_PATH):
        return None
    text = await env.read_text(CONTEXT_PATH)
    stripped = text.strip()
    return stripped or None


async def ensure_starter_files(env: SandboxEnvironment) -> None:
    """Idempotent seed: starter skill + an empty CONTEXT.md template."""
    if not await env.exists(CONTEXT_PATH):
        await env.write_text(CONTEXT_PATH, _STARTER_CONTEXT_MD)

    writing_skill = f"{SKILLS_DIR}/writing-skills/SKILL.md"
    if not await env.exists(writing_skill):
        await env.mkdir(f"{SKILLS_DIR}/writing-skills", parents=True)
        await env.write_text(writing_skill, _STARTER_SKILL_WRITING_SKILLS)

    context_skill = f"{SKILLS_DIR}/managing-context/SKILL.md"
    if not await env.exists(context_skill):
        await env.mkdir(f"{SKILLS_DIR}/managing-context", parents=True)
        await env.write_text(context_skill, _STARTER_SKILL_MANAGING_CONTEXT)


def render_skills_block(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = [
        "# Available skills",
        "",
        "Only the name + description for each skill is listed here. When a skill",
        "looks relevant to the current task, use the `read` tool on its path to",
        "load the full body before following it.",
        "",
    ]
    for skill in skills:
        lines.append(f"- **{skill.name}** ({skill.path})")
        if skill.description:
            lines.append(f"  {skill.description}")
    return "\n".join(lines).rstrip()


def render_context_block(context: str | None) -> str:
    if context is None:
        return ""
    return "# CONTEXT.md (long-term notes about the user)\n\n" + context.strip()


SYSTEM_PROMPT_GUIDANCE = (
    "You have a writable workspace under /workspace. Two paths are special:\n"
    "  * /workspace/CONTEXT.md — durable notes about the user (who they are, "
    "their preferences, working style). Read it for context and update it via "
    "the `edit` tool whenever you learn something durable. Keep it concise.\n"
    "  * /workspace/skills/<name>/SKILL.md — reusable playbooks. The section "
    "below lists each skill by name + description only; when one looks relevant, "
    "use the `read` tool on its path to load the full body before following it. "
    "You may add a new skill by writing a new SKILL.md; it will be auto-loaded "
    "on the next run. See the `writing-skills` skill for the file format."
)


def _parse_skill(raw: str, *, default_name: str) -> tuple[str, str, str]:
    name = default_name
    description = ""
    body = raw

    if raw.startswith("---\n"):
        end = raw.find("\n---", 4)
        if end != -1:
            frontmatter = raw[4:end]
            body = raw[end + 4 :].lstrip("\n")
            for line in frontmatter.splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key == "name" and value:
                    name = value
                elif key == "description" and value:
                    description = value

    return name, description, body


_STARTER_CONTEXT_MD = """Long-term notes about the user. Keep this short and high-signal. Update via
the `edit` tool when you learn something durable (preferences, working style,
recurring projects, key relationships). This file is injected into your
system prompt every run.

(empty)
"""


_STARTER_SKILL_WRITING_SKILLS = """---
name: writing-skills
description: How to author a new skill that will be auto-loaded on the next run.
---

# Writing a new skill

A skill is a folder under `/workspace/skills/<name>/` containing a single
`SKILL.md` file. The file MUST begin with a YAML frontmatter block:

```
---
name: <kebab-case-name>
description: <one-sentence description of when to use this skill>
---

# Body in markdown...
```

Steps to add a skill:

1. Pick a kebab-case name (e.g. `drafting-status-updates`).
2. Use the `bash` tool to `mkdir -p /workspace/skills/<name>`.
3. Use `write` to create `/workspace/skills/<name>/SKILL.md` with the
   frontmatter + body.
4. The next run will see the skill automatically — no restart needed.

Keep skills focused (one workflow per skill) and concrete (include examples
of inputs/outputs). Only the skill's name + description are listed in the
system prompt; the body is loaded on demand via the `read` tool when the
agent decides the skill is relevant. Still write tight — a clear description
helps the agent know when to reach for the skill.
"""


_STARTER_SKILL_MANAGING_CONTEXT = """---
name: managing-context
description: Curate /workspace/CONTEXT.md with durable knowledge about the user.
---

# Maintaining CONTEXT.md

`/workspace/CONTEXT.md` is your long-term scratchpad for facts about the
user that should survive across runs. The whole file is injected into your
system prompt every run (unlike skills, which are listed by name only and
loaded on demand), so keep it tight.

What belongs:
- Identity: name, role, company, timezone.
- Preferences: tone, signature, formats, do-not-do list.
- Recurring projects / people / accounts.
- Working style and rituals.

What does not belong:
- Per-thread chatter (use durable memory via `memory_search` instead).
- Anything secret enough you wouldn't paste into a prompt.

How to update:
1. `read('/workspace/CONTEXT.md')` to see current state.
2. Use the `edit` tool with a precise `old`/`new` pair, OR `write` to
   replace the whole file if it has drifted.
3. Prefer terse bullet points over prose. Aim for under ~50 lines.
"""


__all__ = [
    "CONTEXT_PATH",
    "SKILLS_DIR",
    "SYSTEM_PROMPT_GUIDANCE",
    "Skill",
    "ensure_starter_files",
    "load_skills",
    "read_context",
    "render_context_block",
    "render_skills_block",
]
