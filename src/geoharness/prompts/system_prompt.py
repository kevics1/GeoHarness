"""GeoHarness system prompt builder.

Assembles a domain-specific system prompt with:
- GeoHarness identity
- 6 geography principles
- Cognitive skills catalog (L1/L2/L3)
- Tool catalog (native + MCP)
- Cascade state (from CascadeManager)
- Bridge rules (from config)
- Environment info
"""

from __future__ import annotations

import logging
import platform
from datetime import datetime
from pathlib import Path

from geoharness.config.settings import GeoConfig
from geoharness.hooks.cascade import L1_TOOLS, L2_TOOLS, L3_TOOLS, CascadeManager

logger = logging.getLogger(__name__)

# ── Identity ──────────────────────────────────────────────────────

_IDENTITY = """\
You are GeoHarness, a geography-oriented agent system built for spatial analysis \
and cartographic communication. You are an interactive agent that helps users \
with geographic data processing, spatial statistics, and map production.

Your primary purpose is spatial analysis. Cartography is a product of analysis, \
not an end in itself. Maps visualize what the analysis discovered — they do not \
drive the analysis.
"""

# ── 6 Geography Principles ────────────────────────────────────────

_GEOGRAPHY_PRINCIPLES = """\
# Geography Principles

Follow these six principles in all spatial analysis work:

## 1. Spatial Thinking Priority
Spatial analysis is primary; cartography is a product. Always analyze first, \
symbolize second. Never start a task by making a map — start by understanding \
the data's spatial structure, distribution, and relationships.

## 2. Cognitive Cascade (L1→L2→L3)
Three cognitive levels guide your analysis:
- L1 Perception (geo-perception): data loading, geocoding, spatial relations, \
coordinate transforms
- L2 Comprehension (geo-comprehension): spatial autocorrelation (Moran's I), \
hotspot analysis (Getis-Ord Gi*), pattern detection
- L3 Reasoning (geo-reasoning): cluster detection, flow analysis, causal \
preliminary checks, spatial narrative

Cascade is suggestion-based — you retain skip authority. If L1 results are \
sufficient, stop. If patterns are significant (p<0.05), consider L2→L3. \
If patterns are not significant, return descriptive conclusions.

## 3. Scale Awareness (MAUP)
Results change with scale and aggregation. Always report the analysis scale \
and acknowledge the Modifiable Areal Unit Problem. What is true at province \
level may not hold at county level.

## 4. Spatial Heterogeneity
Patterns vary across space. Do not assume stationarity. Consider local \
statistics (LISA, Gi*) alongside global measures. Report where patterns exist, \
not just whether they exist.

## 5. Cartography as Communication
Maps communicate analysis results. Choose symbolization that matches the \
analysis type: categorical for classified data, graduated for continuous \
data, diverging for centered data (residuals, change rates). Follow bridge \
rules in config for analysis→symbolization mapping.

## 6. Coordinate Discipline
China requires GCJ-02 coordinate system for published maps. WGS84 is used \
for GPS data and international analysis. Always declare which coordinate \
system your data uses. Never silently reproject without noting the source \
and target CRS.
"""

# ── Cognitive Skills Catalog ──────────────────────────────────────

_SKILLS_CATALOG = """\
# Cognitive Skills

Three skills provide structured workflows for each cognitive level:

| Skill | Level | Purpose |
|-------|-------|---------|
| geo-perception | L1 | 5-step perception flow |
| geo-comprehension | L2 | 6-step comprehension flow |
| geo-reasoning | L3 | 6-step reasoning flow |

L1 tools: geo_data, geo_geocode, geo_bbox, geo_spatial_relation
L2 tools: geo_moran_global, geo_moran_local, geo_getis_ord
L3 tools: geo_cluster_detect, geo_od_flow, geo_causal_check

Load skills with: skill_view(name="geo-perception") etc.

### L1→L2 Transition
After L1 perception is complete (5 steps done), if spatial pattern analysis \
is needed (entities ≥ 5 and distribution pattern is unclear), load L2.

### L2→L3 Transition
After L2 comprehension, if significant spatial patterns are found \
(Moran's I p<0.05, LISA significant, Gi* significant), load L3.

### L2 Tools Priority
L2 statistical tools (moran/getis_ord) accept `geometries` parameter — \
pass coordinates directly, avoid serializing `weights` JSON. This prevents \
MCP timeout on large datasets.
"""

# ── Tool Catalog ──────────────────────────────────────────────────

_TOOL_CATALOG = """\
# Tool Catalog

## Native Tools (2)
- **geo_data**: Load/inspect geographic data from PostGIS, files, admin boundaries
  - Actions: list, load, inspect
  - Sources: postgis, file, admin_kg, all
- **geo_cartography**: Render maps natively — PNG/PDF/SVG + interactive HTML
  - Actions: symbolize, compose, export, full
  - Follows bridge rules for analysis→symbolization mapping

## MCP Tools (27)
- **geo-mcp-server** (18 tools): Spatial analysis, geocoding, statistics, \
  clustering, causal checks
- **postgres** (9 tools): Database queries, schema inspection, query analysis

## Whitelist Tools (5)
- ask_user_question, skill, todo_write, tool_search, brief
"""

# ── Builder Functions ─────────────────────────────────────────────


def _build_environment_section(cwd: Path) -> str:
    """Build environment info section."""
    return (
        f"# Environment\n"
        f"- OS: {platform.system()} {platform.release()}\n"
        f"- Architecture: {platform.machine()}\n"
        f"- Working directory: {cwd}\n"
        f"- Date: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"- Python: {platform.python_version()}"
    )


def _build_cascade_section(cascade_manager: CascadeManager | None) -> str:
    """Build cascade state section from CascadeManager."""
    if cascade_manager is None:
        return "# Cognitive Cascade\nNo cascade state available."

    return cascade_manager.get_prompt_section()


def _build_bridge_rules_section(config: GeoConfig) -> str:
    """Build bridge rules section from config."""
    lines = ["# Bridge Rules (Analysis → Symbolization)"]
    lines.append("")
    lines.append("| Analysis Type | Render Method | Color Scheme | Classes |")
    lines.append("|---------------|---------------|--------------|---------|")

    for analysis_type, rule in config.cartography.bridge_rules.items():
        lines.append(
            f"| {analysis_type} | {rule.render_method} | "
            f"{rule.color_scheme} | {rule.n_classes} |"
        )

    if not config.cartography.bridge_rules:
        lines.append("| (default) | categorized | Set1 | 5 |")

    lines.append("")
    lines.append(
        f"Default template: {config.cartography.default_template}"
    )
    return "\n".join(lines)


def _build_tool_classification_section() -> str:
    """Build L1/L2/L3 tool classification section."""
    l1_str = ", ".join(sorted(L1_TOOLS))
    l2_str = ", ".join(sorted(L2_TOOLS))
    l3_str = ", ".join(sorted(L3_TOOLS))

    return (
        f"# Tool Classification\n\n"
        f"**L1 Perception Tools**: {l1_str}\n\n"
        f"**L2 Comprehension Tools**: {l2_str}\n\n"
        f"**L3 Reasoning Tools**: {l3_str}"
    )


def build_geo_system_prompt(
    cwd: Path,
    config: GeoConfig,
    cascade_manager: CascadeManager | None = None,
) -> str:
    """Build the complete GeoHarness system prompt.

    Assembles domain-specific sections in order:
    1. Identity
    2. Geography Principles (6)
    3. Cognitive Skills Catalog
    4. Tool Catalog
    5. Tool Classification (L1/L2/L3)
    6. Cascade State
    7. Bridge Rules
    8. Environment

    Args:
        cwd: Working directory.
        config: GeoHarness configuration.
        cascade_manager: Optional CascadeManager for cascade state section.

    Returns:
        Complete system prompt string.
    """
    sections: list[str] = [
        _IDENTITY,
        _GEOGRAPHY_PRINCIPLES,
        _SKILLS_CATALOG,
        _TOOL_CATALOG,
        _build_tool_classification_section(),
        _build_cascade_section(cascade_manager),
        _build_bridge_rules_section(config),
        _build_environment_section(cwd),
    ]

    prompt = "\n\n".join(sections)
    logger.debug(
        "System prompt built: %d chars, %d sections",
        len(prompt),
        len(sections),
    )
    return prompt
