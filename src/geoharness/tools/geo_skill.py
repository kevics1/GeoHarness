"""Skill lookup with GeoHarness aliases and recoverable discovery errors."""

from __future__ import annotations

from openharness.skills import load_skill_registry
from openharness.skills.types import SkillDefinition
from openharness.tools.base import ToolExecutionContext, ToolResult
from openharness.tools.skill_tool import SkillTool, SkillToolInput

_SKILL_ALIASES = {
    "map-making": "cartography",
    "mapmaking": "cartography",
}
_GEOGRAPHY_SKILLS = (
    "spatial-analysis",
    "cartography",
    "geo-perception",
    "geo-comprehension",
    "geo-reasoning",
)


def _canonical_name(skill: SkillDefinition) -> str:
    return skill.command_name or skill.name


class GeoSkillTool(SkillTool):
    """Preserve upstream skill policy while resolving geography terminology."""

    description = (
        "Read a bundled, user, or plugin skill by name. Geography skills: "
        "spatial-analysis, cartography, geo-perception, geo-comprehension, geo-reasoning. "
        "map-making is accepted as an alias for cartography. "
        "Unknown names return the available skills so you can use an actual name."
    )

    async def execute(self, arguments: SkillToolInput, context: ToolExecutionContext) -> ToolResult:
        registry = load_skill_registry(
            context.cwd,
            extra_skill_dirs=context.metadata.get("extra_skill_dirs"),
            extra_plugin_roots=context.metadata.get("extra_plugin_roots"),
        )
        requested = arguments.name.strip()
        normalized = requested.casefold().replace("_", "-")
        skills = registry.list_skills()
        # An explicitly installed skill named map-making takes precedence over
        # our compatibility alias, including its model invocation policy.
        skill = registry.get(requested)
        if skill is None:
            for candidate in skills:
                names = (
                    candidate.name,
                    candidate.command_name,
                    candidate.display_name,
                    *candidate.aliases,
                )
                if any(
                    name and name.casefold().replace("_", "-") == normalized for name in names
                ):
                    skill = candidate
                    break
        if skill is None:
            target = _SKILL_ALIASES.get(normalized)
            if target is not None:
                skill = registry.get(target)
        if skill is None:
            ordered = sorted(
                skills,
                key=lambda item: (
                    _canonical_name(item) not in _GEOGRAPHY_SKILLS,
                    _canonical_name(item).casefold(),
                ),
            )
            available = [_canonical_name(item) for item in ordered]
            catalog = "\n".join(
                f"- {name}" for name in available
            ) or "(No skills are currently available.)"
            return ToolResult(
                output=(
                    f"Skill not found: {arguments.name}\n"
                    f"Available skills (use these exact names):\n{catalog}"
                ),
                is_error=True,
                metadata={"requested_skill": arguments.name, "available_skills": available},
            )
        canonical = _canonical_name(skill)
        if skill.disable_model_invocation:
            return ToolResult(
                output=f"Skill {canonical} can only be invoked by the user as /{canonical}.",
                is_error=True,
                metadata={"requested_skill": arguments.name, "skill_name": canonical},
            )
        return ToolResult(
            output=skill.content,
            metadata={"requested_skill": arguments.name, "skill_name": canonical},
        )
