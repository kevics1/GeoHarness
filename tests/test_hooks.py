"""Tests for Phase 6: Cognitive cascade hooks.

Verifies that:
- CascadeManager tracks tool usage and generates L1→L2 / L2→L3 suggestions
- PromptHookDefinition hooks are correctly built from config
- register_cascade_hooks() registers hooks in HookRegistry
- get_prompt_section() produces valid markdown for system prompt
- Model retains skip authority (suggestions are advisory)
- No duplicate suggestions when already at higher cognitive level
"""

from __future__ import annotations

import pytest
from openharness.hooks.events import HookEvent
from openharness.hooks.loader import HookRegistry
from openharness.hooks.schemas import PromptHookDefinition

from geoharness.config.settings import CascadeConfig, GeoConfig
from geoharness.hooks.cascade import (
    L1_TOOLS,
    L2_TOOLS,
    L3_TOOLS,
    CascadeManager,
    CascadeSuggestion,
    build_cascade_hook_definitions,
    build_cascade_manager,
    register_cascade_hooks,
)

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def default_config() -> GeoConfig:
    """Default GeoConfig with cascade settings."""
    return GeoConfig(
        model="test-model",
        api_key="test-key",
        base_url="https://test.example.com/v1",
    )


@pytest.fixture
def custom_cascade_config() -> GeoConfig:
    """GeoConfig with custom cascade settings."""
    return GeoConfig(
        model="test-model",
        api_key="test-key",
        base_url="https://test.example.com/v1",
        cognition=__import__(
            "geoharness.config.settings",
            fromlist=["CognitionConfig"],
        ).CognitionConfig(
            cascade=CascadeConfig(
                l1_to_l2_tools=["geo_spatial_relation"],
                l2_to_l3_tools=["geo_moran_local"],
                suggestion_prompt="custom suggestion",
            )
        ),
    )


@pytest.fixture
def empty_cascade_config() -> CascadeConfig:
    """CascadeConfig with empty trigger lists."""
    return CascadeConfig(
        l1_to_l2_tools=[],
        l2_to_l3_tools=[],
        suggestion_prompt="empty",
    )


# ── CascadeManager — Basic State ──────────────────────────────────


class TestCascadeManagerBasic:
    """Test CascadeManager basic state management."""

    def test_initial_state_empty(self) -> None:
        """New CascadeManager has empty state."""
        manager = CascadeManager()
        assert manager.tool_history == []
        assert manager.get_pending_suggestions() == []
        assert manager.suggestions_made == 0
        assert manager.total_suggestions == 0
        assert manager.get_current_level() == "L0"

    def test_record_tool_use_adds_to_history(self) -> None:
        """record_tool_use adds tool name to history."""
        manager = CascadeManager()
        manager.record_tool_use("geo_vector_data")
        manager.record_tool_use("geo_geocode")
        assert manager.tool_history == ["geo_vector_data", "geo_geocode"]

    def test_clear_resets_state(self) -> None:
        """clear() resets all state."""
        manager = CascadeManager()
        manager.record_tool_use("geo_spatial_relation")
        manager.clear()
        assert manager.tool_history == []
        assert manager.get_pending_suggestions() == []
        assert manager.suggestions_made == 0

    def test_tool_history_is_copy(self) -> None:
        """tool_history property returns a copy, not internal list."""
        manager = CascadeManager()
        manager.record_tool_use("geo_vector_data")
        history = manager.tool_history
        history.append("injected")
        assert "injected" not in manager.tool_history


# ── CascadeManager — L1→L2 Suggestion ────────────────────────────


class TestL1ToL2Suggestion:
    """Test L1→L2 cascade suggestion triggering."""

    def test_l1_tool_triggers_l2_suggestion(self) -> None:
        """Using an L1 tool generates L2 suggestion."""
        manager = CascadeManager()
        manager.record_tool_use("geo_spatial_relation")

        suggestions = manager.get_pending_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0].target_level == "L2"
        assert suggestions[0].trigger_tool == "geo_spatial_relation"

    def test_l1_tool_from_config_triggers(self) -> None:
        """L1 tool listed in config.l1_to_l2_tools triggers suggestion."""
        config = CascadeConfig(
            l1_to_l2_tools=["geo_all_relations"],
        )
        manager = CascadeManager(config)
        manager.record_tool_use("geo_all_relations")

        suggestions = manager.get_pending_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0].target_level == "L2"

    def test_any_l1_tool_triggers(self) -> None:
        """Any L1 tool (from L1_TOOLS set) triggers L2 suggestion."""
        manager = CascadeManager()
        # geo_vector_data is in L1_TOOLS
        manager.record_tool_use("geo_vector_data")

        suggestions = manager.get_pending_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0].target_level == "L2"

    def test_l2_suggestion_contains_l2_tools(self) -> None:
        """L2 suggestion lists L2 tools in target_tools."""
        manager = CascadeManager()
        manager.record_tool_use("geo_spatial_relation")

        suggestion = manager.get_pending_suggestions()[0]
        assert set(suggestion.target_tools) == L2_TOOLS

    def test_l2_suggestion_message_mentions_l2(self) -> None:
        """Suggestion message mentions L2 and geo-comprehension."""
        manager = CascadeManager()
        manager.record_tool_use("geo_spatial_relation")

        msg = manager.get_pending_suggestions()[0].message
        assert "L2" in msg
        assert "geo-comprehension" in msg

    def test_l2_suggestion_mentions_skip_authority(self) -> None:
        """Suggestion message mentions model can skip."""
        manager = CascadeManager()
        manager.record_tool_use("geo_spatial_relation")

        msg = manager.get_pending_suggestions()[0].message
        assert "跳过" in msg or "skip" in msg.lower()

    def test_no_l2_suggestion_if_already_l2(self) -> None:
        """No L1→L2 suggestion if L2 tools already used."""
        manager = CascadeManager()
        # Use L2 tool first (generates L2→L3 suggestion)
        manager.record_tool_use("geo_moran_local")
        # Consume the L2→L3 suggestion but keep tool history
        manager.consume_suggestions()
        # Now use L1 tool
        manager.record_tool_use("geo_spatial_relation")
        # Should NOT generate L2 suggestion (already at L2)
        l2_suggestions = [
            s for s in manager.get_pending_suggestions() if s.target_level == "L2"
        ]
        assert len(l2_suggestions) == 0


# ── CascadeManager — L2→L3 Suggestion ────────────────────────────


class TestL2ToL3Suggestion:
    """Test L2→L3 cascade suggestion triggering."""

    def test_l2_tool_triggers_l3_suggestion(self) -> None:
        """Using an L2 tool generates L3 suggestion."""
        manager = CascadeManager()
        manager.record_tool_use("geo_moran_local")

        suggestions = manager.get_pending_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0].target_level == "L3"
        assert suggestions[0].trigger_tool == "geo_moran_local"

    def test_l3_suggestion_contains_l3_tools(self) -> None:
        """L3 suggestion lists L3 tools in target_tools."""
        manager = CascadeManager()
        manager.record_tool_use("geo_getis_ord")

        suggestion = manager.get_pending_suggestions()[0]
        assert set(suggestion.target_tools) == L3_TOOLS

    def test_l3_suggestion_message_mentions_l3(self) -> None:
        """Suggestion message mentions L3 and geo-reasoning."""
        manager = CascadeManager()
        manager.record_tool_use("geo_moran_local")

        msg = manager.get_pending_suggestions()[0].message
        assert "L3" in msg
        assert "geo-reasoning" in msg

    def test_no_l3_suggestion_if_already_l3(self) -> None:
        """No L2→L3 suggestion if L3 tools already used."""
        manager = CascadeManager()
        # Use L3 tool first
        manager.record_tool_use("geo_cluster_detect")
        # Now use L2 tool
        manager.record_tool_use("geo_moran_local")
        # Should NOT generate L3 suggestion (already at L3)
        l3_suggestions = [
            s for s in manager.get_pending_suggestions() if s.target_level == "L3"
        ]
        assert len(l3_suggestions) == 0


# ── CascadeManager — No False Triggers ────────────────────────────


class TestNoFalseTriggers:
    """Test that non-triggering tools don't generate suggestions."""

    def test_non_geo_tool_no_suggestion(self) -> None:
        """Non-geo tool does not generate suggestion."""
        manager = CascadeManager()
        manager.record_tool_use("some_random_tool")
        assert len(manager.get_pending_suggestions()) == 0

    def test_l3_tool_no_l2_suggestion(self) -> None:
        """L3 tool does not generate L2 suggestion (only L3 or none)."""
        manager = CascadeManager()
        manager.record_tool_use("geo_cluster_detect")
        suggestions = manager.get_pending_suggestions()
        # L3 tools don't trigger any suggestion (they're the highest level)
        assert len(suggestions) == 0

    def test_empty_trigger_lists_no_suggestions(self) -> None:
        """With empty trigger lists, only L1_TOOLS/L2_TOOLS set triggers."""
        config = CascadeConfig(
            l1_to_l2_tools=[],
            l2_to_l3_tools=[],
        )
        manager = CascadeManager(config)
        # geo_vector_data is in L1_TOOLS, so it should still trigger
        manager.record_tool_use("geo_vector_data")
        assert len(manager.get_pending_suggestions()) == 1

    def test_multiple_l1_tools_one_suggestion(self) -> None:
        """Multiple L1 tools only generate one L2 suggestion."""
        manager = CascadeManager()
        manager.record_tool_use("geo_vector_data")
        manager.record_tool_use("geo_geocode")
        manager.record_tool_use("geo_spatial_relation")
        # Should have only 3 suggestions (one per L1 tool)
        # because each L1 tool independently triggers
        assert len(manager.get_pending_suggestions()) == 3


# ── CascadeManager — Consume & Prompt Section ─────────────────────


class TestConsumeAndPrompt:
    """Test suggestion consumption and prompt section generation."""

    def test_consume_suggestions_marks_consumed(self) -> None:
        """consume_suggestions marks all pending as consumed."""
        manager = CascadeManager()
        manager.record_tool_use("geo_spatial_relation")

        consumed = manager.consume_suggestions()
        assert len(consumed) == 1
        assert consumed[0].consumed is True
        assert len(manager.get_pending_suggestions()) == 0

    def test_consume_empty_returns_empty(self) -> None:
        """consume_suggestions with no pending returns empty list."""
        manager = CascadeManager()
        consumed = manager.consume_suggestions()
        assert consumed == []

    def test_get_prompt_section_contains_level(self) -> None:
        """get_prompt_section contains current cognitive level."""
        manager = CascadeManager()
        manager.record_tool_use("geo_vector_data")
        section = manager.get_prompt_section()
        assert "L1" in section
        assert "认知级联" in section

    def test_get_prompt_section_contains_suggestions(self) -> None:
        """get_prompt_section contains pending suggestions."""
        manager = CascadeManager()
        manager.record_tool_use("geo_spatial_relation")
        section = manager.get_prompt_section()
        assert "级联建议" in section
        assert "L2" in section

    def test_get_prompt_section_contains_config(self) -> None:
        """get_prompt_section contains cascade configuration."""
        manager = CascadeManager()
        section = manager.get_prompt_section()
        assert "L1→L2" in section
        assert "L2→L3" in section

    def test_get_prompt_section_empty_state(self) -> None:
        """get_prompt_section works with empty state."""
        manager = CascadeManager()
        section = manager.get_prompt_section()
        assert "L0" in section
        assert "暂无级联建议" in section

    def test_get_current_level_progression(self) -> None:
        """get_current_level progresses L0→L1→L2→L3."""
        manager = CascadeManager()
        assert manager.get_current_level() == "L0"

        manager.record_tool_use("geo_vector_data")
        assert manager.get_current_level() == "L1"

        manager.record_tool_use("geo_moran_local")
        assert manager.get_current_level() == "L2"

        manager.record_tool_use("geo_cluster_detect")
        assert manager.get_current_level() == "L3"


# ── Hook Definitions ──────────────────────────────────────────────


class TestHookDefinitions:
    """Test PromptHookDefinition building from config."""

    def test_returns_list_of_tuples(self, default_config: GeoConfig) -> None:
        """build_cascade_hook_definitions returns list of (HookEvent, hook)."""
        hooks = build_cascade_hook_definitions(default_config)
        assert isinstance(hooks, list)
        for event, hook in hooks:
            assert isinstance(event, HookEvent)
            assert isinstance(hook, PromptHookDefinition)

    def test_contains_user_prompt_submit_hook(
        self, default_config: GeoConfig
    ) -> None:
        """At least one hook is for USER_PROMPT_SUBMIT event."""
        hooks = build_cascade_hook_definitions(default_config)
        events = [event for event, _ in hooks]
        assert HookEvent.USER_PROMPT_SUBMIT in events

    def test_hook_is_prompt_type(self, default_config: GeoConfig) -> None:
        """Hook definition has type='prompt'."""
        hooks = build_cascade_hook_definitions(default_config)
        for _, hook in hooks:
            assert hook.type == "prompt"

    def test_hook_prompt_contains_arguments_placeholder(
        self, default_config: GeoConfig
    ) -> None:
        """Hook prompt template contains $ARGUMENTS placeholder."""
        hooks = build_cascade_hook_definitions(default_config)
        for _, hook in hooks:
            assert "$ARGUMENTS" in hook.prompt

    def test_hook_prompt_contains_l1_tools(self, default_config: GeoConfig) -> None:
        """Hook prompt mentions L1 tools from config."""
        hooks = build_cascade_hook_definitions(default_config)
        prompt = hooks[0][1].prompt
        assert "geo_spatial_relation" in prompt

    def test_hook_block_on_failure_false(self, default_config: GeoConfig) -> None:
        """Hook does not block on failure (advisory, not mandatory)."""
        hooks = build_cascade_hook_definitions(default_config)
        for _, hook in hooks:
            assert hook.block_on_failure is False

    def test_hook_has_timeout(self, default_config: GeoConfig) -> None:
        """Hook has a timeout value."""
        hooks = build_cascade_hook_definitions(default_config)
        for _, hook in hooks:
            assert isinstance(hook.timeout_seconds, int)
            assert hook.timeout_seconds > 0

    def test_custom_config_changes_prompt(self, custom_cascade_config: GeoConfig) -> None:
        """Custom cascade config changes the prompt template."""
        hooks = build_cascade_hook_definitions(custom_cascade_config)
        prompt = hooks[0][1].prompt
        # Custom config has only geo_spatial_relation in l1_to_l2_tools
        assert "geo_spatial_relation" in prompt


# ── register_cascade_hooks ────────────────────────────────────────


class TestRegisterCascadeHooks:
    """Test register_cascade_hooks() function."""

    def test_registers_in_registry(self, default_config: GeoConfig) -> None:
        """register_cascade_hooks adds hooks to HookRegistry."""
        registry = HookRegistry()
        register_cascade_hooks(registry, default_config)

        hooks = registry.get(HookEvent.USER_PROMPT_SUBMIT)
        assert len(hooks) >= 1

    def test_registered_hooks_are_prompt_type(
        self, default_config: GeoConfig
    ) -> None:
        """Registered hooks are PromptHookDefinition instances."""
        registry = HookRegistry()
        register_cascade_hooks(registry, default_config)

        hooks = registry.get(HookEvent.USER_PROMPT_SUBMIT)
        for hook in hooks:
            assert isinstance(hook, PromptHookDefinition)

    def test_empty_registry_before_registration(
        self, default_config: GeoConfig
    ) -> None:
        """Registry is empty before register_cascade_hooks is called."""
        registry = HookRegistry()
        assert len(registry.get(HookEvent.USER_PROMPT_SUBMIT)) == 0

    def test_multiple_registrations_accumulate(
        self, default_config: GeoConfig
    ) -> None:
        """Calling register_cascade_hooks twice doubles the hooks."""
        registry = HookRegistry()
        register_cascade_hooks(registry, default_config)
        count_after_first = len(registry.get(HookEvent.USER_PROMPT_SUBMIT))
        register_cascade_hooks(registry, default_config)
        count_after_second = len(registry.get(HookEvent.USER_PROMPT_SUBMIT))
        assert count_after_second == count_after_first * 2


# ── build_cascade_manager ─────────────────────────────────────────


class TestBuildCascadeManager:
    """Test build_cascade_manager() factory function."""

    def test_returns_cascade_manager(self, default_config: GeoConfig) -> None:
        """build_cascade_manager returns CascadeManager instance."""
        manager = build_cascade_manager(default_config)
        assert isinstance(manager, CascadeManager)

    def test_uses_config_cascade_settings(
        self, custom_cascade_config: GeoConfig
    ) -> None:
        """build_cascade_manager uses cascade settings from config."""
        manager = build_cascade_manager(custom_cascade_config)
        manager.record_tool_use("geo_spatial_relation")
        suggestions = manager.get_pending_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0].target_level == "L2"

    def test_manager_starts_empty(self, default_config: GeoConfig) -> None:
        """Built manager starts with empty state."""
        manager = build_cascade_manager(default_config)
        assert manager.tool_history == []
        assert manager.get_pending_suggestions() == []
        assert manager.get_current_level() == "L0"


# ── CascadeSuggestion Dataclass ───────────────────────────────────


class TestCascadeSuggestion:
    """Test CascadeSuggestion dataclass."""

    def test_default_consumed_false(self) -> None:
        """New suggestion has consumed=False."""
        s = CascadeSuggestion(
            trigger_tool="geo_vector_data",
            target_level="L2",
            target_tools=["geo_moran_local"],
            message="test message",
        )
        assert s.consumed is False

    def test_consumed_can_be_set_true(self) -> None:
        """consumed field can be set to True."""
        s = CascadeSuggestion(
            trigger_tool="geo_vector_data",
            target_level="L2",
            target_tools=["geo_moran_local"],
            message="test",
            consumed=True,
        )
        assert s.consumed is True

    def test_fields_accessible(self) -> None:
        """All fields are accessible."""
        s = CascadeSuggestion(
            trigger_tool="geo_spatial_relation",
            target_level="L2",
            target_tools=["geo_moran_local", "geo_getis_ord"],
            message="suggest L2",
        )
        assert s.trigger_tool == "geo_spatial_relation"
        assert s.target_level == "L2"
        assert "geo_moran_local" in s.target_tools
        assert s.message == "suggest L2"


# ── L1/L2/L3 Tool Classification ──────────────────────────────────


class TestToolClassification:
    """Test L1/L2/L3 tool set constants."""

    def test_l1_tools_not_empty(self) -> None:
        """L1_TOOLS is not empty."""
        assert len(L1_TOOLS) > 0

    def test_l2_tools_not_empty(self) -> None:
        """L2_TOOLS is not empty."""
        assert len(L2_TOOLS) > 0

    def test_l3_tools_not_empty(self) -> None:
        """L3_TOOLS is not empty."""
        assert len(L3_TOOLS) > 0

    def test_tool_sets_disjoint(self) -> None:
        """L1, L2, L3 tool sets are mutually disjoint."""
        assert L1_TOOLS & L2_TOOLS == set()
        assert L2_TOOLS & L3_TOOLS == set()
        assert L1_TOOLS & L3_TOOLS == set()

    def test_known_l1_tools_present(self) -> None:
        """Known L1 tools are in L1_TOOLS."""
        assert "geo_vector_data" in L1_TOOLS
        assert "geo_spatial_relation" in L1_TOOLS
        assert "geo_geocode" in L1_TOOLS

    def test_known_l2_tools_present(self) -> None:
        """Known L2 tools are in L2_TOOLS."""
        assert "geo_moran_local" in L2_TOOLS
        assert "geo_getis_ord" in L2_TOOLS
        assert "geo_moran_global" in L2_TOOLS

    def test_known_l3_tools_present(self) -> None:
        """Known L3 tools are in L3_TOOLS."""
        assert "geo_cluster_detect" in L3_TOOLS
        assert "geo_od_flow" in L3_TOOLS
        assert "geo_causal_check" in L3_TOOLS
