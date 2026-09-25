"""GeoCartographyTool — cartography pipeline orchestration tool.

Coordinates a complete cartography pipeline:
1. Reads analysis type from context
2. Looks up bridge rules in config (analysis_type → symbolization)
3. Plans symbolize → compose → export for the native renderer

Bridge rules are config-driven — adding new analysis types = YAML edit.
Rendering is performed natively (PNG/PDF/SVG/HTML), not via an external
desktop GIS application.
"""

from __future__ import annotations

from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field


class GeoCartographyInput(BaseModel):
    """Input model for geo_cartography tool."""

    action: Literal["symbolize", "compose", "export", "full"] = Field(
        default="full",
        description=(
            "symbolize: apply symbolization to a layer; "
            "compose: load template + add map elements; "
            "export: export as PDF/image; "
            "full: symbolize + compose + export in one step"
        ),
    )
    layer_name: str = Field(
        default="",
        description="Data layer name to symbolize (for symbolize/full).",
    )
    analysis_type: str = Field(
        default="",
        description=(
            "Analysis type for bridge rule lookup (e.g., 'moran_local', "
            "'getis_ord', 'cluster_detect'). Determines symbolization strategy."
        ),
    )
    field_name: str = Field(
        default="",
        description="Field name for symbolization (categorized/graduated).",
    )
    output_format: Literal["pdf", "image"] = Field(
        default="pdf",
        description="Export format.",
    )
    output_path: str = Field(
        default="",
        description="Output file path. If empty, saves to ~/.geoharness/exports/.",
    )


class GeoCartographyTool(BaseTool):
    """Cartography pipeline orchestration tool."""

    name: str = "geo_cartography"
    description: str = (
        "Cartography pipeline — render maps natively (PNG/PDF/SVG/HTML): "
        "symbolize → compose → export. Reads bridge rules from config to "
        "auto-select symbolization based on analysis type."
    )
    input_model: type[BaseModel] = GeoCartographyInput

    async def execute(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        """Execute the geo_cartography tool."""
        assert isinstance(arguments, GeoCartographyInput)
        args: GeoCartographyInput = arguments

        # Get config from tool_metadata
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(
                output="Error: GeoHarness config not found in context.",
                is_error=True,
            )

        bridge_rules = config.cartography.bridge_rules
        template_path = config.cartography.default_template

        if args.action == "symbolize":
            return self._symbolize(args, bridge_rules)
        elif args.action == "compose":
            return self._compose(args, template_path)
        elif args.action == "export":
            return self._export(args)
        elif args.action == "full":
            return self._full_pipeline(args, bridge_rules, template_path)
        else:
            return ToolResult(
                output=f"Unknown action: {args.action}",
                is_error=True,
            )

    def _get_bridge_rule(
        self,
        analysis_type: str,
        bridge_rules: dict[str, Any],
    ) -> dict[str, Any]:
        """Look up bridge rule for an analysis type.

        Falls back to default rule if analysis type not found.
        """
        rule = bridge_rules.get(analysis_type)
        if rule is None:
            # Default: simple categorized symbolization
            return {
                "render_method": "categorized",
                "color_scheme": "Set1",
                "n_classes": 0,
            }

        # Convert BridgeRule dataclass to dict if needed
        if hasattr(rule, "render_method"):
            return {
                "render_method": rule.render_method,
                "color_scheme": rule.color_scheme,
                "n_classes": rule.n_classes,
            }
        return rule if isinstance(rule, dict) else dict(rule)

    def _symbolize(
        self,
        args: GeoCartographyInput,
        bridge_rules: dict[str, Any],
    ) -> ToolResult:
        """Generate the symbolization plan for the layer.

        Describes how the native renderer will style the layer based on
        the analysis type's bridge rule.
        """
        if not args.layer_name:
            return ToolResult(
                output="Error: 'layer_name' is required for symbolize action.",
                is_error=True,
            )

        if not args.analysis_type:
            return ToolResult(
                output="Error: 'analysis_type' is required for symbolize. "
                "Specify the analysis type (e.g., 'moran_local', 'getis_ord').",
                is_error=True,
            )

        rule = self._get_bridge_rule(args.analysis_type, bridge_rules)
        method = rule.get("render_method", "categorized")
        colors = rule.get("color_scheme", "Set1")
        n_classes = rule.get("n_classes", 5)

        instructions: list[str] = []
        instructions.append(
            f"Symbolization plan for layer '{args.layer_name}':"
        )
        instructions.append(f"  Analysis type: {args.analysis_type}")
        instructions.append(f"  Render method: {method}")
        instructions.append(f"  Color scheme: {colors}")

        if method == "categorized":
            field = args.field_name or "(auto-detect category field)"
            instructions.append(
                f"  → Categorized rendering: field='{field}', "
                f"color_scheme='{colors}' (one color per category)"
            )
        elif method == "graduated":
            field = args.field_name or "(auto-detect value field)"
            instructions.append(
                f"  → Graduated rendering: field='{field}', "
                f"color_scheme='{colors}', n_classes={n_classes}"
            )
        elif method == "flow":
            instructions.append(
                f"  → Flow-line rendering for '{args.layer_name}'"
            )
        elif method == "none":
            instructions.append(
                "  → No map symbolization needed for this analysis type "
                "(e.g., global statistic)."
            )

        return ToolResult(
            output="\n".join(instructions),
            metadata={"bridge_rule": rule},
        )

    def _compose(
        self,
        args: GeoCartographyInput,
        template_path: str,
    ) -> ToolResult:
        """Generate composition instructions using QPT template."""
        if not template_path:
            return ToolResult(
                output="Error: No default template configured. "
                "Set cartography.default_template in config.yaml.",
                is_error=True,
            )

        instructions: list[str] = []
        instructions.append("Composition plan:")
        instructions.append(f"  Template/reference: {template_path}")
        instructions.append("  Layout elements (rendered natively):")
        instructions.append(
            "  1. map frame — symbolize the layer into the plot area"
        )
        instructions.append("  2. legend — color/class legend")
        instructions.append("  3. scale bar — metric scale")
        instructions.append("  4. north arrow — orientation indicator")
        instructions.append(
            "  5. title — analysis type and data source caption"
        )

        return ToolResult(output="\n".join(instructions))

    def _export(self, args: GeoCartographyInput) -> ToolResult:
        """Generate export instructions."""
        output_path = args.output_path or "~/.geoharness/exports/map_output"

        instructions: list[str] = []
        instructions.append(f"Export plan ({args.output_format}):")

        if args.output_format == "pdf":
            instructions.append(
                f"  → Native renderer writes PDF to '{output_path}.pdf'"
            )
        else:
            instructions.append(
                f"  → Native renderer writes image to '{output_path}.png'"
            )

        return ToolResult(output="\n".join(instructions))

    def _full_pipeline(
        self,
        args: GeoCartographyInput,
        bridge_rules: dict[str, Any],
        template_path: str,
    ) -> ToolResult:
        """Execute full pipeline: symbolize → compose → export."""
        sym_result = self._symbolize(args, bridge_rules)
        if sym_result.is_error:
            return sym_result

        comp_result = self._compose(args, template_path)
        if comp_result.is_error:
            return comp_result

        exp_result = self._export(args)

        # Combine all results
        combined = "\n\n".join([
            sym_result.output,
            comp_result.output,
            exp_result.output,
        ])

        return ToolResult(
            output=f"Full cartography pipeline:\n\n{combined}",
            metadata={
                "bridge_rule": sym_result.metadata.get("bridge_rule", {}),
            },
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        """geo_cartography writes files (exports) — not read-only."""
        assert isinstance(arguments, GeoCartographyInput)
        return arguments.action == "symbolize"
