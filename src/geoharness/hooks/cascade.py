"""Cognitive cascade hook — L1→L2→L3 suggestion system.

This module implements the suggestion-based cognitive cascade:
- After L1 perception tools complete → suggest L2 comprehension tools
- After L2 comprehension tools complete → suggest L3 reasoning tools
- Model retains skip authority (suggestions are advisory, not mandatory)

Two components work together:
1. CascadeManager — pure Python class tracking tool usage and generating suggestions.
   Injected into tool_metadata so native tools and system prompt builder can access it.
2. PromptHookDefinition hooks — registered in HookRegistry for USER_PROMPT_SUBMIT event.
   The prompt template guides the LLM to consider the cognitive cascade when analyzing
   user intent. Even though USER_PROMPT_SUBMIT results are currently discarded by
   QueryEngine, the hooks fire and the prompt template is available for future
   result-capture enhancements.

Design rationale: OpenHarness's hook system only supports 4 declarative types
(command/prompt/http/agent), none of which can directly call back into Python code.
The CascadeManager bridges this gap by being called directly from native tool
execute() methods, while the PromptHookDefinition satisfies the declarative hook
registration requirement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from openharness.hooks.events import HookEvent
from openharness.hooks.loader import HookRegistry
from openharness.hooks.schemas import PromptHookDefinition

from geoharness.config.settings import CascadeConfig, GeoConfig

logger = logging.getLogger(__name__)

# ── L1/L2/L3 tool classification ─────────────────────────────────

# L1 perception tools (from geo-perception skill + geo-mcp-server)
L1_TOOLS: set[str] = {
    "geo_db_data",
    "geo_vector_data",
    "geo_raster_data",
    "geo_geocode",
    "geo_reverse_geocode",
    "geo_bbox",
    "geo_bbox_from_places",
    "geo_transform_crs",
    "geo_calculate_distance",
    "geo_buffer",
    "geo_spatial_relation",
    "geo_all_relations",
    "geo_scale_analysis",
    "geo_spatial_weights",
    "geo_semantic_label",
}

# L2 comprehension tools (from geo-comprehension skill + geo-mcp-server)
L2_TOOLS: set[str] = {
    "geo_moran_global",
    "geo_moran_local",
    "geo_getis_ord",
}

# L3 reasoning tools (from geo-reasoning skill + geo-mcp-server)
L3_TOOLS: set[str] = {
    "geo_cluster_detect",
    "geo_od_flow",
    "geo_causal_check",
}


# ── CascadeManager ────────────────────────────────────────────────


@dataclass
class CascadeSuggestion:
    """A single cascade suggestion."""

    trigger_tool: str
    target_level: str  # "L2" or "L3"
    target_tools: list[str]
    message: str
    consumed: bool = False


class CascadeManager:
    """Manages the L1→L2→L3 cognitive cascade state.

    Tracks which tools have been used and generates suggestions for
    the next cognitive level. Suggestions are advisory — the model
    retains skip authority.

    Usage:
        manager = CascadeManager(config.cognition.cascade)
        manager.record_tool_use("geo_spatial_relation")
        suggestions = manager.get_pending_suggestions()
        prompt_section = manager.get_prompt_section()
    """

    def __init__(self, cascade_config: CascadeConfig | None = None) -> None:
        self._config = cascade_config or CascadeConfig()
        self._tool_history: list[str] = []
        self._suggestions: list[CascadeSuggestion] = []
        self._suggestions_made: int = 0

    def record_tool_use(self, tool_name: str, tool_output: str = "") -> None:
        """Record that a tool was used and check for cascade triggers.

        Args:
            tool_name: Name of the tool that was just used.
            tool_output: Optional output from the tool (for future analysis).
        """
        self._tool_history.append(tool_name)
        logger.debug("Cascade: recorded tool use: %s", tool_name)

        suggestion = self._generate_suggestion(tool_name)
        if suggestion is not None:
            self._suggestions.append(suggestion)
            self._suggestions_made += 1
            logger.info(
                "Cascade: %s → %s suggestion generated (target: %s)",
                tool_name,
                suggestion.target_level,
                suggestion.target_tools,
            )

    def _generate_suggestion(self, tool_name: str) -> CascadeSuggestion | None:
        """Generate a cascade suggestion based on the tool that was used.

        Args:
            tool_name: Name of the tool that was just used.

        Returns:
            CascadeSuggestion if the tool triggers a cascade, None otherwise.
        """
        # Check L1→L2 trigger: if an L1 tool from the config was used
        l1_triggers = set(self._config.l1_to_l2_tools)
        if tool_name in l1_triggers or tool_name in L1_TOOLS:
            # Don't suggest if already at L2 or higher
            if self._has_used_l2_or_higher():
                return None
            return CascadeSuggestion(
                trigger_tool=tool_name,
                target_level="L2",
                target_tools=list(L2_TOOLS),
                message=self._build_l1_to_l2_message(tool_name),
            )

        # Check L2→L3 trigger: if an L2 tool from the config was used
        l2_triggers = set(self._config.l2_to_l3_tools)
        if tool_name in l2_triggers or tool_name in L2_TOOLS:
            # Don't suggest if already at L3
            if self._has_used_l3():
                return None
            return CascadeSuggestion(
                trigger_tool=tool_name,
                target_level="L3",
                target_tools=list(L3_TOOLS),
                message=self._build_l2_to_l3_message(tool_name),
            )

        return None

    def _build_l1_to_l2_message(self, trigger_tool: str) -> str:
        """Build the L1→L2 suggestion message."""
        l2_tools_str = ", ".join(L2_TOOLS)
        return (
            f"L1感知工具 `{trigger_tool}` 已完成。"
            f"如需深入分析空间模式，建议加载L2认知技能 "
            f"(geo-comprehension) 并使用以下工具: {l2_tools_str}。"
            f"\n可跳过此建议——根据分析需求自主判断。"
        )

    def _build_l2_to_l3_message(self, trigger_tool: str) -> str:
        """Build the L2→L3 suggestion message."""
        l3_tools_str = ", ".join(L3_TOOLS)
        return (
            f"L2理解工具 `{trigger_tool}` 已完成。"
            f"如发现显著空间模式(p<0.05)，建议加载L3推理技能 "
            f"(geo-reasoning) 并使用以下工具: {l3_tools_str}。"
            f"\n可跳过此建议——根据统计显著性自主判断。"
        )

    def _has_used_l2_or_higher(self) -> bool:
        """Check if any L2 or L3 tool has already been used."""
        return any(t in L2_TOOLS or t in L3_TOOLS for t in self._tool_history)

    def _has_used_l3(self) -> bool:
        """Check if any L3 tool has already been used."""
        return any(t in L3_TOOLS for t in self._tool_history)

    def get_pending_suggestions(self) -> list[CascadeSuggestion]:
        """Return pending (unconsumed) suggestions.

        Does NOT clear suggestions — call consume_suggestions() for that.
        """
        return [s for s in self._suggestions if not s.consumed]

    def consume_suggestions(self) -> list[CascadeSuggestion]:
        """Return all pending suggestions and mark them as consumed.

        Returns:
            List of suggestions that were pending. They are now marked consumed.
        """
        pending = self.get_pending_suggestions()
        for s in pending:
            s.consumed = True
        return pending

    def get_prompt_section(self) -> str:
        """Build a markdown section for the system prompt.

        Includes:
        - Current cognitive level (based on tools used)
        - Any pending suggestions
        - Cascade configuration
        """
        lines: list[str] = ["## 认知级联状态 (Cognitive Cascade)"]

        # Current level
        level = self.get_current_level()
        lines.append(f"**当前认知层级**: {level}")
        lines.append("")

        # Tool history summary
        if self._tool_history:
            used_tools = list(dict.fromkeys(self._tool_history))  # dedup, preserve order
            lines.append(f"**已用工具**: {', '.join(used_tools)}")
            lines.append("")

        # Pending suggestions
        pending = self.get_pending_suggestions()
        if pending:
            lines.append("### 级联建议 (Cascade Suggestions)")
            lines.append("")
            for s in pending:
                lines.append(f"- {s.message}")
            lines.append("")
            lines.append("> 建议为 advisory 性质，可根据分析需求自主跳过。")
        else:
            lines.append("*暂无级联建议*")
            lines.append("")

        # Cascade trigger config
        lines.append("### 级联配置")
        lines.append("")
        lines.append(
            f"- L1→L2触发工具: {', '.join(self._config.l1_to_l2_tools)}"
        )
        lines.append(
            f"- L2→L3触发工具: {', '.join(self._config.l2_to_l3_tools)}"
        )

        return "\n".join(lines)

    def get_current_level(self) -> str:
        """Return the current cognitive level based on tool history.

        Returns:
            "L0" (no tools used), "L1", "L2", or "L3"
        """
        if not self._tool_history:
            return "L0"
        if self._has_used_l3():
            return "L3"
        if self._has_used_l2_or_higher():
            return "L2"
        if any(t in L1_TOOLS for t in self._tool_history):
            return "L1"
        return "L0"

    def clear(self) -> None:
        """Clear all state."""
        self._tool_history.clear()
        self._suggestions.clear()
        self._suggestions_made = 0

    @property
    def tool_history(self) -> list[str]:
        """Return a copy of the tool usage history."""
        return list(self._tool_history)

    @property
    def suggestions_made(self) -> int:
        """Total number of suggestions generated (including consumed)."""
        return self._suggestions_made

    @property
    def total_suggestions(self) -> int:
        """Total number of suggestions in the list (including consumed)."""
        return len(self._suggestions)


# ── Hook Definitions ──────────────────────────────────────────────


def build_cascade_hook_definitions(
    config: GeoConfig,
) -> list[tuple[HookEvent, PromptHookDefinition]]:
    """Build PromptHookDefinition hooks for the cognitive cascade.

    These hooks are registered in the HookRegistry and fire on
    USER_PROMPT_SUBMIT events. The prompt template guides the LLM
    to consider the cognitive cascade when analyzing user intent.

    Args:
        config: GeoHarness configuration.

    Returns:
        List of (HookEvent, PromptHookDefinition) tuples.
    """
    cascade = config.cognition.cascade
    l1_tools_str = ", ".join(cascade.l1_to_l2_tools)
    l2_tools_str = ", ".join(cascade.l2_to_l3_tools)

    # USER_PROMPT_SUBMIT hook: analyze user intent for cascade
    user_prompt_hook = PromptHookDefinition(
        type="prompt",
        prompt=(
            "分析用户输入的空间分析意图，判断应从哪个认知层级开始：\n"
            f"- L1感知工具 ({l1_tools_str}): 数据加载、地理编码、空间关系\n"
            f"- L2理解工具 ({l2_tools_str}): 空间自相关、热点分析\n"
            f"- L3推理工具: 聚类检测、流向分析、因果检验\n\n"
            f"用户输入: $ARGUMENTS\n\n"
            f"如果用户输入涉及空间模式分析，建议加载L2认知技能。"
            f"如果涉及因果推理，建议加载L3认知技能。"
        ),
        model=None,  # Use default model
        timeout_seconds=30,
        matcher=None,  # Match all prompts
        block_on_failure=False,  # Don't block on suggestion failure
    )

    return [(HookEvent.USER_PROMPT_SUBMIT, user_prompt_hook)]


def register_cascade_hooks(
    registry: HookRegistry,
    config: GeoConfig,
) -> None:
    """Register cascade hooks in the HookRegistry.

    Args:
        registry: HookRegistry to register hooks in.
        config: GeoHarness configuration.
    """
    hooks = build_cascade_hook_definitions(config)
    for event, hook_def in hooks:
        registry.register(event, hook_def)
        logger.info(
            "Registered cascade hook: event=%s, type=%s",
            event.value,
            hook_def.type,
        )


def build_cascade_manager(config: GeoConfig) -> CascadeManager:
    """Create a CascadeManager from GeoConfig.

    Args:
        config: GeoHarness configuration.

    Returns:
        Configured CascadeManager instance.
    """
    return CascadeManager(config.cognition.cascade)
